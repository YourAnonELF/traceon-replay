"""Helpers for replay tests: a fixture builder and a stand-in for the SDK decorators.

Fixtures are built directly from ``traceon.core.schema`` models, normalizing
requests exactly like the recorder (canonicalize -> redact -> call_key), so the
replay engine can be tested without depending on the SDK's decorators.
"""

from __future__ import annotations

import datetime as dt
import functools
import inspect
from collections.abc import Callable, Mapping
from typing import Any

from traceon.core.canonical import call_key, canonicalize, content_hash
from traceon.core.runtime import CallSpec, Session, current_session
from traceon.core.schema import ErrorInfo, Event, Fixture, Payload, Status, Usage
from traceon.replay.session import BUILTIN_VALUE_NAMES, Redactor, default_redactor

T0 = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
_UNSET: Any = object()


def normalize(
    kind: str, name: str, request: Mapping[str, Any], redactor: Redactor | None = None
) -> tuple[Any, str, str]:
    """Return ``(redacted canonical request, sha256, call_key)`` like the recorder."""
    canon = canonicalize(dict(request))
    if not (kind == "value" and name in BUILTIN_VALUE_NAMES):
        canon, _ = (redactor or default_redactor()).apply(canon)
    return canon, content_hash(canon), call_key(kind, name, canon)


class FixtureBuilder:
    """Fluent builder for recorded fixtures with a root span parenting every call."""

    def __init__(self, name: str = "agent", *, redactor: Redactor | None = None) -> None:
        self.name = name
        self.redactor = redactor
        self.events: list[Event] = [
            Event(event_id="ev_root", sequence=0, kind="span", name=name, started_at=T0)
        ]
        self._parent = "ev_root"

    def _next_id(self) -> str:
        return f"ev_{len(self.events):03d}"

    def add(
        self,
        kind: str,
        name: str,
        request: Mapping[str, Any] | None = None,
        response: Any = None,
        *,
        status: Status = "ok",
        error: ErrorInfo | None = None,
        request_state: str = "captured",
        response_payload: Payload | None = None,
        usage: Usage | None = None,
        with_call_key: bool = True,
        parent_id: str | None = _UNSET,
    ) -> Event:
        """Append a call event; returns it."""
        req, sha, key = normalize(kind, name, request or {}, self.redactor)
        if request_state == "captured":
            req_payload = Payload.captured(req, sha256=sha)
        elif request_state == "hash_only":
            req_payload = Payload(state="hash_only", sha256=sha)
        elif request_state == "redacted":
            req_payload = Payload(state="redacted", sha256=sha)
        else:
            req_payload = Payload.not_captured()
        if response_payload is None:
            if status == "ok":
                value = canonicalize(response)
                response_payload = Payload.captured(value, sha256=content_hash(value))
            else:
                response_payload = Payload.not_captured()
        event = Event(
            event_id=self._next_id(),
            parent_id=self._parent if parent_id is _UNSET else parent_id,
            sequence=len(self.events),
            kind=kind,  # type: ignore[arg-type]
            name=name,
            started_at=T0,
            ended_at=T0,
            duration_ms=1.0,
            status=status,
            call_key=key if with_call_key else None,
            request=req_payload,
            response=response_payload,
            error=error,
            usage=usage,
        )
        self.events.append(event)
        return event

    def tool(
        self, name: str, request: Mapping[str, Any] | None = None, response: Any = None, **kw: Any
    ) -> Event:
        return self.add("tool", name, request, response, **kw)

    def model(
        self, name: str, request: Mapping[str, Any] | None = None, response: Any = None, **kw: Any
    ) -> Event:
        kw.setdefault("request_state", "hash_only")
        return self.add("model", name, request, response, **kw)

    def value(self, name: str, response: Any) -> Event:
        return self.add("value", name, {}, response)

    def span(self, name: str) -> Event:
        event = Event(
            event_id=self._next_id(),
            parent_id=self._parent,
            sequence=len(self.events),
            kind="span",
            name=name,
            started_at=T0,
        )
        self.events.append(event)
        return event

    def build(
        self, *, output: Any = _UNSET, status: Status = "ok", error: ErrorInfo | None = None
    ) -> Fixture:
        out = Payload.not_captured()
        if output is not _UNSET:
            value = canonicalize(output)
            out = Payload.captured(value, sha256=content_hash(value))
        return Fixture(
            fixture_id="fx_baseline01",
            run_id="run_baseline1",
            name=self.name,
            created_at=T0,
            status=status,
            error=error,
            output=out,
            input=Payload.captured({"question": "weather?"}),
            events=list(self.events),
        )


def never() -> Any:
    raise AssertionError("the real call must never run during replay")


async def anever() -> Any:
    raise AssertionError("the real call must never run during replay")


def call(
    session: Session, kind: str, name: str, request: Mapping[str, Any] | None = None, **spec_kw: Any
) -> Any:
    """Make one intercepted call through ``session`` with a thunk that must not run."""
    return session.call(
        CallSpec(kind=kind, name=name, request=dict(request or {}), **spec_kw), never
    )  # type: ignore[arg-type]


async def acall(
    session: Session, kind: str, name: str, request: Mapping[str, Any] | None = None, **spec_kw: Any
) -> Any:
    return await session.acall(
        CallSpec(kind=kind, name=name, request=dict(request or {}), **spec_kw), anever
    )  # type: ignore[arg-type]


def stand_in(
    kind: str, name: str | None = None, **spec_kw: Any
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Minimal local stand-in for ``@traceon.tool`` / ``@traceon.model``."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)
        label = name or fn.__name__

        def spec_for(args: tuple[Any, ...], kwargs: dict[str, Any]) -> CallSpec:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return CallSpec(kind=kind, name=label, request=dict(bound.arguments), **spec_kw)  # type: ignore[arg-type]

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                session = current_session()
                if session is None:
                    return await fn(*args, **kwargs)
                return await session.acall(spec_for(args, kwargs), lambda: fn(*args, **kwargs))

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            session = current_session()
            if session is None:
                return fn(*args, **kwargs)
            return session.call(spec_for(args, kwargs), lambda: fn(*args, **kwargs))

        return wrapper

    return deco
