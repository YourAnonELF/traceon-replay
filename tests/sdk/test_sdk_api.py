"""``record_run`` / ``RunHandle``: lifecycle, fixture files, run payloads, secrets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sdk_testutil import SECRETS

from traceon.core.errors import TraceonError
from traceon.core.io import dump_fixture, load_fixture, parse_fixture
from traceon.core.runtime import current_session
from traceon.sdk import CapturePolicy, RedactionPolicy, RunHandle, model, record_run, span, tool


@tool
def echo(value: Any) -> Any:
    return value


@tool
def fail(value: Any) -> None:
    raise RuntimeError(f"failed with {value}")


@model
def llm(prompt: str) -> str:
    return f"answer to {prompt}"


def test_fixture_written_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "run.json"
    with record_run("roundtrip", path, input={"q": "hi"}, metadata={"suite": "unit"}) as run:
        assert isinstance(run, RunHandle)
        assert current_session() is run.session
        echo("x")
        run.set_output({"answer": 42})
    assert current_session() is None
    assert run.path == path
    loaded = load_fixture(path)
    assert loaded == run.fixture
    assert parse_fixture(path.read_text(encoding="utf-8")) == loaded
    assert loaded.run_id == run.run_id and loaded.run_id.startswith("run_")
    assert loaded.fixture_id.startswith("fx_")
    assert loaded.input.value == {"q": "hi"}
    assert loaded.output.value == {"answer": 42}
    assert loaded.metadata == {"suite": "unit"}
    assert loaded.environment.python and loaded.environment.traceon_version
    assert loaded.created_at.tzinfo is not None
    assert loaded.mode == "record" and loaded.status == "ok"


def test_fixture_written_when_body_raises(tmp_path: Path) -> None:
    path = tmp_path / "err.json"
    with pytest.raises(ZeroDivisionError), record_run("crash", path) as run:
        echo(1)
        _ = 1 / 0
    fx = load_fixture(path)
    assert fx == run.fixture
    assert fx.status == "error"
    assert fx.error is not None and fx.error.type == "builtins.ZeroDivisionError"
    root = fx.events[0]
    assert root.status == "error" and root.error is not None
    assert fx.output.state == "not_captured"


async def test_async_record_run(tmp_path: Path) -> None:
    path = tmp_path / "async.json"
    async with record_run("async-run", path) as run:
        echo("a")
        run.set_output(None)
    fx = load_fixture(path)
    assert [e.name for e in fx.events] == ["async-run", "echo"]
    assert fx.output.state == "captured" and fx.output.value is None


def test_nested_record_run_rejected() -> None:
    inner = record_run("inner")
    with record_run("outer") as run:
        with pytest.raises(
            TraceonError, match="cannot start inside another active Traceon session"
        ):
            inner.__enter__()
        echo("still recording")
    assert [e.name for e in run.fixture.events] == ["outer", "echo"]


def test_fixture_unavailable_before_exit_and_single_use() -> None:
    handle = record_run("once")
    with pytest.raises(TraceonError, match="only available after"):
        _ = handle.fixture
    with handle, pytest.raises(TraceonError):
        _ = handle.fixture
    with pytest.raises(TraceonError, match="only be entered once"), handle:
        pass
    with pytest.raises(TraceonError, match="too late"):
        handle.set_output(1)


def test_no_output_and_no_input_are_not_captured() -> None:
    with record_run("empty") as run:
        pass
    fx = run.fixture
    assert fx.input.state == "not_captured"
    assert fx.output.state == "not_captured"
    assert [e.name for e in fx.events] == ["empty"]


def test_run_output_capture_disabled() -> None:
    with record_run("no-out", capture=CapturePolicy(run_output=False)) as run:
        run.set_output("secret plan")
    assert run.fixture.output.state == "not_captured"


def test_redaction_info_recorded() -> None:
    policy = RedactionPolicy(policy_id="strict", redact_emails=True)
    with record_run("redaction-info", redaction=policy) as run:
        echo({"password": "pw", "contact": "bob@example.com"})
    info = run.fixture.redaction
    assert info.policy_id == "strict"
    assert "email" in info.rules and "sensitive_key" in info.rules
    # Request paths are under the bound parameter; response paths are relative to the result.
    assert info.fields_omitted == ["$.contact", "$.password", "$.value.contact", "$.value.password"]


def test_save_failure_without_body_error_raises_traceon_error(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with (
        pytest.raises(TraceonError, match="could not write fixture"),
        record_run("bad-path", blocker / "run.json"),
    ):
        pass


def test_no_secret_reaches_the_fixture_file(tmp_path: Path) -> None:
    """Every default rule, through every payload channel, never appears in the file."""
    path = tmp_path / "secrets.json"
    texts = [text for text, _ in SECRETS.values()]
    with (
        pytest.raises(RuntimeError),
        record_run(
            "secret-run",
            path,
            input={"texts": texts, "api_key": "plain-key-value"},
            capture=CapturePolicy.full(),
            metadata={"note": texts[0], "Authorization": "Basic dXNlcjpwYXNz"},
        ) as run,
    ):
        for text in texts:
            with span("each", detail=text):
                echo(text)
                llm(text)
                echo({"nested": [{"msg": text}], "token": "tkn-value-123"})
        run.set_output(texts)
        fail(texts)
    dumped = path.read_text(encoding="utf-8")
    assert dumped == dump_fixture(run.fixture)
    raw = json.dumps(json.loads(dumped))  # normalize escapes (e.g. PEM newlines)
    for rule, (_, secret) in SECRETS.items():
        assert secret not in dumped, rule
        assert secret not in raw, rule
    for plain in ("plain-key-value", "dXNlcjpwYXNz", "tkn-value-123"):
        assert plain not in dumped
    fx = run.fixture
    assert fx.status == "error"
    assert fx.error is not None and "REDACTED" in fx.error.message
    assert set(fx.redaction.fields_omitted) >= {"$.api_key", "$.value", "$.prompt"}
