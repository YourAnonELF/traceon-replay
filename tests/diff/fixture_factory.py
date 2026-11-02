"""Build schema-valid :class:`Fixture` objects directly, for tests of diff / CLI / pytest.

Shared by ``tests/diff``, ``tests/cli`` and ``tests/pytest_plugin`` (their
conftests load this file by path).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from traceon.core.canonical import call_key, canonicalize, content_hash
from traceon.core.schema import Cost, ErrorInfo, Event, Fixture, Payload, Usage

T0 = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
_UNSET: Any = object()


def captured(value: Any) -> Payload:
    value = canonicalize(value)
    return Payload.captured(value, sha256=content_hash(value))


def hash_only(value: Any) -> Payload:
    return Payload(state="hash_only", sha256=content_hash(value))


class FixtureBuilder:
    """Fluent builder. Calls are parented to a root span named after the run."""

    def __init__(
        self,
        name: str = "agent",
        fixture_id: str = "fx_base",
        *,
        root: bool = True,
        step_ms: float = 10.0,
    ) -> None:
        self.name = name
        self.fixture_id = fixture_id
        self.events: list[Event] = []
        self.step_ms = step_ms
        self._clock = T0
        self.root_id: str | None = None
        if root:
            self.root_id = self._add(kind="span", name=name, parent_id=None, duration_ms=None)

    def _next_id(self) -> str:
        return f"{self.fixture_id}_ev{len(self.events)}"

    def _add(
        self,
        *,
        kind: str,
        name: str,
        parent_id: str | None,
        duration_ms: float | None,
        **fields: Any,
    ) -> str:
        ev_id = self._next_id()
        start = self._clock
        end = start + dt.timedelta(milliseconds=duration_ms) if duration_ms is not None else None
        self.events.append(
            Event(
                event_id=ev_id,
                parent_id=parent_id,
                sequence=len(self.events),
                kind=kind,
                name=name,
                started_at=start,
                ended_at=end,  # type: ignore[arg-type]
                duration_ms=duration_ms,
                **fields,
            )
        )
        self._clock = start + dt.timedelta(milliseconds=self.step_ms)
        return ev_id

    def span(self, name: str, parent: str | None = _UNSET) -> str:
        parent_id = self.root_id if parent is _UNSET else parent
        return self._add(kind="span", name=name, parent_id=parent_id, duration_ms=5.0)

    def call(
        self,
        kind: str,
        name: str,
        request: Any = None,
        response: Any = None,
        *,
        request_payload: Payload | None = None,
        response_payload: Payload | None = None,
        status: str = "ok",
        error: tuple[str, str] | None = None,
        usage: Usage | None = None,
        parent: str | None = _UNSET,
        duration_ms: float = 10.0,
        side_effect: bool = False,
    ) -> str:
        req = request if request is not None else {}
        parent_id = self.root_id if parent is _UNSET else parent
        return self._add(
            kind=kind,
            name=name,
            parent_id=parent_id,
            duration_ms=duration_ms,
            status=status,
            side_effect=side_effect,
            call_key=call_key(kind, name, canonicalize(req)),
            request=request_payload or captured(req),
            response=response_payload
            or (captured(response) if status == "ok" else Payload.not_captured()),
            error=ErrorInfo(type=error[0], message=error[1]) if error else None,
            usage=usage,
        )

    def tool(self, name: str, request: Any = None, response: Any = None, **kw: Any) -> str:
        return self.call("tool", name, request, response, **kw)

    def model(
        self,
        name: str,
        request: Any = None,
        response: Any = None,
        *,
        tokens: tuple[int, int] | None = None,
        cost: float | None = None,
        cost_kind: str = "measured",
        currency: str = "USD",
        **kw: Any,
    ) -> str:
        usage = None
        if tokens is not None or cost is not None:
            usage = Usage(
                input_tokens=tokens[0] if tokens else None,
                output_tokens=tokens[1] if tokens else None,
                total_tokens=sum(tokens) if tokens else None,
                cost=Cost(amount=cost, kind=cost_kind, currency=currency)  # type: ignore[arg-type]
                if cost is not None
                else None,
            )
        return self.call("model", name, request, response, usage=usage, **kw)

    def value(self, name: str, response: Any) -> str:
        return self.call("value", name, {}, response, duration_ms=0.01)

    def build(
        self,
        *,
        output: Any = _UNSET,
        input: Any = _UNSET,
        status: str = "ok",
        error: tuple[str, str] | None = None,
        output_payload: Payload | None = None,
        warnings: list[str] | None = None,
        root_duration_ms: float | None = _UNSET,
    ) -> Fixture:
        events = list(self.events)
        if self.root_id is not None:
            total = (
                root_duration_ms
                if root_duration_ms is not _UNSET
                else max(10.0, self.step_ms * len(events))
            )
            root = events[0]
            end = root.started_at + dt.timedelta(milliseconds=total) if total is not None else None
            events[0] = root.model_copy(
                update={"duration_ms": total, "ended_at": end, "status": status}
            )
        out = output_payload or (
            captured(output) if output is not _UNSET else Payload.not_captured()
        )
        return Fixture(
            fixture_id=self.fixture_id,
            run_id=f"run_{self.fixture_id}",
            name=self.name,
            created_at=T0,
            status=status,
            events=events,  # type: ignore[arg-type]
            input=captured(input) if input is not _UNSET else Payload.not_captured(),
            output=out,
            error=ErrorInfo(type=error[0], message=error[1]) if error else None,
            warnings=warnings or [],
        )


def simple_run(
    fixture_id: str = "fx_base",
    *,
    query: str = "weather in paris",
    answer: str = "sunny",
    extra_call: bool = False,
    name: str = "agent",
) -> Fixture:
    """A small, realistic run: now -> model:planner -> tool:search -> model:writer."""
    b = FixtureBuilder(name, fixture_id)
    b.value("now", {"$datetime": "2026-01-01T12:00:00+00:00"})
    b.model("planner", {"prompt": "plan"}, {"text": "search"}, tokens=(100, 20), cost=0.002)
    b.tool("search", {"query": query}, {"results": ["a", "b"]})
    if extra_call:
        b.tool("fetch", {"url": "https://example.org"}, {"status": 200})
    b.model(
        "writer",
        {"prompt": "write"},
        {"text": answer},
        tokens=(300, 50),
        cost=0.004,
        cost_kind="estimated",
    )
    return b.build(input={"question": "weather?"}, output={"answer": answer})
