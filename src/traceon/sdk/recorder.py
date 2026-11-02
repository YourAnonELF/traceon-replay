"""The recording session: runs real calls and turns them into fixture events.

:class:`RecordSession` implements :class:`traceon.core.runtime.Session` in
``mode="record"``. Instrumentation (``@tool``, ``@model``, ``span``, ``now`` ...)
hands it a :class:`~traceon.core.runtime.CallSpec` and a thunk; the session runs
the thunk, measures it, and stores an :class:`~traceon.core.schema.Event` whose
payloads went through the canonicalize -> redact -> size-limit pipeline.

Ordering and nesting:

* ``sequence`` is the global *start* order of events, allocated under a lock, so
  it is well defined across threads and asyncio tasks;
* the parent of an event is the innermost span or call active in the current
  ``contextvars`` context (:func:`traceon.core.runtime.current_parent_id`). Threads
  must be started with ``contextvars.copy_context().run`` to inherit it; asyncio
  tasks inherit it automatically.

Most users never construct a session directly -- use :func:`traceon.sdk.record_run`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import datetime as _dt
import inspect
import platform
import threading
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Literal, TypeVar

from traceon._version import __version__
from traceon.core.canonical import JSONValue, call_key
from traceon.core.errors import TraceonError
from traceon.core.limits import DEFAULT_LIMITS, Limits
from traceon.core.runtime import CallSpec, Session, current_parent_id, push_parent
from traceon.core.schema import (
    Cost,
    Environment,
    ErrorInfo,
    Event,
    Fixture,
    Payload,
    RedactionInfo,
    Status,
    Usage,
)
from traceon.sdk.payloads import (
    PreparedValue,
    make_payload,
    prepare_value,
    truncate_text,
    truncate_value,
)
from traceon.sdk.policy import CaptureMode, CapturePolicy, RedactionPolicy

__all__ = [
    "BUILTIN_VALUE_NAMES",
    "MAX_ERROR_MESSAGE_BYTES",
    "RecordSession",
    "extract_usage",
    "new_id",
]

T = TypeVar("T")

#: Names of Traceon's own nondeterministic value helpers. Their outputs (timestamps,
#: random floats, UUIDs) are never secrets, so they skip redaction -- which also
#: guarantees replay can always serve them.
BUILTIN_VALUE_NAMES: Final = frozenset({"now", "random", "uuid4"})

#: Error messages are cut to this many UTF-8 bytes after redaction.
MAX_ERROR_MESSAGE_BYTES: Final = 4096
_MAX_STACK_BYTES: Final = 32 * 1024
_NOT_CAPTURED_MESSAGE: Final = "[message not captured]"

_CANCELLED_TYPES: tuple[type[BaseException], ...] = (
    asyncio.CancelledError,
    concurrent.futures.CancelledError,
    KeyboardInterrupt,
)


def new_id(prefix: str) -> str:
    """Return ``prefix + 12 random hex characters`` (e.g. ``"ev_3fa85f6457b2"``)."""
    return prefix + uuid.uuid4().hex[:12]


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _type_name(exc: BaseException) -> str:
    t = type(exc)
    return f"{t.__module__}.{t.__qualname__}"


def _safe_str(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:
        return f"<unprintable {type(exc).__name__}>"


# --------------------------------------------------------------------------- usage

_INPUT_FIELDS: Final = ("input_tokens", "prompt_tokens", "prompt_token_count")
_OUTPUT_FIELDS: Final = ("output_tokens", "completion_tokens", "candidates_token_count")
_TOTAL_FIELDS: Final = ("total_tokens", "total_token_count")


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    try:
        return getattr(obj, key, None)
    except Exception:
        return None


def _first_int(obj: Any, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        val = _get(obj, key)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    return None


def _coerce_cost(raw: Any) -> Cost | None:
    if raw is None:
        return None
    if isinstance(raw, Cost):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return Cost(amount=float(raw), kind="measured")
    if isinstance(raw, Mapping):
        data = dict(raw)
        data.setdefault("kind", "measured")
        return Cost.model_validate(data)
    return None


def _coerce_usage(raw: Any) -> Usage | None:
    if raw is None:
        return None
    if isinstance(raw, Usage):
        return raw
    input_tokens = _first_int(raw, _INPUT_FIELDS)
    output_tokens = _first_int(raw, _OUTPUT_FIELDS)
    total_tokens = _first_int(raw, _TOTAL_FIELDS)
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    cost = _coerce_cost(_get(raw, "cost"))
    if input_tokens is None and output_tokens is None and total_tokens is None and cost is None:
        return None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost=cost,
    )


def extract_usage(spec: CallSpec, result: Any) -> Usage | None:
    """Extract token usage from a call result.

    Uses ``spec.usage`` when given. Otherwise, for ``kind="model"`` only, looks for
    ``result.usage`` / ``result["usage"]`` (OpenAI: ``prompt_tokens`` /
    ``completion_tokens``; Anthropic: ``input_tokens`` / ``output_tokens``) or
    ``usage_metadata`` (Gemini). ``total_tokens`` is derived when missing.
    """
    if spec.usage is not None:
        return _coerce_usage(spec.usage(result))
    if spec.kind != "model" or result is None:
        return None
    raw = _get(result, "usage")
    if raw is None:
        raw = _get(result, "usage_metadata")
    return _coerce_usage(raw)


# --------------------------------------------------------------------------- session


@dataclass
class _OpenEvent:
    event_id: str
    parent_id: str | None
    sequence: int
    spec: CallSpec
    started_at: _dt.datetime
    t0_ns: int
    request: Payload
    call_key: str | None
    attributes: dict[str, Any]
    redacted_paths: list[str] = field(default_factory=list)


class RecordSession(Session):
    """A :class:`~traceon.core.runtime.Session` that executes and records calls.

    Thread-safe: calls may arrive concurrently from threads and asyncio tasks.

    Args:
        name: Run name, stored as ``Fixture.name``.
        input: Run-level input. ``None`` means "no input" (stored as not captured).
        capture: Which payloads to store (default :class:`CapturePolicy`).
        redaction: How to scrub stored payloads (default :class:`RedactionPolicy`).
        metadata: Free-form run metadata (canonicalized and redacted).
        limits: Hard limits; events beyond ``limits.max_events`` or nested deeper
            than ``limits.max_span_depth`` are executed but not recorded (a warning
            is added to the fixture).
    """

    mode: ClassVar[Literal["record", "replay"]] = "record"

    def __init__(
        self,
        name: str,
        *,
        input: Any = None,
        capture: CapturePolicy | None = None,
        redaction: RedactionPolicy | None = None,
        metadata: Mapping[str, Any] | None = None,
        limits: Limits = DEFAULT_LIMITS,
    ) -> None:
        if not isinstance(name, str) or not name or len(name) > 512:
            raise ValueError("run name must be a non-empty string of at most 512 characters")
        self.name = name
        self.capture = capture if capture is not None else CapturePolicy()
        self.redaction = redaction if redaction is not None else RedactionPolicy()
        self.limits = limits
        self.run_id = new_id("run_")
        self.fixture_id = new_id("fx_")
        self.created_at = _utcnow()
        # Timestamps are derived from one wall-clock anchor plus a monotonic,
        # high-resolution counter, so they never go backwards and agree with durations.
        self._anchor_ns = time.perf_counter_ns()

        self._lock = threading.Lock()
        self._next_sequence = 0
        self._ids: set[str] = set()
        self._depth: dict[str, int] = {}
        self._open: dict[int, _OpenEvent] = {}
        self._done: dict[int, Event] = {}
        self._omitted: set[str] = set()
        self._warnings: list[str] = []
        self._warned: set[str] = set()
        self._finished = False
        self._output: Any = None
        self._output_set = False

        self._input = (
            self._run_payload(input, self.capture.run_input)
            if input is not None
            else (Payload.not_captured())
        )
        meta: dict[str, Any] = {}
        if metadata:
            prepared = prepare_value(dict(metadata), self.redaction)
            self._omitted.update(prepared.redacted_paths)
            limited = truncate_value(prepared.value, self.capture.max_payload_bytes)
            if isinstance(limited, dict):
                meta = limited
        self._metadata = meta

    # ------------------------------------------------------------------ public helpers
    @property
    def finished(self) -> bool:
        """Whether :meth:`finish` has been called."""
        return self._finished

    def set_output(self, value: Any) -> None:
        """Set the run-level output (``None`` is a real, captured value here)."""
        self._output = value
        self._output_set = True

    # ------------------------------------------------------------------ Session API
    def call(self, spec: CallSpec, invoke: Callable[[], T]) -> T:
        """Run ``invoke`` and record it as one ``tool``/``model``/``value`` event."""
        ev = self._start(spec)
        if ev is None:
            return invoke()
        try:
            with push_parent(ev.event_id):
                result = invoke()
                if inspect.isgenerator(result):
                    items = list(result)
                    ev.attributes["traceon.materialized"] = "generator"
                    self._end_ok(ev, items)
                    return iter(items)  # type: ignore[return-value]
        except BaseException as exc:
            self._end_error(ev, exc)
            raise
        self._end_ok(ev, result)
        return result

    async def acall(self, spec: CallSpec, invoke: Callable[[], Awaitable[T]]) -> T:
        """Await ``invoke()`` and record it as one event (``CancelledError`` -> cancelled)."""
        ev = self._start(spec)
        if ev is None:
            return await invoke()
        try:
            with push_parent(ev.event_id):
                result = await invoke()
        except BaseException as exc:
            self._end_error(ev, exc)
            raise
        self._end_ok(ev, result)
        return result

    @contextlib.contextmanager
    def span(self, spec: CallSpec) -> Iterator[str]:
        """Open a structural span; yields its ``event_id``. Exceptions are recorded and re-raised."""
        ev = self._start(spec)
        if ev is None:
            yield current_parent_id() or ""
            return
        try:
            with push_parent(ev.event_id):
                yield ev.event_id
        except BaseException as exc:
            self._end_error(ev, exc)
            raise
        self._end_ok(ev, None)

    def finish(self, *, output: Any = None, error: BaseException | None = None) -> Fixture:
        """Close the session and build the :class:`~traceon.core.schema.Fixture`.

        ``output`` (when not ``None``) overrides a value set with :meth:`set_output`;
        when neither is given the run output is stored as not captured. Events still
        open (e.g. a background thread outliving the run) are stored with status
        ``"cancelled"`` and attribute ``traceon.unfinished=true``.
        """
        with self._lock:
            if self._finished:
                raise TraceonError(f"record session {self.name!r} is already finished")
            self._finished = True
            open_events = sorted(self._open.values(), key=lambda e: e.sequence)
            self._open.clear()
        for ev in open_events:
            attrs = dict(ev.attributes)
            attrs["traceon.unfinished"] = True
            self._done[ev.sequence] = Event(
                event_id=ev.event_id,
                parent_id=ev.parent_id,
                sequence=ev.sequence,
                kind=ev.spec.kind,
                name=ev.spec.name,
                started_at=ev.started_at,
                status="cancelled",
                side_effect=ev.spec.side_effect,
                call_key=ev.call_key,
                request=ev.request,
                attributes=attrs,
            )

        if output is not None:
            self.set_output(output)
        out_payload = (
            self._run_payload(self._output, self.capture.run_output)
            if self._output_set
            else Payload.not_captured()
        )

        status: Status = "ok"
        error_info: ErrorInfo | None = None
        if error is not None:
            status = "cancelled" if isinstance(error, _CANCELLED_TYPES) else "error"
            error_info = self._error_info(error)

        events = [self._done[k] for k in sorted(self._done)]
        return Fixture(
            fixture_id=self.fixture_id,
            run_id=self.run_id,
            name=self.name,
            created_at=self.created_at,
            mode="record",
            status=status,
            environment=Environment(
                python=platform.python_version(),
                platform=platform.platform(),
                traceon_version=__version__,
            ),
            redaction=RedactionInfo(
                policy_id=self.redaction.policy_id,
                rules=self.redaction.rules,
                fields_omitted=sorted(self._omitted),
            ),
            input=self._input,
            output=out_payload,
            error=error_info,
            events=events,
            metadata=self._metadata,
            warnings=list(self._warnings),
        )

    # ------------------------------------------------------------------ internals
    def _at(self, perf_ns: int) -> _dt.datetime:
        return self.created_at + _dt.timedelta(microseconds=(perf_ns - self._anchor_ns) // 1000)

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self._warnings.append(message)

    def _run_payload(self, value: Any, capture: bool) -> Payload:
        if not capture:
            return Payload.not_captured()
        prepared = prepare_value(value, self.redaction)
        self._note_omitted(prepared)
        return make_payload(prepared, True, self.capture.max_payload_bytes)

    def _note_omitted(self, prepared: PreparedValue) -> None:
        if prepared.redacted_paths:
            with self._lock:
                self._omitted.update(prepared.redacted_paths)

    def _request_capture(self, spec: CallSpec) -> CaptureMode:
        if spec.capture_request is not None:
            return spec.capture_request
        if spec.kind == "model":
            return self.capture.model_prompts
        return True if spec.kind == "value" else self.capture.tool_args

    def _response_capture(self, spec: CallSpec) -> bool:
        if spec.capture_response is not None:
            return spec.capture_response
        if spec.kind == "model":
            return self.capture.model_results
        if spec.kind == "value":
            return True
        return self.capture.tool_results

    def _redaction_for(self, spec: CallSpec) -> RedactionPolicy | None:
        if spec.kind == "value" and spec.name in BUILTIN_VALUE_NAMES:
            return None
        return self.redaction

    def _attributes(self, spec: CallSpec) -> dict[str, Any]:
        if not spec.attributes:
            return {}
        prepared = prepare_value(dict(spec.attributes), self.redaction)
        self._note_omitted(prepared)
        value: JSONValue = prepared.value
        if prepared.size_bytes > self.capture.max_payload_bytes:
            value = {"traceon.attributes": truncate_value(value, self.capture.max_payload_bytes)}
        return dict(value) if isinstance(value, dict) else {}

    def _start(self, spec: CallSpec) -> _OpenEvent | None:
        # Snapshot the request *before* running the call: tools may mutate arguments.
        request = Payload.not_captured()
        key: str | None = None
        redacted_paths: list[str] = []
        if spec.kind != "span" or spec.request:
            prepared = prepare_value(dict(spec.request), self._redaction_for(spec))
            if spec.kind != "span":
                key = call_key(spec.kind, spec.name, prepared.value)
            request = make_payload(
                prepared, self._request_capture(spec), self.capture.max_payload_bytes
            )
            if request.state != "not_captured":
                redacted_paths = prepared.redacted_paths
        attributes = self._attributes(spec)
        parent = current_parent_id()

        with self._lock:
            if self._finished:
                self._warn_once(
                    "after_finish",
                    f"{spec.label} started after the run finished and was not recorded",
                )
                return None
            if len(self._done) + len(self._open) >= self.limits.max_events:
                self._warn_once(
                    "max_events",
                    f"event limit of {self.limits.max_events} reached; later events were not recorded",
                )
                return None
            if parent is not None and parent not in self._depth:
                parent = None
            depth = 0 if parent is None else self._depth[parent] + 1
            if depth > self.limits.max_span_depth:
                self._warn_once(
                    "max_span_depth",
                    f"span nesting deeper than {self.limits.max_span_depth} was not recorded",
                )
                return None
            event_id = new_id("ev_")
            while event_id in self._ids:
                event_id = new_id("ev_")
            self._ids.add(event_id)
            self._depth[event_id] = depth
            sequence = self._next_sequence
            self._next_sequence += 1
            self._omitted.update(redacted_paths)
            ev = _OpenEvent(
                event_id=event_id,
                parent_id=parent,
                sequence=sequence,
                spec=spec,
                started_at=self._at(t0_ns := time.perf_counter_ns()),
                t0_ns=t0_ns,
                request=request,
                call_key=key,
                attributes=attributes,
            )
            self._open[sequence] = ev
        return ev

    def _store(self, ev: _OpenEvent, **fields: Any) -> None:
        elapsed_ns = time.perf_counter_ns() - ev.t0_ns
        duration_ms = max(elapsed_ns, 0) / 1_000_000
        event = Event(
            event_id=ev.event_id,
            parent_id=ev.parent_id,
            sequence=ev.sequence,
            kind=ev.spec.kind,
            name=ev.spec.name,
            started_at=ev.started_at,
            ended_at=self._at(ev.t0_ns + elapsed_ns),
            duration_ms=duration_ms,
            side_effect=ev.spec.side_effect,
            call_key=ev.call_key,
            request=ev.request,
            attributes=ev.attributes,
            **fields,
        )
        with self._lock:
            if self._open.pop(ev.sequence, None) is None:
                return  # finish() already closed it as unfinished
            self._done[ev.sequence] = event

    def _end_ok(self, ev: _OpenEvent, result: Any) -> None:
        if ev.spec.kind == "span":
            self._store(ev, status="ok")
            return
        usage: Usage | None = None
        try:
            usage = extract_usage(ev.spec, result)
        except Exception as exc:
            ev.attributes["traceon.usage_error"] = _type_name(exc)
        capture = self._response_capture(ev.spec)
        response = Payload.not_captured()
        if capture:
            prepared = prepare_value(result, self._redaction_for(ev.spec))
            self._note_omitted(prepared)
            response = make_payload(prepared, True, self.capture.max_payload_bytes)
        self._store(ev, status="ok", response=response, usage=usage)

    def _end_error(self, ev: _OpenEvent, exc: BaseException) -> None:
        status: Status = "cancelled" if isinstance(exc, _CANCELLED_TYPES) else "error"
        self._store(ev, status=status, error=self._error_info(exc))

    def _error_info(self, exc: BaseException) -> ErrorInfo:
        if self.capture.error_messages:
            message, _ = self.redaction.redact_text(_safe_str(exc))
            message = truncate_text(message, MAX_ERROR_MESSAGE_BYTES)
        else:
            message = _NOT_CAPTURED_MESSAGE
        stack: list[str] | None = None
        if self.capture.stack_traces:
            lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
            stack = []
            budget = _MAX_STACK_BYTES
            for line in lines:
                redacted, _ = self.redaction.redact_text(line)
                size = len(redacted.encode("utf-8"))
                if size > budget:
                    stack.append("[stack truncated]")
                    break
                budget -= size
                stack.append(redacted)
        return ErrorInfo(type=_type_name(exc), message=message, stack=stack)
