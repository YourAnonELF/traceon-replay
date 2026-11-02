from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from pathlib import Path
from typing import Any

import pytest
from replay_support import FixtureBuilder, stand_in

from traceon.core.errors import (
    DivergenceError,
    FixtureError,
    NetworkBlockedError,
    ReplayError,
    TraceonError,
    UnmatchedCallError,
)
from traceon.core.io import load_fixture, save_fixture
from traceon.core.limits import Limits
from traceon.core.runtime import current_session, use_session
from traceon.core.schema import Fixture
from traceon.replay import ReplayHandle, ReplayReport, ReplaySession, replay_run


@stand_in("tool")
def search(query: str, limit: int = 3) -> list[str]:
    raise AssertionError("real search must not run during replay")


@stand_in("model", "gpt")
def summarize(prompt: str) -> str:
    raise AssertionError("real model must not run during replay")


@stand_in("tool")
async def asearch(query: str) -> list[str]:
    raise AssertionError("real async search must not run during replay")


def agent(question: str) -> str:
    hits = search(question)
    return str(summarize(f"summarize: {', '.join(hits)}"))


def recording() -> Fixture:
    b = FixtureBuilder("research-agent")
    b.tool("search", {"query": "weather in Paris", "limit": 3}, ["sunny"])
    b.model("gpt", {"prompt": "summarize: sunny"}, "It is sunny.")
    return b.build(output="It is sunny.")


def test_replay_ok_report() -> None:
    with replay_run(recording()) as handle:
        handle.set_output(agent("weather in Paris"))
    report = handle.report
    assert isinstance(report, ReplayReport)
    assert report.ok and report.first_divergence is None
    assert report.fixture_id == "fx_baseline01"
    assert report.calls_served == 2 and report.calls_overridden == 0
    assert report.calls_total_recorded == 2 and report.network_attempts == 0
    root = report.replayed.events[0]
    assert root.kind == "span" and root.name == "research-agent" and root.parent_id is None
    assert all(e.parent_id == root.event_id for e in report.replayed.calls())


def test_replay_from_path(tmp_path: Path) -> None:
    path = save_fixture(recording(), tmp_path / "rec.json")
    with replay_run(path) as handle:
        handle.set_output(agent("weather in Paris"))
    assert handle.report.ok


def test_missing_fixture_file_raises_fixture_error(tmp_path: Path) -> None:
    with pytest.raises(FixtureError, match="not found"):
        replay_run(tmp_path / "missing.json")


def test_invalid_arguments_rejected_early() -> None:
    with pytest.raises(ValueError, match="order"):
        replay_run(recording(), order="any")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="mode"):
        replay_run(recording(), mode="lenient")  # type: ignore[arg-type]


def test_divergence_raised_by_call_propagates_with_report() -> None:
    with pytest.raises(UnmatchedCallError) as info, replay_run(recording()):
        agent("weather in Pariss")
    report = info.value.traceon_report  # type: ignore[attr-defined]
    assert isinstance(report, ReplayReport) and not report.ok
    assert report.first_divergence is not None
    assert report.first_divergence.message.startswith(
        'Call #0 tool:search diverged at $.query: expected "weather in Paris", got "weather in Pariss"'
    )


def test_swallowed_divergence_still_raises_on_exit() -> None:
    # The agent swallows the error; replay still fails on exit.
    with (
        pytest.raises(DivergenceError) as info,
        replay_run(recording()),
        contextlib.suppress(UnmatchedCallError),
    ):
        search("something else")
    assert info.value.divergence.kind == "argument_mismatch"
    assert info.value.traceon_report.first_divergence == info.value.divergence  # type: ignore[attr-defined]
    assert [d.kind for d in info.value.divergences] == ["argument_mismatch", "missing_call"]


def test_missing_calls_raise_on_exit() -> None:
    with (
        pytest.raises(DivergenceError, match=r"Call #1 model:gpt was recorded but never made"),
        replay_run(recording()),
    ):
        search("weather in Paris")


def test_raise_on_divergence_false_returns_report() -> None:
    with replay_run(recording(), raise_on_divergence=False) as handle:
        search("weather in Paris")
        handle.set_output("nothing")
    report = handle.report
    assert [d.kind for d in report.divergences] == ["missing_call", "output_mismatch"]
    assert report.unconsumed == [recording().calls()[1].event_id]


def test_output_mismatch_detected_on_exit() -> None:
    with pytest.raises(DivergenceError) as info, replay_run(recording()) as handle:
        agent("weather in Paris")
        handle.set_output("It is rainy.")
    assert info.value.divergence.kind == "output_mismatch"


def test_agent_exception_status_mismatch_attached() -> None:
    with pytest.raises(KeyError) as info, replay_run(recording()):
        agent("weather in Paris")
        raise KeyError("parse failure")
    report = info.value.traceon_report  # type: ignore[attr-defined]
    assert report.first_divergence.kind == "status_mismatch"
    assert report.replayed.status == "error"
    assert report.replayed.events[0].status == "error"  # root span


def test_save_to_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "out" / "replayed.json"
    with pytest.raises(DivergenceError), replay_run(recording(), save_to=out):
        search("weather in Paris")
    saved = load_fixture(out)
    assert saved.mode == "replay" and saved.name == "research-agent"
    assert [(e.kind, e.name) for e in saved.calls()] == [("tool", "search")]
    assert saved.calls()[0].call_key == recording().calls()[0].call_key


def test_replayed_fixture_can_itself_be_replayed(tmp_path: Path) -> None:
    out = tmp_path / "replayed.json"
    with replay_run(recording(), save_to=out) as handle:
        handle.set_output(agent("weather in Paris"))
    with replay_run(out) as again:
        again.set_output(agent("weather in Paris"))
    assert again.report.ok and again.report.fixture_id == handle.report.replayed.fixture_id


def test_overrides_through_replay_run() -> None:
    with replay_run(recording(), overrides={"model:gpt": lambda prompt: prompt.upper()}) as handle:
        result = agent("weather in Paris")
    assert result == "SUMMARIZE: SUNNY"
    assert handle.report.calls_overridden == 1 and handle.report.calls_served == 1


def test_override_mode_what_if() -> None:
    def what_if(query: str, limit: int) -> list[str]:
        return ["rain"]

    with replay_run(
        recording(),
        mode="override",
        overrides={"search": what_if, "gpt": "It rains."},
        raise_on_divergence=False,
    ) as handle:
        handle.set_output(agent("weather in Paris"))
    report = handle.report
    assert [d.kind for d in report.divergences] == ["output_mismatch"]
    assert report.calls_overridden == 2


def test_nested_replay_inside_active_session_is_rejected() -> None:
    inner = replay_run(recording())
    with (
        replay_run(recording(), allow_unconsumed=True),
        pytest.raises(TraceonError, match="cannot start inside"),
        inner,
    ):
        pass


def test_nested_inside_foreign_session_is_rejected() -> None:
    session = ReplaySession(recording())
    with use_session(session), pytest.raises(TraceonError):
        replay_run(recording()).__enter__()


def test_session_is_bound_only_inside_block() -> None:
    handle = replay_run(recording(), allow_unconsumed=True)
    with pytest.raises(ReplayError):
        _ = handle.session
    with handle:
        assert current_session() is handle.session
    assert current_session() is None


def test_handle_cannot_be_reentered_and_report_requires_exit() -> None:
    handle = replay_run(recording(), allow_unconsumed=True)
    with pytest.raises(ReplayError, match="after the replay block exits"):
        _ = handle.report
    with handle:
        pass
    with pytest.raises(ReplayError, match="only once"), handle:
        pass


def test_set_output_after_exit_rejected() -> None:
    with replay_run(recording(), allow_unconsumed=True) as handle:
        pass
    with pytest.raises(ReplayError):
        handle.set_output("late")


def test_network_blocked_inside_replay_and_counted() -> None:
    with pytest.raises(NetworkBlockedError) as info, replay_run(recording()):
        agent("weather in Paris")
        socket.create_connection(("192.0.2.1", 9))
    report = info.value.traceon_report  # type: ignore[attr-defined]
    assert report.network_attempts == 1
    assert report.first_divergence.kind == "status_mismatch"
    assert "NetworkBlockedError" in report.first_divergence.message


def test_block_network_false_installs_no_guard() -> None:
    with replay_run(recording(), block_network=False, allow_unconsumed=True):
        assert "connect" not in socket.socket.__dict__


def test_guard_restored_after_replay() -> None:
    original = socket.getaddrinfo
    with replay_run(recording(), allow_unconsumed=True):
        assert socket.getaddrinfo is not original
    assert socket.getaddrinfo is original


def test_replay_time_limit() -> None:
    with (
        pytest.raises(ReplayError, match="wall-clock limit"),
        replay_run(recording(), limits=Limits(max_replay_seconds=0.01)),
    ):
        time.sleep(0.05)
        search("weather in Paris")


async def test_async_context_manager_and_gather_by_key() -> None:
    b = FixtureBuilder("async-agent")
    for q in ("a", "b", "c"):
        b.tool("asearch", {"query": q}, [q.upper()])
    fixture = b.build(output=["A", "B", "C"])

    async def run() -> list[Any]:
        return list(await asyncio.gather(asearch("c"), asearch("a"), asearch("b")))

    async with replay_run(fixture, order="by_key") as handle:
        results = await run()
        handle.set_output(sorted(r[0] for r in results))
    assert results == [["C"], ["A"], ["B"]]
    assert handle.report.ok


async def test_async_divergence_raised_on_exit() -> None:
    b = FixtureBuilder("async-agent")
    b.tool("asearch", {"query": "a"}, ["A"])
    with pytest.raises(DivergenceError):
        async with replay_run(b.build()):
            pass


def test_handle_type() -> None:
    assert isinstance(replay_run(recording()), ReplayHandle)
