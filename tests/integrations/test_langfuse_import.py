"""Tests for converting Langfuse trace JSON into Traceon fixtures (no network)."""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from traceon.core.canonical import call_key, canonical_json, canonicalize
from traceon.core.errors import FixtureError
from traceon.core.io import dump_fixture, parse_fixture
from traceon.core.limits import Limits
from traceon.core.schema import Fixture
from traceon.integrations.langfuse import import_trace, observation_kind

DATA = Path(__file__).parent / "data"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATA / name).read_text(encoding="utf-8"))
    return data


@pytest.fixture
def trace() -> dict[str, Any]:
    return load("langfuse_trace.json")


@pytest.fixture
def fixture(trace: dict[str, Any]) -> Fixture:
    return import_trace(trace)


class NoRedaction:
    policy_id = "none"
    rules: tuple[str, ...] = ()

    def apply(self, value: Any) -> tuple[Any, list[str]]:
        return value, []


def minimal(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "t-1",
        "timestamp": "2026-10-01T10:00:00.000Z",
        "name": "minimal",
        "observations": [],
    }
    data.update(extra)
    return data


def obs(oid: str, start: str = "2026-10-01T10:00:01.000Z", **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"id": oid, "type": "SPAN", "name": oid, "startTime": start}
    data.update(extra)
    return data


# --------------------------------------------------------------------------- mapping


@pytest.mark.parametrize(
    ("obs_type", "kind"),
    [
        ("GENERATION", "model"),
        ("EMBEDDING", "model"),
        ("TOOL", "tool"),
        ("RETRIEVER", "tool"),
        ("SPAN", "span"),
        ("EVENT", "span"),
        ("AGENT", "span"),
        ("CHAIN", "span"),
        ("EVALUATOR", "span"),
        ("GUARDRAIL", "span"),
        ("SOMETHING_NEW", "span"),
        ("generation", "model"),
        (None, "span"),
    ],
)
def test_observation_kind(obs_type: str | None, kind: str) -> None:
    assert observation_kind(obs_type) == kind


def test_fixture_identity(fixture: Fixture) -> None:
    assert fixture.source == "langfuse"
    assert fixture.mode == "record"
    assert fixture.name == "research-agent"
    assert fixture.fixture_id == "lf_7f3c2a9e4b1d4c0e9a8b6d5c4e3f2a1b"
    assert fixture.run_id == "7f3c2a9e4b1d4c0e9a8b6d5c4e3f2a1b"
    assert fixture.created_at == dt.datetime(2026, 10, 28, 9, 14, 2, 118000, tzinfo=dt.timezone.utc)
    assert fixture.environment.traceon_version


def test_events_ordered_by_start_time_with_root_span(fixture: Fixture) -> None:
    names = [e.name for e in fixture.events]
    assert names == [
        "research-agent",
        "research_agent",
        "plan",
        "search",
        "fetch_document",
        "vector_lookup",
        "embed_query",
        "summarize",
        "pii_check",
        "answer_ready",
    ]
    assert [e.sequence for e in fixture.events] == list(range(len(fixture.events)))
    root = fixture.events[0]
    assert root.kind == "span" and root.parent_id is None
    assert root.ended_at == dt.datetime(2026, 10, 28, 9, 14, 5, 600000, tzinfo=dt.timezone.utc)
    assert root.duration_ms == pytest.approx(3482.0)


def test_kinds_and_parents(fixture: Fixture) -> None:
    by_name = {e.name: e for e in fixture.events}
    assert by_name["research_agent"].parent_id == "trace"
    assert by_name["plan"].parent_id == "obs-agent"
    assert by_name["embed_query"].parent_id == "obs-retrieve"
    assert {e.name for e in fixture.calls()} == {
        "plan",
        "search",
        "fetch_document",
        "vector_lookup",
        "embed_query",
        "summarize",
    }
    assert by_name["plan"].kind == "model"
    assert by_name["vector_lookup"].kind == "tool"
    assert by_name["pii_check"].kind == "span"
    assert by_name["pii_check"].call_key is None


def test_payloads_and_call_keys(fixture: Fixture) -> None:
    search = next(e for e in fixture.events if e.name == "search")
    assert search.request.state == "captured"
    assert search.request.value == {"query": "refund policy Q3", "limit": 3}
    assert search.response.value[0]["id"] == "doc-17"
    assert search.request.sha256 and search.request.sha256.startswith("sha256:")
    assert search.call_key == call_key("tool", "search", {"query": "refund policy Q3", "limit": 3})
    # A null output means Langfuse did not capture one.
    fetch = next(e for e in fixture.events if e.name == "fetch_document")
    assert fetch.response.state == "not_captured"
    event = next(e for e in fixture.events if e.name == "answer_ready")
    assert event.request.state == "not_captured"
    assert event.ended_at is None and event.duration_ms is None


def test_usage_and_cost(fixture: Fixture) -> None:
    by_name = {e.name: e for e in fixture.events}
    plan = by_name["plan"].usage
    assert plan is not None
    assert (plan.input_tokens, plan.output_tokens, plan.total_tokens) == (182, 14, 196)
    assert plan.cost is not None
    assert plan.cost.amount == pytest.approx(0.0000357)
    assert plan.cost.kind == "estimated"
    assert plan.cost.price_source == "langfuse"
    assert plan.cost.currency == "USD"
    summarize = by_name["summarize"]
    assert summarize.usage is not None and summarize.usage.total_tokens == 437
    assert summarize.attributes["usage_details"]["input_cached_tokens"] == 128
    embed = by_name["embed_query"].usage
    assert embed is not None and embed.output_tokens is None and embed.input_tokens == 3
    # Zero usage reported on non-model observations is dropped.
    assert by_name["research_agent"].usage is None
    assert by_name["search"].usage is None


def test_attributes(fixture: Fixture) -> None:
    plan = next(e for e in fixture.events if e.name == "plan")
    assert plan.attributes["langfuse.type"] == "GENERATION"
    assert plan.attributes["langfuse.id"] == "obs-gen-1"
    assert plan.attributes["model"] == "gpt-4o-mini-2024-07-18"
    assert plan.attributes["model_parameters"] == {"temperature": 0, "max_tokens": 256}
    assert plan.attributes["prompt_name"] == "planner"
    assert plan.attributes["prompt_version"] == 3
    assert plan.attributes["time_to_first_token_s"] == pytest.approx(0.339)
    assert plan.attributes["metadata"] == {"ls_provider": "openai"}
    guard = next(e for e in fixture.events if e.name == "pii_check")
    assert guard.status == "ok"
    assert guard.attributes["level"] == "WARNING"
    assert guard.attributes["status_message"] == "slow guardrail"


def test_error_level_maps_to_error_status_with_redacted_message(fixture: Fixture) -> None:
    fetch = next(e for e in fixture.events if e.name == "fetch_document")
    assert fetch.status == "error"
    assert fetch.error is not None
    assert fetch.error.type == "langfuse.ObservationError"
    assert "KeyError" in fetch.error.message
    assert "sk-proj-" not in fetch.error.message
    assert "[REDACTED:" in fetch.error.message
    # A nested failure does not fail the run.
    assert fixture.status == "ok"
    assert fixture.error is None


def test_top_level_error_fails_the_run() -> None:
    data = minimal(observations=[obs("a", level="ERROR", statusMessage="boom")])
    fx = import_trace(data)
    assert fx.status == "error"
    assert fx.error is not None and fx.error.message == "boom"


def test_error_without_status_message() -> None:
    fx = import_trace(minimal(observations=[obs("a", level="ERROR")]))
    assert fx.event("a").error is not None
    assert fx.event("a").error.message == "observation level ERROR"  # type: ignore[union-attr]


def test_redaction_of_inputs_and_metadata(fixture: Fixture) -> None:
    text = dump_fixture(fixture)
    assert "lf-demo-should-never-leak" not in text
    assert "abcdefghijklmnop123" not in text
    assert "sk-proj-abc123" not in text
    assert fixture.input.value["api_key"] == "[REDACTED:sensitive_key]"
    assert fixture.input.redacted_paths == ["$.api_key"]
    assert fixture.redaction.policy_id == "default"
    assert "sensitive_key" in fixture.redaction.rules
    assert "$.api_key" in fixture.redaction.fields_omitted
    assert "$.metadata.langfuse.metadata.authorization" in fixture.redaction.fields_omitted
    assert "$.error.message" in fixture.redaction.fields_omitted


def test_trace_metadata(fixture: Fixture) -> None:
    info = fixture.metadata["langfuse"]
    assert info["trace_id"] == "7f3c2a9e4b1d4c0e9a8b6d5c4e3f2a1b"
    assert info["session_id"] == "sess-2026-10-28-a"
    assert info["user_id"] == "user-4821"
    assert info["tags"] == ["research", "staging"]
    assert info["release"] == "agent@1.4.2"
    assert info["latency_s"] == pytest.approx(3.482)
    assert info["metadata"]["deployment"] == "staging"
    assert "external_id" not in info  # null fields are skipped


def test_unmapped_fields_are_reported(fixture: Fixture) -> None:
    joined = "\n".join(fixture.warnings)
    assert "scores" in joined
    assert "experimentalField (1)" in joined
    assert "createdAt" in joined


def test_unknown_trace_field_is_reported() -> None:
    fx = import_trace(minimal(brandNewField={"a": 1}, scores=[]))
    assert any("brandNewField" in w for w in fx.warnings)
    assert not any("scores" in w for w in fx.warnings)


def test_round_trips_through_fixture_io(fixture: Fixture) -> None:
    again = parse_fixture(dump_fixture(fixture))
    assert again == fixture


def test_import_is_deterministic(trace: dict[str, Any]) -> None:
    assert dump_fixture(import_trace(copy.deepcopy(trace))) == dump_fixture(import_trace(trace))


def test_input_is_not_mutated(trace: dict[str, Any]) -> None:
    before = copy.deepcopy(trace)
    import_trace(trace)
    assert trace == before


# --------------------------------------------------------------------------- v2 / shapes


def test_v2_observations_response() -> None:
    fx = import_trace(load("langfuse_observations_v2.json"))
    assert fx.name == "ticket-triage"
    assert fx.run_id == "0af7651916cd43dd8448eb211c80319c"
    assert [e.kind for e in fx.events] == ["span", "span", "model"]
    assert fx.events[2].parent_id == "a1b2c3d4e5f60718"
    # Nanosecond timestamps are accepted (truncated to microseconds).
    assert fx.events[1].started_at.microsecond == 123
    assert fx.metadata["langfuse"]["tags"] == ["prod"]
    assert fx.metadata["langfuse"]["release"] == "triage@2.0.0"
    assert any("without a trace object" in w for w in fx.warnings)
    usage = fx.events[2].usage
    assert usage is not None and usage.cost is not None
    assert usage.cost.amount == pytest.approx(0.000067)


def test_trace_without_observations_key() -> None:
    data = minimal()
    del data["observations"]
    fx = import_trace(data)
    assert len(fx.events) == 1
    assert any("no 'observations'" in w for w in fx.warnings)


def test_observations_null_is_empty() -> None:
    assert len(import_trace(minimal(observations=None)).events) == 1


def test_rejects_non_mapping() -> None:
    with pytest.raises(FixtureError, match="JSON object"):
        import_trace([1, 2])  # type: ignore[arg-type]


def test_rejects_non_list_observations() -> None:
    with pytest.raises(FixtureError, match="must be a list"):
        import_trace(minimal(observations={"a": 1}))


def test_rejects_trace_without_id() -> None:
    data = minimal()
    del data["id"]
    with pytest.raises(FixtureError, match="no 'id'"):
        import_trace(data)


def test_trace_id_falls_back_to_observation_trace_id() -> None:
    data = minimal(observations=[obs("a", traceId="from-obs")])
    del data["id"]
    assert import_trace(data).run_id == "from-obs"


def test_rejects_trace_without_any_timestamp() -> None:
    data = minimal(observations=[{"id": "a", "type": "SPAN"}])
    del data["timestamp"]
    with pytest.raises(FixtureError, match="timestamp"):
        import_trace(data)


def test_timestamp_falls_back_to_first_observation() -> None:
    data = minimal(observations=[obs("a", start="2026-10-01T09:00:00Z")])
    del data["timestamp"]
    fx = import_trace(data)
    assert fx.created_at == dt.datetime(2026, 10, 1, 9, tzinfo=dt.timezone.utc)


def test_too_many_observations() -> None:
    data = minimal(observations=[obs(f"o{i}") for i in range(5)])
    with pytest.raises(FixtureError, match="limit"):
        import_trace(data, limits=Limits(max_events=3))


# --------------------------------------------------------------------------- robustness


def test_missing_ids_names_and_types() -> None:
    data = minimal(observations=[{"startTime": "2026-10-01T10:00:01Z"}])
    fx = import_trace(data)
    ev = fx.events[1]
    assert ev.event_id == "observation-0"
    assert ev.name == "span"
    assert any("no 'id'" in w for w in fx.warnings)


def test_skips_non_object_observations() -> None:
    fx = import_trace(minimal(observations=["oops", obs("a")]))
    assert [e.event_id for e in fx.events] == ["trace", "a"]
    assert any("observations[0]" in w for w in fx.warnings)


def test_unknown_parent_is_attached_to_root() -> None:
    fx = import_trace(minimal(observations=[obs("a", parentObservationId="ghost")]))
    assert fx.event("a").parent_id == "trace"
    assert any("ghost" in w for w in fx.warnings)


def test_self_parent_and_cycles_are_broken() -> None:
    data = minimal(
        observations=[
            obs("self", parentObservationId="self"),
            obs("a", parentObservationId="b"),
            obs("b", parentObservationId="a"),
        ]
    )
    fx = import_trace(data)
    assert fx.event("self").parent_id == "trace"
    parents = {fx.event("a").parent_id, fx.event("b").parent_id}
    assert "trace" in parents
    assert any("cycle" in w for w in fx.warnings)


def test_deep_nesting_is_capped() -> None:
    chain = [obs("n0")] + [obs(f"n{i}", parentObservationId=f"n{i - 1}") for i in range(1, 12)]
    fx = import_trace(minimal(observations=chain), limits=Limits(max_span_depth=5))
    assert any("nested deeper" in w for w in fx.warnings)
    for ev in fx.events:
        depth, cur = 0, ev
        while cur.parent_id is not None:
            depth += 1
            cur = fx.event(cur.parent_id)
        assert depth <= 5


def test_deep_but_valid_chain_is_kept() -> None:
    chain = [obs("n0")] + [obs(f"n{i}", parentObservationId=f"n{i - 1}") for i in range(1, 100)]
    fx = import_trace(minimal(observations=chain))
    assert fx.event("n99").parent_id == "n98"
    assert not any("nested deeper" in w for w in fx.warnings)


def test_duplicate_and_reserved_ids_are_renamed() -> None:
    data = minimal(
        observations=[obs("dup"), obs("dup", start="2026-10-01T10:00:02Z"), obs("trace")]
    )
    fx = import_trace(data)
    ids = [e.event_id for e in fx.events]
    assert len(ids) == len(set(ids)) == 4
    assert "dup~2" in ids and "trace~2" in ids


def test_unsafe_ids_are_sanitized() -> None:
    fx = import_trace(minimal(id="../../etc/passwd", observations=[obs("a/b c")]))
    assert fx.fixture_id == "lf_.._.._etc_passwd"
    assert fx.events[1].event_id == "a_b_c"
    long_id = "x" * 500
    assert len(import_trace(minimal(id=long_id)).run_id) == 32


def test_bad_and_naive_timestamps() -> None:
    data = minimal(
        observations=[
            obs("a", start="not-a-date"),
            obs("b", start="2026-10-01T10:00:05", endTime="2026-10-01T10:00:04"),
            obs("c", start="2026-10-01T10:00:06+02:00", endTime=12345),
        ]
    )
    fx = import_trace(data)
    joined = "\n".join(fx.warnings)
    assert "unparseable timestamp" in joined
    assert "missing startTime" in joined
    assert "interpreted as UTC" in joined
    assert "endTime is before startTime" in joined
    assert fx.event("b").duration_ms is None
    assert fx.event("b").started_at.tzinfo is not None


def test_long_names_are_truncated() -> None:
    fx = import_trace(minimal(name="n" * 600, observations=[obs("a", name="m" * 700)]))
    assert len(fx.name) == 512
    assert len(fx.event("a").name) == 512
    assert sum("truncated" in w for w in fx.warnings) == 2


def test_numeric_ids_and_names_are_accepted() -> None:
    fx = import_trace(minimal(id=12345, observations=[obs("a", name=7)]))
    assert fx.run_id == "12345"
    assert fx.event("a").name == "7"


# --------------------------------------------------------------------------- usage variants


def test_legacy_usage_and_openai_style_keys() -> None:
    data = minimal(
        observations=[
            obs(
                "legacy",
                type="GENERATION",
                usage={"input": 10, "output": 5, "unit": "TOKENS", "totalCost": 0.5},
            ),
            obs(
                "openai",
                type="GENERATION",
                usageDetails={"prompt_tokens": 7, "completion_tokens": 3},
            ),
            obs("flat", type="GENERATION", promptTokens=4, completionTokens=1, totalTokens=5),
            obs(
                "chars", type="GENERATION", usage={"input": 100, "output": 20, "unit": "CHARACTERS"}
            ),
            obs("bad", type="GENERATION", usageDetails={"input": -1, "output": True, "total": "9"}),
        ]
    )
    fx = import_trace(data)
    legacy = fx.event("legacy").usage
    assert legacy is not None and legacy.total_tokens == 15 and legacy.cost is not None
    assert legacy.cost.amount == 0.5
    openai = fx.event("openai").usage
    assert openai is not None and (
        openai.input_tokens,
        openai.output_tokens,
        openai.total_tokens,
    ) == (7, 3, 10)
    flat = fx.event("flat").usage
    assert flat is not None and flat.total_tokens == 5
    assert fx.event("chars").usage is None
    assert any("CHARACTERS" in w for w in fx.warnings)
    assert fx.event("bad").usage is None


def test_cost_from_total_cost_and_summed_details() -> None:
    data = minimal(
        observations=[
            obs("total", type="GENERATION", totalCost=0.25),
            obs("calc", type="GENERATION", calculatedTotalCost="0.125"),
            obs("sum", type="GENERATION", costDetails={"input": 0.1, "output": 0.2}),
        ]
    )
    fx = import_trace(data)
    amounts = {eid: fx.event(eid).usage.cost.amount for eid in ("total", "calc", "sum")}  # type: ignore[union-attr]
    assert amounts["total"] == 0.25
    assert amounts["calc"] == 0.125
    assert amounts["sum"] == pytest.approx(0.3)


def test_tool_with_real_usage_is_kept() -> None:
    fx = import_trace(minimal(observations=[obs("t", type="TOOL", costDetails={"total": 0.01})]))
    usage = fx.event("t").usage
    assert usage is not None and usage.cost is not None and usage.cost.amount == 0.01


# --------------------------------------------------------------------------- capture options


def test_capture_io_false_keeps_call_keys() -> None:
    data = minimal(
        input={"q": 1}, observations=[obs("t", type="TOOL", input={"x": 1}, output={"y": 2})]
    )
    fx = import_trace(data, capture_io=False)
    ev = fx.event("t")
    assert ev.request.state == "not_captured" and ev.response.state == "not_captured"
    assert ev.call_key == call_key("tool", "t", {"x": 1})
    assert fx.input.state == "not_captured"


def test_large_payload_is_truncated() -> None:
    big = {"text": "a" * 5000}
    fx = import_trace(
        minimal(observations=[obs("t", type="TOOL", output=big)]),
        limits=Limits(max_payload_bytes=300),
    )
    resp = fx.event("t").response
    assert resp.state == "captured" and resp.truncated
    assert resp.size_bytes is not None and resp.size_bytes > 5000
    assert "$truncated" in resp.value
    assert len(canonical_json(resp.value).encode()) <= 300
    assert not resp.is_servable


def test_fully_redacted_string_payload() -> None:
    fx = import_trace(
        minimal(
            observations=[obs("t", type="TOOL", input="sk-ant-api03-abcdefghijklmnopqrstuvwxyz")]
        )
    )
    req = fx.event("t").request
    assert req.state == "redacted"
    assert req.value is None
    assert req.redacted_paths == ["$"]


def test_custom_redactor() -> None:
    data = minimal(observations=[obs("t", type="TOOL", input={"api_key": "keep-me"})])
    fx = import_trace(data, redaction=NoRedaction())
    assert fx.event("t").request.value == {"api_key": "keep-me"}
    assert fx.redaction.policy_id == "none"
    assert fx.redaction.fields_omitted == []


def test_redactor_without_rule_metadata() -> None:
    class Minimal:
        policy_id = "mini"
        key_patterns = ("password",)
        value_patterns = (("token", r"tok_[a-z]+"),)

        def apply(self, value: Any) -> tuple[Any, list[str]]:
            return value, []

    fx = import_trace(minimal(), redaction=Minimal())
    assert fx.redaction.rules == ["key:password", "token"]


def test_non_json_native_values_are_canonicalized() -> None:
    data = minimal(observations=[obs("t", type="TOOL", input={"n": float("nan")})])
    fx = import_trace(data, redaction=NoRedaction())
    assert fx.event("t").request.value == canonicalize({"n": float("nan")})
