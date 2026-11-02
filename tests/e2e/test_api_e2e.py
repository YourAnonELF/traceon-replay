"""Record -> replay -> diff through the public ``traceon`` API."""

from __future__ import annotations

import contextlib
import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import e2e_agents as agents
import pytest

from traceon import (
    DivergenceError,
    Fixture,
    NetworkBlockedError,
    RecordedCallError,
    UnmatchedCallError,
    compare,
    load_fixture,
    record_run,
    replay_run,
)
from traceon.diff import metrics as diff_metrics

QUESTION = "replay"
DATA = Path(__file__).resolve().parent.parent / "integrations" / "data"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    agents.REAL_CALLS.clear()
    yield
    agents.REAL_CALLS.clear()


def record(path: Path, fn: Any = agents.research, **kwargs: Any) -> Fixture:
    with record_run("e2e-research", fixture=path, input=kwargs) as run:
        run.set_output(fn(**kwargs))
    return run.fixture


@pytest.fixture
def recorded(tmp_path: Path) -> Path:
    path = tmp_path / "baseline.json"
    record(path, question=QUESTION)
    agents.REAL_CALLS.clear()
    return path


def test_record_writes_a_valid_fixture(recorded: Path) -> None:
    fixture = load_fixture(recorded)
    assert fixture.schema_version == "traceon.fixture/v1"
    assert fixture.status == "ok"
    labels = [f"{e.kind}:{e.name}" for e in fixture.calls()]
    assert labels == [
        "value:uuid4",
        "value:now",
        "tool:search",
        "tool:fetch",
        "tool:fetch",
        "model:answer",
        "tool:send_email",
    ]
    root = fixture.events[0]
    assert root.kind == "span" and root.name == "e2e-research"
    assert all(e.parent_id is not None for e in fixture.events[1:])
    assert next(e for e in fixture.calls() if e.name == "send_email").side_effect
    usage = next(e for e in fixture.calls() if e.name == "answer").usage
    assert usage is not None and usage.input_tokens == 20


def test_secrets_never_reach_the_fixture(recorded: Path) -> None:
    text = recorded.read_text(encoding="utf-8")
    assert "e2eSecretValue" not in text
    fetch = next(e for e in load_fixture(recorded).calls() if e.name == "fetch")
    assert fetch.request.value["api_key"].startswith("[REDACTED:")
    assert "$.api_key" in fetch.request.redacted_paths


def test_strict_replay_is_offline_and_identical(recorded: Path) -> None:
    baseline = load_fixture(recorded)
    with replay_run(recorded) as handle:
        output = agents.research(QUESTION)
        handle.set_output(output)
    report = handle.report
    assert report.ok
    assert report.first_divergence is None
    assert report.calls_served == report.calls_total_recorded == len(baseline.calls())
    assert report.unconsumed == []
    assert sum(agents.REAL_CALLS.values()) == 0  # nothing real ran, not even send_email
    assert output == baseline.output.value


def test_replay_is_repeatable(recorded: Path) -> None:
    outputs = []
    for _ in range(3):
        with replay_run(recorded) as handle:
            outputs.append(agents.research(QUESTION))
            handle.set_output(outputs[-1])
    assert outputs[0] == outputs[1] == outputs[2]


def test_changed_argument_fails_closed(recorded: Path) -> None:
    with pytest.raises(UnmatchedCallError) as info, replay_run(recorded) as handle:
        agents.research_changed_limit(QUESTION)
    d = info.value.divergence
    assert d.kind == "argument_mismatch"
    assert d.label == "tool:search"
    assert d.path == "$.limit"
    assert (d.expected, d.actual) == (2, 3)
    assert d.position == 2
    assert agents.REAL_CALLS["search"] == 0
    assert handle.report.first_divergence is not None


def test_missing_calls_are_reported_on_exit(recorded: Path) -> None:
    with pytest.raises(DivergenceError) as info, replay_run(recorded) as handle:
        handle.set_output(agents.research_skips_email(QUESTION))
    kinds = {d.kind for d in info.value.divergences}
    assert kinds & {"unexpected_call", "kind_mismatch", "name_mismatch", "missing_call"}


def test_report_without_raising(recorded: Path) -> None:
    handle = replay_run(recorded, raise_on_divergence=False)
    with handle, contextlib.suppress(UnmatchedCallError):
        agents.research_changed_limit(QUESTION)
    assert not handle.report.ok
    assert handle.report.divergences[0].kind == "argument_mismatch"


def test_override_replaces_a_response(recorded: Path) -> None:
    def fake_search(query: str, limit: int) -> list[str]:
        return [f"{query}-override-{i}" for i in range(limit)]

    handle = replay_run(
        recorded, mode="override", overrides={"tool:search": fake_search}, raise_on_divergence=False
    )
    # fetch() is then called with documents that were never recorded.
    with handle, contextlib.suppress(UnmatchedCallError):
        agents.research(QUESTION)
    assert handle.report.calls_overridden == 1
    first = handle.report.first_divergence
    assert first is not None and first.label == "tool:fetch"


def test_recorded_tool_error_is_replayed(tmp_path: Path) -> None:
    path = tmp_path / "flaky.json"
    with record_run("flaky", fixture=path) as run:
        run.set_output(agents.research_with_failure("doc-1"))
    failed = run.fixture.calls()[0]
    assert failed.status == "error" and failed.error is not None
    assert failed.error.type == "builtins.ConnectionError"

    agents.REAL_CALLS.clear()
    with replay_run(path) as handle:
        with pytest.raises(RecordedCallError) as info:
            agents.flaky("doc-1")
        handle.set_output(run.fixture.output.value)
    assert info.value.error_type == "builtins.ConnectionError"
    assert "upstream reset" in info.value.error_message
    assert agents.REAL_CALLS["flaky"] == 0


def test_network_is_blocked_during_replay(recorded: Path) -> None:
    with replay_run(recorded, raise_on_divergence=False) as handle:
        with pytest.raises(NetworkBlockedError):
            # Loopback only: even if the guard were broken nothing leaves this machine.
            socket.create_connection(("127.0.0.1", 9), timeout=0.05)
        handle.set_output(agents.research(QUESTION))
    assert handle.report.network_attempts >= 1


async def test_async_concurrent_agent_by_key(tmp_path: Path) -> None:
    path = tmp_path / "async.json"
    keys = ["a", "bb", "ccc", "dddd"]
    async with record_run("async-agent", fixture=path, input={"keys": keys}) as run:
        run.set_output(await agents.research_async(keys))
    assert [e.name for e in run.fixture.calls()] == ["async_lookup"] * 4

    agents.REAL_CALLS.clear()
    async with replay_run(path, order="by_key") as handle:
        result = await agents.research_async(keys)
        handle.set_output(result)
    assert handle.report.ok
    assert [r["value"] for r in result] == ["A", "BB", "CCC", "DDDD"]
    assert agents.REAL_CALLS["async_lookup"] == 0


def test_saved_replay_diffs_equivalent(recorded: Path, tmp_path: Path) -> None:
    replayed_path = tmp_path / "replayed.json"
    with replay_run(recorded, save_to=replayed_path) as handle:
        handle.set_output(agents.research(QUESTION))
    replayed = load_fixture(replayed_path)
    assert replayed.mode == "replay"
    report = compare(load_fixture(recorded), replayed)
    assert report.equivalent, report.first_divergence
    assert all(pair.status == "same" for pair in report.alignment)


def test_diff_of_two_recordings_finds_first_divergence(recorded: Path, tmp_path: Path) -> None:
    candidate_path = tmp_path / "candidate.json"
    record(candidate_path, fn=agents.research_changed_limit, question=QUESTION)
    report = compare(load_fixture(recorded), load_fixture(candidate_path))
    assert not report.equivalent
    first = report.first_divergence
    assert first is not None
    assert first.label == "tool:search"
    assert first.path == "$.limit"
    assert report.candidate_metrics.tool_calls > report.baseline_metrics.tool_calls
    # The report is plain JSON data (used by the CLI, CI summaries and Studio).
    json.dumps(report.model_dump(mode="json"))


def test_two_live_recordings_differ_only_in_nondeterministic_values(
    recorded: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other.json"
    record(other, question=QUESTION)
    report = compare(load_fixture(recorded), load_fixture(other), compare_output=False)
    # Same calls with the same arguments; only uuid4()/now() produced new values.
    assert [p.kind for p in report.alignment] == [e.kind for e in load_fixture(recorded).calls()]
    assert all(not d.label or d.label.startswith("value:") for d in report.divergences)


def test_imported_langfuse_trace_is_a_regular_fixture(tmp_path: Path) -> None:
    from traceon.core.io import save_fixture
    from traceon.integrations.langfuse import import_trace

    data = json.loads((DATA / "langfuse_trace.json").read_text(encoding="utf-8"))
    path = save_fixture(import_trace(data), tmp_path / "lf.json")
    fixture = load_fixture(path)
    assert fixture.source == "langfuse"
    assert compare(fixture, fixture).equivalent
    metrics = diff_metrics(fixture)
    assert metrics.model_calls == 3
    assert metrics.tool_calls == 3
    assert metrics.errors >= 1
