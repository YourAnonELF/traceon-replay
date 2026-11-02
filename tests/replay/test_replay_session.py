from __future__ import annotations

import asyncio
import datetime as dt
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from replay_support import FixtureBuilder, acall, call, normalize

from traceon.core.errors import (
    RecordedCallError,
    ReplayError,
    ResponseNotCapturedError,
    UnmatchedCallError,
)
from traceon.core.limits import Limits
from traceon.core.runtime import CallSpec, use_session
from traceon.core.schema import ErrorInfo, Fixture, Payload, Usage
from traceon.replay import session as session_mod
from traceon.replay.session import ReplaySession, default_redactor


def weather_fixture() -> Fixture:
    b = FixtureBuilder()
    b.tool("search", {"query": "weather in Paris"}, ["sunny", "22C"])
    b.model(
        "gpt",
        {"prompt": "summarize: sunny"},
        {"text": "It is sunny."},
        usage=Usage(input_tokens=10, output_tokens=4),
    )
    b.tool("send_email", {"to": "a@example.com"}, {"sent": True})
    return b.build(output={"answer": "It is sunny."})


def replay_all(session: ReplaySession) -> list[Any]:
    return [
        call(session, "tool", "search", {"query": "weather in Paris"}),
        call(session, "model", "gpt", {"prompt": "summarize: sunny"}),
        call(session, "tool", "send_email", {"to": "a@example.com"}),
    ]


# ------------------------------------------------------------------ serving
def test_exact_replay_serves_recorded_responses_without_running_thunks() -> None:
    session = ReplaySession(weather_fixture())
    assert replay_all(session) == [["sunny", "22C"], {"text": "It is sunny."}, {"sent": True}]
    session.set_output({"answer": "It is sunny."})
    fixture = session.finish()
    report = session.report()
    assert report.ok and report.divergences == []
    assert report.calls_served == 3 and report.calls_total_recorded == 3
    assert report.unconsumed == []
    assert fixture.mode == "replay" and fixture.status == "ok"


def test_served_values_are_copies() -> None:
    fixture = weather_fixture()
    session = ReplaySession(fixture)
    result = call(session, "tool", "search", {"query": "weather in Paris"})
    result.append("mutated")
    assert fixture.calls()[0].response.value == ["sunny", "22C"]


def test_decode_is_applied_to_served_value() -> None:
    session = ReplaySession(weather_fixture())
    result = call(session, "tool", "search", {"query": "weather in Paris"}, decode=tuple)
    assert result == ("sunny", "22C")


def test_replayed_fixture_mirrors_recording_and_is_diffable() -> None:
    baseline = weather_fixture()
    session = ReplaySession(baseline)
    with use_session(session), session.span(CallSpec(kind="span", name="agent")) as root:
        replay_all(session)
    replayed = session.finish()
    assert replayed.fixture_id != baseline.fixture_id and replayed.run_id != baseline.run_id
    assert [(e.kind, e.name, e.call_key) for e in replayed.calls()] == [
        (e.kind, e.name, e.call_key) for e in baseline.calls()
    ]
    assert all(e.parent_id == root for e in replayed.calls())
    assert [e.response for e in replayed.calls()] == [e.response for e in baseline.calls()]
    assert replayed.calls()[1].request.state == "hash_only"  # mirrors the recording
    assert replayed.calls()[1].usage == Usage(input_tokens=10, output_tokens=4)
    assert replayed.metadata["replay_of"]["fixture_id"] == baseline.fixture_id
    assert replayed.input == baseline.input
    # Round-trips through validation.
    assert Fixture.model_validate(replayed.model_dump(mode="json")) == replayed


def test_value_events_are_decoded() -> None:
    b = FixtureBuilder()
    b.value("now", {"$datetime": "2026-05-01T12:00:00+00:00"})
    b.value("now", {"$datetime": "2026-05-01T12:00:00"})
    b.value("now", {"$datetime": "2026-05-01T12:00:00Z"})
    b.value("random", 1)
    b.value("uuid4", "0b6f6c1e-8c43-4c57-a5c5-1d1f9d0a7a11")
    b.value("uuid4", {"$uuid": "0b6f6c1e-8c43-4c57-a5c5-1d1f9d0a7a12"})
    session = ReplaySession(b.build())
    expected_dt = dt.datetime(2026, 5, 1, 12, tzinfo=dt.timezone.utc)
    for _ in range(3):
        value = call(session, "value", "now")
        assert value == expected_dt and value.tzinfo is not None
    rnd = call(session, "value", "random")
    assert rnd == 1.0 and isinstance(rnd, float)
    assert call(session, "value", "uuid4") == "0b6f6c1e-8c43-4c57-a5c5-1d1f9d0a7a11"
    assert call(session, "value", "uuid4") == "0b6f6c1e-8c43-4c57-a5c5-1d1f9d0a7a12"
    session.finish()
    assert session.report().ok


# ------------------------------------------------------------- divergences
def test_argument_mismatch_reports_path_expected_actual() -> None:
    session = ReplaySession(weather_fixture())
    with pytest.raises(UnmatchedCallError) as info:
        call(session, "tool", "search", {"query": "weather in Pariss"})
    div = info.value.divergence
    assert div.kind == "argument_mismatch"
    assert div.position == 0
    assert div.label == "tool:search"
    assert div.path == "$.query"
    assert div.expected == "weather in Paris" and div.actual == "weather in Pariss"
    assert div.baseline_event_id == weather_fixture().calls()[0].event_id
    assert div.message.startswith(
        'Call #0 tool:search diverged at $.query: expected "weather in Paris", got "weather in Pariss"'
    )
    assert info.value.divergences == [div]


def test_argument_added_and_removed_messages() -> None:
    b = FixtureBuilder()
    b.tool("search", {"query": "x", "limit": 5}, [])
    b.tool("search", {"query": "x"}, [])
    session = ReplaySession(b.build())
    with pytest.raises(UnmatchedCallError) as removed:
        call(session, "tool", "search", {"query": "x"})
    assert removed.value.divergence.path == "$.limit"
    assert "argument missing, expected 5" in removed.value.divergence.message
    with pytest.raises(UnmatchedCallError) as added:
        call(session, "tool", "search", {"query": "x", "lang": "fr"})
    assert added.value.divergence.path == "$.lang"
    assert 'argument added (not in the recording), got "fr"' in added.value.divergence.message


def test_hash_only_request_mismatch_explains_hashes() -> None:
    session = ReplaySession(weather_fixture())
    call(session, "tool", "search", {"query": "weather in Paris"})
    with pytest.raises(UnmatchedCallError) as info:
        call(session, "model", "gpt", {"prompt": "summarize: rainy"})
    div = info.value.divergence
    assert div.kind == "argument_mismatch" and div.position == 1 and div.label == "model:gpt"
    assert div.expected.startswith("sha256:") and div.actual.startswith("sha256:")
    assert div.expected != div.actual
    assert "hash-only" in div.message and "capture_prompt=True" in div.message


@pytest.mark.parametrize("state", ["not_captured", "redacted"])
def test_uncaptured_request_mismatch(state: str) -> None:
    b = FixtureBuilder()
    b.tool("search", {"query": "a"}, [], request_state=state)
    session = ReplaySession(b.build())
    with pytest.raises(UnmatchedCallError) as info:
        call(session, "tool", "search", {"query": "b"})
    expected_word = "redacted" if state == "redacted" else "not captured"
    assert f"The recorded request {'was ' + expected_word}" in info.value.divergence.message


def test_kind_mismatch() -> None:
    session = ReplaySession(weather_fixture())
    with pytest.raises(UnmatchedCallError) as info:
        call(session, "model", "search", {"query": "weather in Paris"})
    div = info.value.divergence
    assert div.kind == "kind_mismatch" and div.expected == "tool" and div.actual == "model"
    assert "expected a tool call (tool:search), got a model call (model:search)" in div.message


def test_name_mismatch_suggests_close_name() -> None:
    session = ReplaySession(weather_fixture())
    with pytest.raises(UnmatchedCallError) as info:
        call(session, "tool", "serch", {"query": "weather in Paris"})
    div = info.value.divergence
    assert div.kind == "name_mismatch" and div.expected == "search" and div.actual == "serch"
    assert "expected tool:search, got tool:serch" in div.message
    assert "did you mean tool:search?" in div.message


def test_extra_call_after_recording_is_unexpected() -> None:
    session = ReplaySession(weather_fixture())
    replay_all(session)
    with pytest.raises(UnmatchedCallError) as info:
        call(session, "tool", "send_email", {"to": "a@example.com"})
    div = info.value.divergence
    assert div.kind == "unexpected_call" and div.position == 3 and div.label == "tool:send_email"
    assert div.baseline_event_id is None and div.candidate_event_id is not None
    assert "only 3 calls" in div.message and "never runs the real call" in div.message


def test_missing_call_reported_at_finish() -> None:
    session = ReplaySession(weather_fixture())
    call(session, "tool", "search", {"query": "weather in Paris"})
    session.finish()
    report = session.report()
    kinds = [(d.kind, d.position, d.label) for d in report.divergences]
    assert kinds == [("missing_call", 1, "model:gpt"), ("missing_call", 2, "tool:send_email")]
    assert "was recorded but never made" in report.divergences[0].message
    assert report.unconsumed == [e.event_id for e in weather_fixture().calls()[1:]]
    assert not report.ok


def test_allow_unconsumed_suppresses_missing_calls() -> None:
    session = ReplaySession(weather_fixture(), allow_unconsumed=True)
    call(session, "tool", "search", {"query": "weather in Paris"})
    session.finish()
    report = session.report()
    assert report.ok and len(report.unconsumed) == 2


def test_divergence_is_recorded_as_failed_event_in_replayed_fixture() -> None:
    session = ReplaySession(weather_fixture())
    with pytest.raises(UnmatchedCallError):
        call(session, "tool", "search", {"query": "other"})
    replayed = session.finish()
    event = replayed.calls()[0]
    assert event.status == "error"
    assert event.error is not None and event.error.type.endswith("UnmatchedCallError")
    assert event.request.value == {"query": "other"}


def test_imported_fixture_without_call_keys_matches_on_captured_request() -> None:
    b = FixtureBuilder()
    b.tool("search", {"query": "a"}, ["r"], with_call_key=False)
    b.tool("search", {"query": "b"}, ["s"], with_call_key=False)
    session = ReplaySession(b.build())
    assert call(session, "tool", "search", {"query": "a"}) == ["r"]
    with pytest.raises(UnmatchedCallError) as info:
        call(session, "tool", "search", {"query": "c"})
    assert info.value.divergence.path == "$.query"


# ------------------------------------------------------------------- order
def reordered_fixture() -> Fixture:
    b = FixtureBuilder()
    b.tool("search", {"query": "a"}, ["A"])
    b.tool("fetch", {"url": "u"}, "page")
    b.tool("search", {"query": "b"}, ["B"])
    return b.build()


def test_strict_order_rejects_reordered_calls_with_hint() -> None:
    session = ReplaySession(reordered_fixture())
    with pytest.raises(UnmatchedCallError) as info:
        call(session, "tool", "fetch", {"url": "u"})
    msg = info.value.divergence.message
    assert info.value.divergence.kind == "name_mismatch"
    assert "this exact call at #1" in msg and 'order="by_key"' in msg


def test_strict_argument_reorder_hint() -> None:
    session = ReplaySession(reordered_fixture())
    with pytest.raises(UnmatchedCallError) as info:
        call(session, "tool", "search", {"query": "b"})
    assert info.value.divergence.kind == "argument_mismatch"
    assert "this exact call at #2" in info.value.divergence.message


def test_by_key_accepts_reordered_calls() -> None:
    session = ReplaySession(reordered_fixture(), order="by_key")
    assert call(session, "tool", "search", {"query": "b"}) == ["B"]
    assert call(session, "tool", "fetch", {"url": "u"}) == "page"
    assert call(session, "tool", "search", {"query": "a"}) == ["A"]
    session.finish()
    assert session.report().ok


def test_by_key_identical_calls_are_fifo() -> None:
    b = FixtureBuilder()
    b.tool("roll", {}, 1)
    b.tool("roll", {}, 2)
    session = ReplaySession(b.build(), order="by_key")
    assert [call(session, "tool", "roll"), call(session, "tool", "roll")] == [1, 2]


def test_by_key_extra_call_and_unknown_name() -> None:
    session = ReplaySession(reordered_fixture(), order="by_key")
    call(session, "tool", "fetch", {"url": "u"})
    with pytest.raises(UnmatchedCallError) as extra:
        call(session, "tool", "fetch", {"url": "u"})
    assert extra.value.divergence.kind == "unexpected_call"
    assert (
        "all 1 recorded tool:fetch call(s) were already replayed" in extra.value.divergence.message
    )
    with pytest.raises(UnmatchedCallError) as unknown:
        call(session, "tool", "fetsh", {"url": "u"})
    assert "did you mean tool:fetch?" in unknown.value.divergence.message


def test_by_key_argument_mismatch_does_not_consume() -> None:
    session = ReplaySession(reordered_fixture(), order="by_key")
    with pytest.raises(UnmatchedCallError) as info:
        call(session, "tool", "search", {"query": "zzz"})
    assert info.value.divergence.kind == "argument_mismatch"
    assert info.value.divergence.position == 0
    # Both searches can still be served afterwards.
    assert call(session, "tool", "search", {"query": "b"}) == ["B"]
    assert call(session, "tool", "search", {"query": "a"}) == ["A"]


def test_threads_with_by_key() -> None:
    b = FixtureBuilder()
    for i in range(20):
        b.tool("work", {"i": i}, i * i)
    session = ReplaySession(b.build(), order="by_key")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda i: call(session, "tool", "work", {"i": i}), reversed(range(20)))
        )
    assert results == [i * i for i in reversed(range(20))]
    replayed = session.finish()
    assert [e.sequence for e in replayed.events] == sorted(e.sequence for e in replayed.events)
    assert session.report().ok


# --------------------------------------------------------------- overrides
def test_callable_override_by_name_receives_bound_args() -> None:
    seen: dict[str, Any] = {}

    def fake_search(query: str) -> list[str]:
        seen["query"] = query
        return ["overridden"]

    session = ReplaySession(weather_fixture(), overrides={"search": fake_search})
    assert call(session, "tool", "search", {"query": "weather in Paris"}) == ["overridden"]
    assert seen == {"query": "weather in Paris"}
    call(session, "model", "gpt", {"prompt": "summarize: sunny"})
    call(session, "tool", "send_email", {"to": "a@example.com"})
    replayed = session.finish()
    report = session.report()
    assert report.calls_overridden == 1
    assert replayed.calls()[0].response.value == ["overridden"]


def test_constant_override_by_label_wins_over_name() -> None:
    session = ReplaySession(
        weather_fixture(), overrides={"tool:send_email": {"sent": False}, "send_email": "by-name"}
    )
    call(session, "tool", "search", {"query": "weather in Paris"})
    call(session, "model", "gpt", {"prompt": "summarize: sunny"})
    assert call(session, "tool", "send_email", {"to": "a@example.com"}) == {"sent": False}
    replayed = session.finish()
    assert any("'send_email' was never used" in w for w in replayed.warnings)


def test_strict_mode_override_still_checks_arguments() -> None:
    session = ReplaySession(weather_fixture(), overrides={"search": ["x"]})
    with pytest.raises(UnmatchedCallError) as info:
        call(session, "tool", "search", {"query": "something else"})
    assert info.value.divergence.kind == "argument_mismatch"


def test_override_mode_exempts_overridden_calls_from_matching() -> None:
    session = ReplaySession(weather_fixture(), mode="override", overrides={"search": ["rain"]})
    assert call(session, "tool", "search", {"query": "different"}) == ["rain"]
    call(session, "model", "gpt", {"prompt": "summarize: sunny"})
    call(session, "tool", "send_email", {"to": "a@example.com"})
    # Extra overridden calls are allowed too.
    assert call(session, "tool", "search", {"query": "again"}) == ["rain"]
    session.finish()
    report = session.report()
    assert report.ok and report.calls_overridden == 2 and report.calls_served == 2


def test_override_resolves_unservable_response() -> None:
    b = FixtureBuilder()
    b.tool("secret_lookup", {"id": 1}, response_payload=Payload(state="redacted"))
    session = ReplaySession(b.build(), overrides={"secret_lookup": lambda id: f"value-{id}"})
    assert call(session, "tool", "secret_lookup", {"id": 1}) == "value-1"


def test_override_exception_propagates_and_is_recorded() -> None:
    def boom(query: str) -> Any:
        raise KeyError("api-down")

    session = ReplaySession(weather_fixture(), overrides={"search": boom}, allow_unconsumed=True)
    with pytest.raises(KeyError):
        call(session, "tool", "search", {"query": "weather in Paris"})
    replayed = session.finish()
    event = replayed.calls()[0]
    assert event.status == "error" and event.error is not None
    assert event.error.type == "builtins.KeyError"


# ------------------------------------------------------- recorded outcomes
def test_recorded_error_is_reraised() -> None:
    b = FixtureBuilder()
    info = ErrorInfo(type="httpx.ReadTimeout", message="timed out")
    b.tool("search", {"query": "q"}, status="error", error=info)
    session = ReplaySession(b.build())
    with pytest.raises(RecordedCallError) as raised:
        call(session, "tool", "search", {"query": "q"})
    assert raised.value.error_type == "httpx.ReadTimeout"
    assert raised.value.error_message == "timed out"
    replayed = session.finish()
    assert replayed.calls()[0].status == "error" and replayed.calls()[0].error == info
    assert session.report().calls_served == 1


def test_recorded_cancellation_is_reraised() -> None:
    b = FixtureBuilder()
    b.tool("slow", {}, status="cancelled")
    session = ReplaySession(b.build())
    with pytest.raises(asyncio.CancelledError):
        call(session, "tool", "slow")


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (Payload.not_captured(), "was not captured"),
        (Payload(state="redacted"), "was redacted"),
        (Payload(state="hash_only", sha256="sha256:abc"), "was stored as a hash only"),
        (Payload.captured("partial", truncated=True, size_bytes=999999), "was truncated"),
    ],
)
def test_response_unavailable(payload: Payload, reason: str) -> None:
    b = FixtureBuilder()
    b.tool("search", {"query": "q"}, response_payload=payload)
    session = ReplaySession(b.build())
    with pytest.raises(ResponseNotCapturedError) as raised:
        call(session, "tool", "search", {"query": "q"})
    assert reason in str(raised.value)
    session.finish()
    report = session.report()
    div = report.first_divergence
    assert div is not None and div.kind == "response_unavailable"
    assert div.label == "tool:search" and div.position == 0
    assert "overrides={'tool:search': ...}" in div.message
    assert report.unconsumed == []


# --------------------------------------------------------------- run level
def test_output_match_and_mismatch() -> None:
    same = ReplaySession(weather_fixture())
    replay_all(same)
    same.finish(output={"answer": "It is sunny."})
    assert same.report().ok

    diff = ReplaySession(weather_fixture())
    replay_all(diff)
    diff.finish(output={"answer": "It is rainy."})
    div = diff.report().first_divergence
    assert div is not None and div.kind == "output_mismatch" and div.path == "$.answer"
    assert (
        div.message
        == 'Run output diverged at $.answer: expected "It is sunny.", got "It is rainy."'
    )


def test_output_not_compared_when_not_set() -> None:
    session = ReplaySession(weather_fixture())
    replay_all(session)
    session.finish()
    assert session.report().ok


def test_status_mismatch_replay_failed() -> None:
    session = ReplaySession(weather_fixture())
    replay_all(session)
    session.finish(error=ValueError("bad parse"))
    div = session.report().first_divergence
    assert div is not None and div.kind == "status_mismatch"
    assert div.expected == "ok" and div.actual == "error"
    assert "builtins.ValueError: bad parse" in div.message


def test_status_mismatch_recording_failed_but_replay_succeeds() -> None:
    b = FixtureBuilder()
    b.tool("t", {}, 1)
    fixture = b.build(status="error", error=ErrorInfo(type="builtins.ValueError", message="x"))
    session = ReplaySession(fixture)
    call(session, "tool", "t")
    session.finish()
    div = session.report().first_divergence
    assert div is not None and div.kind == "status_mismatch" and div.actual == "ok"


def test_error_mismatch() -> None:
    b = FixtureBuilder()
    fixture = b.build(status="error", error=ErrorInfo(type="builtins.ValueError", message="x"))
    session = ReplaySession(fixture)
    session.finish(error=KeyError("y"))
    div = session.report().first_divergence
    assert div is not None and div.kind == "error_mismatch"
    assert "builtins.ValueError" in div.message and "builtins.KeyError" in div.message


def test_same_recorded_error_propagating_is_not_a_divergence() -> None:
    b = FixtureBuilder()
    info = ErrorInfo(type="httpx.ReadTimeout", message="timed out")
    b.tool("search", {}, status="error", error=info)
    fixture = b.build(status="error", error=info)
    session = ReplaySession(fixture)
    with pytest.raises(RecordedCallError) as raised:
        call(session, "tool", "search")
    session.finish(error=raised.value)
    assert session.report().ok


def test_own_unmatched_error_does_not_add_status_mismatch() -> None:
    session = ReplaySession(weather_fixture(), allow_unconsumed=True)
    with pytest.raises(UnmatchedCallError) as raised:
        call(session, "tool", "nope")
    session.finish(error=raised.value)
    assert [d.kind for d in session.report().divergences] == ["name_mismatch"]


# --------------------------------------------------------------- lifecycle
def test_finish_twice_and_call_after_finish() -> None:
    session = ReplaySession(weather_fixture(), allow_unconsumed=True)
    session.finish()
    with pytest.raises(ReplayError, match="already called"):
        session.finish()
    with pytest.raises(ReplayError, match="after the replay session finished"):
        call(session, "tool", "search", {"query": "weather in Paris"})


def test_report_requires_finish() -> None:
    with pytest.raises(ReplayError):
        ReplaySession(weather_fixture()).report()


def test_invalid_mode_and_order() -> None:
    with pytest.raises(ValueError, match="mode"):
        ReplaySession(weather_fixture(), mode="loose")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="order"):
        ReplaySession(weather_fixture(), order="random")  # type: ignore[arg-type]


def test_wall_clock_limit_checked_on_each_call() -> None:
    now = [0.0]
    session = ReplaySession(
        weather_fixture(), limits=Limits(max_replay_seconds=5.0), clock=lambda: now[0]
    )
    call(session, "tool", "search", {"query": "weather in Paris"})
    now[0] = 6.0
    with pytest.raises(ReplayError, match=r"wall-clock limit of 5s .* at model:gpt"):
        call(session, "model", "gpt", {"prompt": "summarize: sunny"})


def test_span_records_status_and_nesting() -> None:
    session = ReplaySession(weather_fixture(), allow_unconsumed=True)
    with (
        use_session(session),
        session.span(CallSpec(kind="span", name="outer", attributes={"step": 1})) as outer,
    ):
        with session.span(CallSpec(kind="span", name="inner")) as inner:
            call(session, "tool", "search", {"query": "weather in Paris"})
        with pytest.raises(RuntimeError), session.span(CallSpec(kind="span", name="bad")):
            raise RuntimeError("boom")
    replayed = session.finish()
    by_name = {e.name: e for e in replayed.events}
    assert by_name["inner"].parent_id == outer
    assert by_name["search"].parent_id == inner
    assert by_name["outer"].attributes == {"step": 1} and by_name["outer"].ended_at is not None
    assert by_name["bad"].status == "error" and by_name["bad"].error is not None


def test_span_kind_is_not_a_call() -> None:
    session = ReplaySession(weather_fixture())
    with pytest.raises(ValueError, match="spans are not calls"):
        call(session, "span", "x")


# --------------------------------------------------------------- redaction
def test_requests_are_redacted_before_computing_call_key() -> None:
    redactor = default_redactor()
    _, paths = redactor.apply({"api_key": "sk-live-1234567890abcdefghij", "q": "x"})
    if not paths:
        pytest.skip("default redaction policy does not redact api_key")
    b = FixtureBuilder()
    b.tool("search", {"api_key": "sk-live-1234567890abcdefghij", "q": "x"}, ["ok"])
    session = ReplaySession(b.build())
    # A different secret redacts to the same placeholder, so the call still matches.
    assert call(
        session, "tool", "search", {"api_key": "sk-other-0987654321zyxwvutsrq", "q": "x"}
    ) == ["ok"]
    replayed = session.finish()
    assert "sk-other" not in replayed.model_dump_json()


class _UpperRedactor:
    policy_id = "upper"

    def apply(self, value: Any) -> tuple[Any, list[str]]:
        if isinstance(value, dict) and "token" in value:
            return {**value, "token": "[REDACTED]"}, ["$.token"]
        return value, []


def test_custom_redactor_and_policy_mismatch_note() -> None:
    redactor = _UpperRedactor()
    b = FixtureBuilder(redactor=redactor)
    b.tool("auth", {"token": "abc", "user": "u1"}, True)
    fixture = b.build()
    session = ReplaySession(fixture, redaction=redactor)
    assert call(session, "tool", "auth", {"token": "zzz", "user": "u1"}) is True
    replayed = session.finish()
    assert replayed.redaction.policy_id == "upper"
    assert "$.token" in replayed.redaction.fields_omitted
    # The recording says policy "default", replay uses "upper": the mismatch message says so.
    other = ReplaySession(fixture, redaction=redactor)
    with pytest.raises(UnmatchedCallError) as info:
        call(other, "tool", "auth", {"token": "zzz", "user": "u2"})
    assert "redaction policy 'default'" in info.value.divergence.message


def test_value_helpers_skip_redaction() -> None:
    req, _, key = normalize("value", "now", {"hint": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"})
    assert req == {"hint": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}
    assert key.startswith("sha256:")


def test_default_redactor_falls_back_without_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> Any:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(session_mod.importlib, "import_module", missing)
    redactor = default_redactor()
    assert redactor.apply({"password": "x"}) == ({"password": "x"}, [])
    session = ReplaySession(weather_fixture(), redaction=None)
    replayed = session.finish()
    assert any("traceon.sdk.policy is not available" in w for w in replayed.warnings)


def test_default_redactor_uses_sdk_policy_module(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("traceon.sdk.policy")

    class RedactionPolicy:
        policy_id = "fake"

        def apply(self, value: Any) -> tuple[Any, list[str]]:
            return "[ALL]", ["$"]

    fake.RedactionPolicy = RedactionPolicy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "traceon.sdk.policy", fake)
    assert default_redactor().apply({"a": 1}) == ("[ALL]", ["$"])


def test_unrelated_import_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(name: str) -> Any:
        raise ModuleNotFoundError("No module named 'some_dependency'", name="some_dependency")

    monkeypatch.setattr(session_mod.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        default_redactor()


# ------------------------------------------------------------------- async
async def test_async_acall_serves_and_awaits_overrides() -> None:
    async def fake_send(to: str) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"sent": "fake", "to": to}

    session = ReplaySession(weather_fixture(), overrides={"send_email": fake_send})
    assert await acall(session, "tool", "search", {"query": "weather in Paris"}) == ["sunny", "22C"]
    assert await acall(session, "model", "gpt", {"prompt": "summarize: sunny"}) == {
        "text": "It is sunny."
    }
    assert await acall(session, "tool", "send_email", {"to": "a@example.com"}) == {
        "sent": "fake",
        "to": "a@example.com",
    }
    session.finish()
    assert session.report().ok


async def test_asyncio_gather_with_by_key() -> None:
    b = FixtureBuilder()
    for city in ("Paris", "Rome", "Oslo"):
        b.tool("weather", {"city": city}, f"{city}: sunny")
    session = ReplaySession(b.build(), order="by_key")

    async def ask(city: str) -> Any:
        await asyncio.sleep(0.001 * (3 - len(city) % 3))
        return await acall(session, "tool", "weather", {"city": city})

    with use_session(session):
        results = await asyncio.gather(ask("Oslo"), ask("Paris"), ask("Rome"))
    assert results == ["Oslo: sunny", "Paris: sunny", "Rome: sunny"]
    session.finish()
    assert session.report().ok


async def test_asyncio_gather_strict_order_detects_reorder() -> None:
    b = FixtureBuilder()
    b.tool("weather", {"city": "Paris"}, "p")
    b.tool("weather", {"city": "Rome"}, "r")
    session = ReplaySession(b.build())
    results = await asyncio.gather(
        acall(session, "tool", "weather", {"city": "Rome"}),
        acall(session, "tool", "weather", {"city": "Paris"}),
        return_exceptions=True,
    )
    assert isinstance(results[0], UnmatchedCallError)
    assert 'order="by_key"' in results[0].divergence.message


async def test_async_override_cancellation_is_recorded() -> None:
    started = asyncio.Event()

    async def slow(query: str) -> Any:
        started.set()
        await asyncio.sleep(10)

    session = ReplaySession(weather_fixture(), overrides={"search": slow}, allow_unconsumed=True)
    task = asyncio.ensure_future(acall(session, "tool", "search", {"query": "weather in Paris"}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    replayed = session.finish()
    assert replayed.calls()[0].status == "cancelled"


def test_thread_safety_of_sequence_allocation() -> None:
    b = FixtureBuilder()
    for i in range(50):
        b.value("random", i / 100)
    session = ReplaySession(b.build())
    barrier = threading.Barrier(5)
    out: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        for _ in range(10):
            value = call(session, "value", "random")
            with lock:
                out.append(value)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(out) == [i / 100 for i in range(50)]
    replayed = session.finish()
    assert len({e.sequence for e in replayed.events}) == 50
