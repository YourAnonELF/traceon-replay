"""Build the two sample fixtures used by the Studio tests and the demo recording.

Run from the repository root::

    PYTHONPATH=src python tests/studio/make_sample_fixtures.py

It writes ``tests/studio/data/research-agent.baseline.json`` and
``tests/studio/data/research-agent.candidate.json``. The candidate run is the
same research agent after a prompt/config change: its ``web_search`` call asks
for ``recency_days=365`` instead of ``30``, the flaky fetch no longer times out
(so the retry disappears), and the final answer differs.

The output is fully deterministic (fixed ids and timestamps), so regenerating
the files produces no diff unless this script changes.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from traceon.core.canonical import call_key, canonical_json, canonicalize, content_hash
from traceon.core.io import save_fixture
from traceon.core.schema import (
    CALL_KINDS,
    Cost,
    Environment,
    ErrorInfo,
    Event,
    EventKind,
    Fixture,
    Payload,
    RedactionInfo,
    Status,
    Usage,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
T0 = dt.datetime(2026, 10, 28, 14, 3, 11, 120000, tzinfo=dt.timezone.utc)
PRICE_SOURCE = "anthropic.com/pricing (2026-10-01)"


def captured(value: Any, *, redacted_paths: list[str] | None = None) -> Payload:
    canon = canonicalize(value)
    return Payload.captured(
        canon,
        sha256=content_hash(canon),
        size_bytes=len(canonical_json(canon).encode("utf-8")),
        redacted_paths=redacted_paths,
    )


def hash_only(value: Any) -> Payload:
    canon = canonicalize(value)
    return Payload(
        state="hash_only",
        sha256=content_hash(canon),
        size_bytes=len(canonical_json(canon).encode("utf-8")),
    )


def truncated(value: Any, full: Any) -> Payload:
    full_canon = canonicalize(full)
    return Payload.captured(
        canonicalize(value),
        sha256=content_hash(full_canon),
        size_bytes=len(canonical_json(full_canon).encode("utf-8")),
        truncated=True,
    )


@dataclass
class Builder:
    """Accumulates events with sequential ids and offsets relative to ``T0``."""

    prefix: str
    events: list[Event] = field(default_factory=list)

    def add(
        self,
        kind: EventKind,
        name: str,
        start_ms: float,
        dur_ms: float,
        *,
        parent: str | None,
        request: Payload | None = None,
        response: Payload | None = None,
        raw_request: Any = None,
        status: Status = "ok",
        error: ErrorInfo | None = None,
        usage: Usage | None = None,
        side_effect: bool = False,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        seq = len(self.events)
        event_id = f"ev_{self.prefix}{seq:08x}"
        started = T0 + dt.timedelta(milliseconds=start_ms)
        key = None
        if kind in CALL_KINDS:
            key = call_key(kind, name, canonicalize(raw_request if raw_request is not None else {}))
        self.events.append(
            Event(
                event_id=event_id,
                parent_id=parent,
                sequence=seq,
                kind=kind,
                name=name,
                started_at=started,
                ended_at=started + dt.timedelta(milliseconds=dur_ms),
                duration_ms=dur_ms,
                status=status,
                side_effect=side_effect,
                call_key=key,
                request=request or Payload.not_captured(),
                response=response or Payload.not_captured(),
                error=error,
                usage=usage,
                attributes=attributes or {},
            )
        )
        return event_id

    def resize(self, event_id: str, dur_ms: float) -> None:
        """Set a span's duration once its children are known."""
        _resize(self.events, event_id, dur_ms)


def _resize(events: list[Event], event_id: str, dur_ms: float) -> None:
    for n, ev in enumerate(events):
        if ev.event_id == event_id:
            dur_ms = round(dur_ms, 3)
            events[n] = ev.model_copy(
                update={
                    "duration_ms": dur_ms,
                    "ended_at": ev.started_at + dt.timedelta(milliseconds=dur_ms),
                }
            )
            return
    raise KeyError(event_id)


def model_usage(inp: int, out: int, *, measured: bool) -> Usage:
    # Claude Sonnet-class pricing: $3 / MTok in, $15 / MTok out.
    amount = round(inp * 3e-6 + out * 15e-6, 6)
    cost = (
        Cost(amount=amount, kind="measured")
        if measured
        else Cost(amount=amount, kind="estimated", price_source=PRICE_SOURCE)
    )
    return Usage(input_tokens=inp, output_tokens=out, total_tokens=inp + out, cost=cost)


SOURCES_RECENT = [
    {"url": "https://arxiv.org/abs/2410.01234", "title": "Replay-based debugging for LLM agents"},
    {"url": "https://example.org/blog/agent-flakiness", "title": "Why agent tests are flaky"},
    {"url": "https://docs.example.com/tracing/overview", "title": "Tracing overview"},
]
SOURCES_YEAR = [
    {"url": "https://arxiv.org/abs/2410.01234", "title": "Replay-based debugging for LLM agents"},
    {"url": "https://example.org/blog/agent-flakiness", "title": "Why agent tests are flaky"},
    {"url": "https://example.net/2025/record-replay", "title": "Record/replay, revisited (2025)"},
]


def build(*, candidate: bool) -> Fixture:
    b = Builder(prefix="c" if candidate else "b")
    question = "How do teams make LLM agent tests deterministic?"
    recency = 365 if candidate else 30
    sources = SOURCES_YEAR if candidate else SOURCES_RECENT

    root = b.add(
        "span",
        "research-agent",
        0,
        0,
        parent=None,
        attributes={"agent.version": "0.4.2" if not candidate else "0.4.3"},
    )
    b.add(
        "value",
        "now",
        1.2,
        0.02,
        parent=root,
        response=captured({"$datetime": "2026-10-28T14:03:11.121200+00:00"}),
    )
    run_id_req: dict[str, Any] = {}
    b.add(
        "value",
        "uuid4",
        1.4,
        0.01,
        parent=root,
        raw_request=run_id_req,
        response=captured("6f1c2a4e-93b7-4d0e-8b0a-55e2f9d1c3a8"),
    )

    plan = b.add("span", "plan", 3, 1490, parent=root)
    plan_prompt = {
        "model": "claude-sonnet-4-5",
        "system": "You are a research planner. Produce a numbered plan.",
        "messages": [{"role": "user", "content": question}],
        "max_tokens": 600,
    }
    b.add(
        "model",
        "planner",
        6,
        1472,
        parent=plan,
        raw_request=plan_prompt,
        request=hash_only(plan_prompt),
        response=captured(
            {
                "plan": [
                    "Search for recent work on record/replay for agents",
                    "Read the top sources",
                    "Summarize techniques and trade-offs",
                    "Draft an answer with citations",
                ],
                "stop_reason": "end_turn",
            }
        ),
        usage=model_usage(812, 164, measured=True),
        attributes={"provider": "anthropic", "temperature": 0.2},
    )

    # Span durations are set once their children are known (see resize below).
    gather = b.add("span", "gather", 1500, 0, parent=root)
    search_req = {
        "query": "deterministic replay LLM agent tests",
        "top_k": 3,
        "recency_days": recency,
    }
    b.add(
        "tool",
        "web_search",
        1504,
        612,
        parent=gather,
        raw_request=search_req,
        request=captured(search_req),
        response=captured(sources),
        attributes={"backend": "searx"},
    )
    t = 2120.0
    for i, src in enumerate(sources):
        req = {"url": src["url"], "timeout_s": 5}
        if i == 1 and not candidate:
            b.add(
                "tool",
                "fetch_url",
                t,
                5003,
                parent=gather,
                raw_request=req,
                request=captured(req),
                status="error",
                error=ErrorInfo(
                    type="builtins.TimeoutError", message=f"GET {src['url']} timed out after 5.0s"
                ),
                attributes={"attempt": 1},
            )
            t += 5006
            b.add(
                "tool",
                "fetch_url",
                t,
                284,
                parent=gather,
                raw_request=req,
                request=captured(req),
                response=truncated(
                    {
                        "status": 200,
                        "text": "Agent tests are flaky because model sampling, tool "
                        "latency and wall-clock values all vary between runs. Pinning seeds is not "
                        "enough when",
                    },
                    {"status": 200, "text": "x" * 310_000},
                ),
                attributes={"attempt": 2},
            )
            t += 288
            continue
        dur = 241.0 + 37 * i
        body = {
            "status": 200,
            "text": f"{src['title']}. Recording every external call and "
            f"serving it back during tests removes nondeterminism at the boundary.",
        }
        b.add(
            "tool",
            "fetch_url",
            t,
            dur,
            parent=gather,
            raw_request=req,
            request=captured(req),
            response=captured(body),
            attributes={"attempt": 1},
        )
        t += dur + 4
    upsert_req = {
        "collection": "research-notes",
        "documents": len(sources),
        "api_key": "[REDACTED:key:api_key]",
    }
    b.add(
        "tool",
        "vector_store.upsert",
        t + 2,
        96,
        parent=gather,
        raw_request=upsert_req,
        request=captured(upsert_req, redacted_paths=["$.api_key"]),
        response=captured({"upserted": len(sources)}),
        side_effect=True,
    )

    gather_end = t + 2 + 96 + 4
    b.resize(gather, gather_end - 1500)
    a0 = gather_end + 16
    analyze = b.add("span", "analyze", a0, 3310, parent=root)
    usages = [(2410, 288), (2195, 251), (2630, 302)]
    for i, src in enumerate(sources):
        prompt = {
            "model": "claude-sonnet-4-5",
            "messages": [
                {
                    "role": "user",
                    "content": f"Summarize for the question: {question}\n\n{src['title']}",
                }
            ],
        }
        inp, out = usages[i]
        b.add(
            "model",
            "summarize",
            a0 + 4 + i * 1010,
            1002,
            parent=analyze,
            raw_request=prompt,
            request=hash_only(prompt),
            response=captured(
                {
                    "summary": f"{src['title']}: capture tool and model I/O, then "
                    "replay it with network access blocked.",
                    "stop_reason": "end_turn",
                }
            ),
            usage=model_usage(inp, out, measured=True),
            attributes={"provider": "anthropic", "chunk": i},
        )
    cite_req = {"urls": [s["url"] for s in sources]}
    b.add(
        "tool",
        "extract_citations",
        a0 + 3040,
        31,
        parent=analyze,
        raw_request=cite_req,
        request=captured(cite_req),
        response=captured([{"n": i + 1, "url": s["url"]} for i, s in enumerate(sources)]),
    )
    b.add("value", "random", a0 + 3075, 0.01, parent=analyze, response=captured(0.4183))

    d0 = a0 + 3320
    draft = b.add("span", "draft", d0, 1940, parent=root)
    writer_prompt = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": "Write the answer with citations."}],
    }
    answer = (
        "Teams record every tool call, model call and nondeterministic value once, then replay "
        "the run offline with the network blocked [1][3]. Divergence checks pinpoint the first "
        "call whose arguments changed [2]."
    )
    if candidate:
        answer = (
            "Teams record tool and model I/O and replay it offline [1]. A 2025 survey argues "
            "record/replay should be combined with property checks [3]."
        )
    b.add(
        "model",
        "writer",
        d0 + 3,
        1702,
        parent=draft,
        raw_request=writer_prompt,
        request=hash_only(writer_prompt),
        response=captured({"text": answer, "stop_reason": "end_turn"}),
        usage=model_usage(3920, 412 if not candidate else 371, measured=False),
        attributes={"provider": "anthropic"},
    )
    calc_req = {"expression": "3 / 3 * 100"}
    b.add(
        "tool",
        "calculator",
        d0 + 1712,
        2,
        parent=draft,
        raw_request=calc_req,
        request=captured(calc_req),
        response=captured(100.0),
    )
    b.add(
        "value",
        "now",
        d0 + 1716,
        0.02,
        parent=draft,
        response=captured({"$datetime": "2026-10-28T14:03:21.612000+00:00"}),
    )

    r0 = d0 + 1950
    review = b.add("span", "review", r0, 1520, parent=root)
    critic_prompt = {
        "model": "claude-haiku-4-5",
        "messages": [{"role": "user", "content": "Check the answer for unsupported claims."}],
    }
    b.add(
        "model",
        "critic",
        r0 + 2,
        811,
        parent=review,
        raw_request=critic_prompt,
        request=Payload.not_captured(),
        response=captured({"verdict": "pass", "issues": []}),
        usage=model_usage(1240, 58, measured=False),
        attributes={"provider": "anthropic"},
    )
    save_req = {"path": "reports/agent-testing.md", "bytes": len(answer)}
    b.add(
        "tool",
        "save_report",
        r0 + 820,
        44,
        parent=review,
        raw_request=save_req,
        request=captured(save_req),
        response=captured({"written": True}),
        side_effect=True,
    )
    notify_req = {
        "to": "[REDACTED:email]",
        "subject": "Research report ready",
        "authorization": "[REDACTED:key:authorization]",
    }
    b.add(
        "tool",
        "notify",
        r0 + 870,
        612,
        parent=review,
        raw_request=notify_req,
        request=captured(notify_req, redacted_paths=["$.to", "$.authorization"]),
        response=Payload(state="redacted"),
        side_effect=True,
    )

    run_end = max(
        e.started_at + dt.timedelta(milliseconds=e.duration_ms or 0) for e in b.events[1:]
    )
    b.resize(root, (run_end - T0).total_seconds() * 1000 + 20)

    output = {"answer": answer, "citations": [s["url"] for s in sources]}
    return Fixture(
        fixture_id="fx_7d41c09a2be5" if not candidate else "fx_c3e88a1f6d20",
        run_id="run_2b9e4f10a7c3" if not candidate else "run_91d0b6e2c845",
        name="research-agent" if not candidate else "research-agent (prompt v2)",
        created_at=T0 if not candidate else T0 + dt.timedelta(days=4, hours=2, minutes=17),
        status="ok",
        environment=Environment(
            python="3.12.10",
            platform="Linux-6.8.0-x86_64",
            traceon_version="0.1.0",
            adapters={"anthropic": "0.69.0"},
        ),
        redaction=RedactionInfo(
            policy_id="default",
            rules=["key:api_key", "key:authorization", "email"],
            fields_omitted=["$.api_key", "$.authorization", "$.to"],
        ),
        input=captured({"question": question}),
        output=captured(output),
        events=b.events,
        metadata={"git_sha": "a41f9c2" if not candidate else "e07b3d5", "branch": "main"},
    )


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    save_fixture(build(candidate=False), DATA_DIR / "research-agent.baseline.json")
    save_fixture(build(candidate=True), DATA_DIR / "research-agent.candidate.json")


if __name__ == "__main__":
    main()
