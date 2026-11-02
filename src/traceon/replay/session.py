"""The replay session: serves recorded calls instead of running them.

:class:`ReplaySession` implements the :class:`~traceon.core.runtime.Session`
contract for ``mode="replay"``. Instrumented code hands it a
:class:`~traceon.core.runtime.CallSpec` plus a thunk that would perform the real
call; the session **never runs the thunk**. Instead it normalizes the request
exactly like the recorder (canonicalize, redact, ``call_key``), matches it against
the fixture and

* returns the recorded response (``spec.decode(value)`` if a decoder is set),
* re-raises a recorded failure as :class:`~traceon.core.errors.RecordedCallError`,
* or fails closed with :class:`~traceon.core.errors.UnmatchedCallError` /
  :class:`~traceon.core.errors.ResponseNotCapturedError`, recording a divergence.

Calls named in ``overrides`` (by ``"name"`` or ``"kind:name"`` label; the label
wins) are answered by the override instead: a callable is called with the call's
bound arguments as keyword arguments (``override(**spec.request)``; awaited if it
returns an awaitable), anything else is returned as-is.

* ``mode="strict"`` -- overridden calls must still match the recording; only the
  response is replaced.
* ``mode="override"`` -- overridden calls are exempt from argument matching (the
  "what if this tool returned X?" scenario): they consume the matching recorded
  call by kind and name when one is available and are never divergences.

While replaying, the session builds its own fixture (``mode="replay"``) mirroring
what happened, normalized like a recording so ``traceon diff`` between the
recording and the replay is meaningful.

The session is thread-safe and works with ``asyncio.gather`` when
``order="by_key"``.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import datetime as _dt
import hashlib
import importlib
import inspect
import platform
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol, TypeVar, cast

from traceon._version import __version__
from traceon.core.canonical import call_key, canonical_json_of_canonical, canonicalize
from traceon.core.compare import first_difference
from traceon.core.errors import (
    DivergenceError,
    RecordedCallError,
    ReplayError,
    ResponseNotCapturedError,
    UnmatchedCallError,
)
from traceon.core.limits import DEFAULT_LIMITS, Limits
from traceon.core.runtime import CallSpec, Session, current_parent_id, push_parent
from traceon.core.schema import (
    Divergence,
    Environment,
    ErrorInfo,
    Event,
    Fixture,
    Payload,
    RedactionInfo,
    Status,
)
from traceon.replay.matcher import CallAttempt, Matcher, change_phrase, make_matcher
from traceon.replay.matcher import missing_call_divergence as _missing
from traceon.replay.report import ReplayReport

__all__ = ["Redactor", "ReplaySession", "default_redactor"]

T = TypeVar("T")
#: Traceon's nondeterministic value helpers; their payloads skip redaction.
BUILTIN_VALUE_NAMES: frozenset[str] = frozenset({"now", "random", "uuid4"})
ReplayMode = Literal["strict", "override"]
ReplayOrder = Literal["strict", "by_key"]


class Redactor(Protocol):
    """Anything with the ``RedactionPolicy.apply`` signature."""

    def apply(self, value: Any) -> tuple[Any, list[str]]:
        """Return ``(redacted value, JSONPaths that were redacted)``."""
        ...


class _IdentityRedactor:
    """Used only when :mod:`traceon.sdk.policy` is not importable."""

    policy_id = "none"

    def apply(self, value: Any) -> tuple[Any, list[str]]:
        return value, []


def default_redactor() -> Redactor:
    """Return the SDK's default :class:`~traceon.sdk.policy.RedactionPolicy`.

    The recorder computes ``call_key`` on the *redacted* request, so replay must
    apply the same redaction to produce the same keys. Imported lazily to keep the
    replay engine independent of the SDK's import time.
    """
    try:
        module = importlib.import_module("traceon.sdk.policy")
    except ModuleNotFoundError as exc:
        if exc.name not in ("traceon.sdk", "traceon.sdk.policy"):
            raise
        return _IdentityRedactor()
    return cast(Redactor, module.RedactionPolicy())


def _policy_id(redactor: Redactor) -> str:
    value = getattr(redactor, "policy_id", None)
    return value if isinstance(value, str) else type(redactor).__name__


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _qualified_type(exc: BaseException) -> str:
    t = type(exc)
    return f"{t.__module__}.{t.__qualname__}"


def _decode_value_event(name: str, value: Any) -> Any:
    """Turn a recorded ``now`` / ``random`` / ``uuid4`` value back into a Python value."""
    if isinstance(value, dict) and len(value) == 1:
        if "$datetime" in value and isinstance(value["$datetime"], str):
            text = value["$datetime"]
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = _dt.datetime.fromisoformat(text)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=_dt.timezone.utc)
        if "$uuid" in value and isinstance(value["$uuid"], str):
            return value["$uuid"]
    if name == "random" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return value


class _NoOverride:
    pass


_NO_OVERRIDE = _NoOverride()


@dataclass
class _Normalized:
    value: Any
    redacted_paths: list[str]
    sha256: str
    size_bytes: int
    text: str


@dataclass
class _Plan:
    spec: CallSpec
    attempt: CallAttempt
    sequence: int
    parent_id: str | None
    started_at: _dt.datetime
    t0: float
    request: Payload
    attributes: dict[str, Any]
    recorded: Event | None
    position: int | None
    override: Any


class ReplaySession(Session):
    """Session that answers calls from a recorded :class:`~traceon.core.schema.Fixture`.

    Most users go through :func:`traceon.replay.replay_run`, which also installs the
    network guard and binds the session to the current context.
    """

    mode: ClassVar[Literal["record", "replay"]] = "replay"

    def __init__(
        self,
        fixture: Fixture,
        *,
        mode: ReplayMode = "strict",
        overrides: Mapping[str, Callable[..., Any] | Any] | None = None,
        order: ReplayOrder = "strict",
        allow_unconsumed: bool = False,
        limits: Limits = DEFAULT_LIMITS,
        redaction: Redactor | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if mode not in ("strict", "override"):
            raise ValueError(f'mode must be "strict" or "override", got {mode!r}')
        if order not in ("strict", "by_key"):
            raise ValueError(f'order must be "strict" or "by_key", got {order!r}')
        self.baseline = fixture
        self.replay_mode: ReplayMode = mode
        self.order: ReplayOrder = order
        self.allow_unconsumed = allow_unconsumed
        self.limits = limits
        self._overrides: dict[str, Any] = dict(overrides or {})
        self._overrides_used: set[str] = set()
        self._redactor: Redactor = redaction if redaction is not None else default_redactor()
        self._warnings: list[str] = []
        note: str | None = None
        replay_policy = _policy_id(self._redactor)
        if isinstance(self._redactor, _IdentityRedactor):
            self._warnings.append(
                "traceon.sdk.policy is not available; replay requests were compared without redaction"
            )
        if replay_policy != fixture.redaction.policy_id:
            note = (
                f"Note: the recording used redaction policy {fixture.redaction.policy_id!r} but "
                f"replay uses {replay_policy!r}; pass the recording's policy as redaction=... if "
                "redacted fields cause false mismatches"
            )
        self._matcher: Matcher = make_matcher(order, fixture.calls(), note=note)
        self._clock = clock
        self._t0 = clock()
        self._started_at = _utcnow()
        # Timestamps are one UTC anchor plus the high-resolution clock: the Windows
        # wall clock ticks every ~15 ms, which would give most events identical times.
        self._lock = threading.RLock()
        self._sequence = 0
        self._call_index = 0
        self._events: dict[int, Event] = {}
        self._divergences: list[Divergence] = []
        self._raised: list[BaseException] = []
        self._redacted_paths: dict[str, None] = {}
        self._served = 0
        self._overridden = 0
        self._output: Payload | None = None
        self._result: Fixture | None = None

    # ------------------------------------------------------------------ state
    @property
    def divergences(self) -> list[Divergence]:
        """Divergences detected so far (a snapshot)."""
        with self._lock:
            return list(self._divergences)

    @property
    def calls_served(self) -> int:
        """Calls answered from the recording so far."""
        return self._served

    @property
    def calls_overridden(self) -> int:
        """Calls answered by an override so far."""
        return self._overridden

    @property
    def finished(self) -> bool:
        """Whether :meth:`finish` has been called."""
        return self._result is not None

    # ---------------------------------------------------------- normalization
    def _normalize(self, value: Any, *, redact: bool = True) -> _Normalized:
        canon = canonicalize(value, max_depth=self.limits.max_json_depth)
        redacted, paths = self._redactor.apply(canon) if redact else (canon, [])
        text = canonical_json_of_canonical(redacted)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return _Normalized(
            redacted, list(paths), f"sha256:{digest}", len(text.encode("utf-8")), text
        )

    def _note_paths(self, paths: list[str]) -> None:
        for p in paths:
            self._redacted_paths.setdefault(p, None)

    def _captured(self, norm: _Normalized) -> Payload:
        limit = self.limits.max_payload_bytes
        if norm.size_bytes > limit:
            clipped = norm.text.encode("utf-8")[:limit].decode("utf-8", "ignore")
            return Payload.captured(
                clipped,
                sha256=norm.sha256,
                size_bytes=norm.size_bytes,
                truncated=True,
                redacted_paths=norm.redacted_paths,
            )
        return Payload.captured(
            norm.value,
            sha256=norm.sha256,
            size_bytes=norm.size_bytes,
            redacted_paths=norm.redacted_paths,
        )

    def _request_payload(
        self, spec: CallSpec, norm: _Normalized, recorded: Event | None
    ) -> Payload:
        capture: bool | Literal["hash"] | None = spec.capture_request
        if capture is None:
            if recorded is not None:
                state = recorded.request.state
                capture = "hash" if state == "hash_only" else state != "not_captured"
            else:
                capture = "hash" if spec.kind == "model" else True
        if capture is False:
            return Payload.not_captured()
        if capture == "hash":
            return Payload(state="hash_only", sha256=norm.sha256, size_bytes=norm.size_bytes)
        return self._captured(norm)

    def _error_info(self, exc: BaseException) -> ErrorInfo:
        if isinstance(exc, RecordedCallError):
            return exc.info.model_copy(deep=True)
        redacted, _ = self._redactor.apply(str(exc))
        message = redacted if isinstance(redacted, str) else str(redacted)
        limit = self.limits.max_payload_bytes
        if len(message) > limit:
            message = message[:limit]
        return ErrorInfo(type=_qualified_type(exc), message=message)

    # ---------------------------------------------------------------- events
    def _alloc(self) -> int:
        if len(self._events) >= self.limits.max_events:
            raise ReplayError(
                f"replay produced more than {self.limits.max_events} events (Limits.max_events)"
            )
        seq = self._sequence
        self._sequence += 1
        return seq

    def _close(
        self,
        plan: _Plan,
        *,
        status: Status,
        response: Payload | None = None,
        error: ErrorInfo | None = None,
        recorded_usage: bool = False,
    ) -> None:
        ended = self._wall()
        rec = plan.recorded
        event = Event(
            event_id=plan.attempt.event_id or _new_id("ev"),
            parent_id=plan.parent_id,
            sequence=plan.sequence,
            kind=plan.spec.kind,
            name=plan.spec.name,
            started_at=plan.started_at,
            ended_at=ended,
            duration_ms=max(0.0, (self._clock() - plan.t0) * 1000.0),
            status=status,
            side_effect=plan.spec.side_effect,
            call_key=plan.attempt.call_key,
            request=plan.request,
            response=response if response is not None else Payload.not_captured(),
            error=error,
            usage=rec.usage.model_copy(deep=True) if recorded_usage and rec and rec.usage else None,
            attributes=plan.attributes,
        )
        with self._lock:
            self._events[plan.sequence] = event

    # ------------------------------------------------------------- matching
    def _wall(self) -> _dt.datetime:
        """Current UTC time derived from the session anchor and the high-resolution clock."""
        return self._started_at + _dt.timedelta(seconds=max(0.0, self._clock() - self._t0))

    def _check_time(self, label: str) -> None:
        elapsed = self._clock() - self._t0
        limit = self.limits.max_replay_seconds
        if elapsed > limit:
            raise ReplayError(
                f"replay exceeded the wall-clock limit of {limit:g}s (elapsed {elapsed:.3f}s) at "
                f"{label}; raise Limits.max_replay_seconds if the agent legitimately needs longer"
            )

    def _lookup_override(self, spec: CallSpec) -> tuple[str | None, Any]:
        for key in (spec.label, spec.name):
            if key in self._overrides:
                return key, self._overrides[key]
        return None, _NO_OVERRIDE

    def _fail(self, plan: _Plan, divergence: Divergence, exc: BaseException) -> BaseException:
        """Record ``divergence``, close the event as failed and return ``exc`` to raise."""
        with self._lock:
            self._divergences.append(divergence)
            self._raised.append(exc)
        self._close(
            plan, status="error", error=ErrorInfo(type=_qualified_type(exc), message=str(exc))
        )
        return exc

    def _begin(self, spec: CallSpec) -> _Plan:
        if self._result is not None:
            raise ReplayError(
                f"{spec.label} was called after the replay session finished; calls made after "
                "the replay block exits are not intercepted"
            )
        if spec.kind == "span":
            raise ValueError("spans are not calls; use session.span(spec)")
        self._check_time(spec.label)
        # Like the recorder: Traceon's own value helpers are never secrets, never redacted.
        redact = not (spec.kind == "value" and spec.name in BUILTIN_VALUE_NAMES)
        norm = self._normalize(dict(spec.request), redact=redact)
        key = call_key(spec.kind, spec.name, norm.value)
        attrs = canonicalize(dict(spec.attributes), max_depth=self.limits.max_json_depth)
        override_key, override = self._lookup_override(spec)
        parent_id = current_parent_id()
        with self._lock:
            sequence = self._alloc()
            index = self._call_index
            self._call_index += 1
            attempt = CallAttempt(
                index=index,
                kind=spec.kind,
                name=spec.name,
                request=norm.value,
                request_hash=norm.sha256,
                call_key=key,
                event_id=_new_id("ev"),
            )
            lenient = override_key is not None and self.replay_mode == "override"
            match = self._matcher.match(attempt, lenient=lenient)
            if override_key is not None and match.divergence is None:
                self._overrides_used.add(override_key)
            started_at = self._wall()
            plan = _Plan(
                spec=spec,
                attempt=attempt,
                sequence=sequence,
                parent_id=parent_id,
                started_at=started_at,
                t0=self._clock(),
                request=self._request_payload(spec, norm, match.recorded),
                attributes=attrs if isinstance(attrs, dict) else {},
                recorded=match.recorded,
                position=match.position,
                override=override,
            )
            self._events[sequence] = Event(
                event_id=attempt.event_id or _new_id("ev"),
                parent_id=parent_id,
                sequence=sequence,
                kind=spec.kind,
                name=spec.name,
                started_at=started_at,
                call_key=key,
                request=plan.request,
                side_effect=spec.side_effect,
                attributes=plan.attributes,
            )
            self._note_paths(norm.redacted_paths)
        if match.divergence is not None:
            exc = UnmatchedCallError(match.divergence, [*self.divergences, match.divergence])
            raise self._fail(plan, match.divergence, exc)
        return plan

    def _serve(self, plan: _Plan) -> Any:
        rec = plan.recorded
        if rec is None:  # pragma: no cover - guaranteed by _begin
            raise ReplayError(f"internal error: no recorded call for {plan.spec.label}")
        if rec.status == "error":
            info = rec.error or ErrorInfo(
                type="traceon.UnknownError",
                message="the recorded call failed but its error details were not captured",
            )
            with self._lock:
                self._served += 1
            self._close(plan, status="error", error=info.model_copy(deep=True), recorded_usage=True)
            raise RecordedCallError(info)
        if rec.status == "cancelled":
            with self._lock:
                self._served += 1
            self._close(plan, status="cancelled", recorded_usage=True)
            raise asyncio.CancelledError(f"{plan.spec.label} was cancelled in the recording")
        response = rec.response
        if not response.is_servable:
            if response.state == "captured":
                original = f" ({response.size_bytes} bytes)" if response.size_bytes else ""
                reason = f"was truncated to fit Limits.max_payload_bytes{original}"
            elif response.state == "hash_only":
                reason = "was stored as a hash only"
            elif response.state == "redacted":
                reason = "was redacted"
            else:
                reason = "was not captured"
            position = plan.position if plan.position is not None else plan.attempt.index
            div = Divergence(
                kind="response_unavailable",
                position=position,
                baseline_event_id=rec.event_id,
                candidate_event_id=plan.attempt.event_id,
                label=plan.spec.label,
                message=(
                    f"Call #{position} {plan.spec.label} matched the recording but its response "
                    f"cannot be served: the recorded response {reason}. Re-record with result "
                    "capture enabled (capture_result=True, larger max_payload_bytes if truncated) "
                    f"or answer it with overrides={{{plan.spec.label!r}: ...}}"
                ),
            )
            raise self._fail(plan, div, ResponseNotCapturedError(div.message))
        value = copy.deepcopy(response.value)
        try:
            if plan.spec.decode is not None:
                result = plan.spec.decode(value)
            elif plan.spec.kind == "value":
                result = _decode_value_event(plan.spec.name, value)
            else:
                result = value
        except BaseException as exc:
            self._close(plan, status="error", error=self._error_info(exc))
            raise
        with self._lock:
            self._served += 1
        self._close(plan, status="ok", response=response.model_copy(deep=True), recorded_usage=True)
        return result

    def _start_override(self, plan: _Plan) -> Any:
        override = plan.override
        if callable(override):
            return override(**dict(plan.spec.request))
        return override

    def _finish_override(self, plan: _Plan, result: Any) -> None:
        with self._lock:
            self._overridden += 1
        if plan.spec.capture_response is False:
            response = Payload.not_captured()
        else:
            norm = self._normalize(result)
            with self._lock:
                self._note_paths(norm.redacted_paths)
            response = self._captured(norm)
        self._close(plan, status="ok", response=response)

    def _override_failed(self, plan: _Plan, exc: BaseException) -> None:
        with self._lock:
            self._overridden += 1
        status: Status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        self._close(
            plan, status=status, error=None if status == "cancelled" else self._error_info(exc)
        )

    # -------------------------------------------------------- Session API
    def call(self, spec: CallSpec, invoke: Callable[[], T]) -> T:
        """Answer a synchronous call from the recording (``invoke`` is never run)."""
        plan = self._begin(spec)
        if plan.override is _NO_OVERRIDE:
            return cast(T, self._serve(plan))
        try:
            result = self._start_override(plan)
        except BaseException as exc:
            self._override_failed(plan, exc)
            raise
        self._finish_override(plan, result)
        return cast(T, result)

    async def acall(self, spec: CallSpec, invoke: Callable[[], Awaitable[T]]) -> T:
        """Answer an asynchronous call from the recording (``invoke`` is never awaited)."""
        plan = self._begin(spec)
        if plan.override is _NO_OVERRIDE:
            return cast(T, self._serve(plan))
        try:
            result = self._start_override(plan)
            if inspect.isawaitable(result):
                result = await result
        except BaseException as exc:
            self._override_failed(plan, exc)
            raise
        self._finish_override(plan, result)
        return cast(T, result)

    def span(self, spec: CallSpec) -> contextlib.AbstractContextManager[str]:
        """Open a structural span in the replayed fixture; yields its ``event_id``."""
        return self._span(spec)

    @contextlib.contextmanager
    def _span(self, spec: CallSpec) -> Iterator[str]:
        attrs = canonicalize(dict(spec.attributes), max_depth=self.limits.max_json_depth)
        parent_id = current_parent_id()
        event_id = _new_id("ev")
        t0 = self._clock()
        with self._lock:
            sequence = self._alloc()
            started = Event(
                event_id=event_id,
                parent_id=parent_id,
                sequence=sequence,
                kind="span",
                name=spec.name,
                started_at=self._wall(),
                attributes=attrs if isinstance(attrs, dict) else {},
            )
            self._events[sequence] = started
        status: Status = "ok"
        error: ErrorInfo | None = None
        try:
            with push_parent(event_id):
                yield event_id
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except BaseException as exc:
            status, error = "error", self._error_info(exc)
            raise
        finally:
            closed = started.model_copy(
                update={
                    "ended_at": self._wall(),
                    "duration_ms": max(0.0, (self._clock() - t0) * 1000.0),
                    "status": status,
                    "error": error,
                }
            )
            with self._lock:
                self._events[sequence] = closed

    def set_output(self, value: Any) -> None:
        """Set the run-level output compared with the recording's output at :meth:`finish`."""
        norm = self._normalize(value)
        with self._lock:
            self._note_paths(norm.redacted_paths)
            self._output = self._captured(norm)

    def finish(self, *, output: Any = None, error: BaseException | None = None) -> Fixture:
        """Close the session, detect end-of-run divergences and return the replayed fixture.

        ``output`` (when not ``None``) is passed to :meth:`set_output`. Detected at
        finish: unconsumed recorded calls (``missing_call``, unless
        ``allow_unconsumed``), run output differences (``output_mismatch``, when both
        outputs are available) and run status/error differences
        (``status_mismatch`` / ``error_mismatch``).
        """
        if self._result is not None:
            raise ReplayError("finish() was already called on this replay session")
        if output is not None:
            self.set_output(output)
        with self._lock:
            if not self.allow_unconsumed:
                for position, ev in self._matcher.unconsumed():
                    self._divergences.append(_missing(position, ev))
            out_div = self._output_divergence()
            if out_div is not None:
                self._divergences.append(out_div)
            status: Status = "ok"
            error_info: ErrorInfo | None = None
            if error is not None:
                status = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
                error_info = None if status == "cancelled" else self._error_info(error)
            status_div = self._status_divergence(status, error_info, error)
            if status_div is not None:
                self._divergences.append(status_div)
            for key in sorted(set(self._overrides) - self._overrides_used):
                self._warnings.append(
                    f"override {key!r} was never used: no matching call was made during replay"
                )
            base = self.baseline
            self._result = Fixture(
                fixture_id=_new_id("fx"),
                run_id=_new_id("run"),
                name=base.name,
                created_at=self._started_at,
                source="traceon",
                mode="replay",
                status=status,
                environment=Environment(
                    python=platform.python_version(),
                    platform=sys.platform,
                    traceon_version=__version__,
                ),
                redaction=RedactionInfo(
                    policy_id=_policy_id(self._redactor),
                    rules=list(base.redaction.rules),
                    fields_omitted=list(self._redacted_paths),
                ),
                input=base.input.model_copy(deep=True),
                output=self._output or Payload.not_captured(),
                error=error_info,
                events=[self._events[s] for s in sorted(self._events)],
                metadata={
                    "replay_of": {
                        "fixture_id": base.fixture_id,
                        "run_id": base.run_id,
                        "mode": self.replay_mode,
                        "order": self.order,
                    }
                },
                warnings=list(self._warnings),
            )
            return self._result

    def report(self, *, network_attempts: int = 0) -> ReplayReport:
        """Build the :class:`ReplayReport` (requires :meth:`finish`)."""
        if self._result is None:
            raise ReplayError("the replay report is available only after finish()")
        with self._lock:
            divergences = list(self._divergences)
            return ReplayReport(
                fixture_id=self.baseline.fixture_id,
                replayed=self._result,
                divergences=divergences,
                first_divergence=divergences[0] if divergences else None,
                calls_served=self._served,
                calls_overridden=self._overridden,
                calls_total_recorded=len(self._matcher.calls),
                unconsumed=[ev.event_id for _, ev in self._matcher.unconsumed()],
                network_attempts=network_attempts,
                ok=not divergences,
            )

    # ------------------------------------------------------- end-of-run checks
    def _output_divergence(self) -> Divergence | None:
        recorded, replayed = self.baseline.output, self._output
        if replayed is None:
            return None
        if (
            recorded.state == "captured"
            and replayed.state == "captured"
            and not recorded.truncated
            and not replayed.truncated
        ):
            change = first_difference(recorded.value, replayed.value)
            if change is None:
                return None
            return Divergence(
                kind="output_mismatch",
                path=change.path,
                expected=change.expected,
                actual=change.actual,
                label="run:output",
                message=f"Run output diverged at {change.path}: {change_phrase(change, noun='field')}",
            )
        if recorded.state in ("captured", "hash_only") and recorded.sha256 and replayed.sha256:
            if recorded.sha256 == replayed.sha256:
                return None
            return Divergence(
                kind="output_mismatch",
                path="$",
                expected=recorded.sha256,
                actual=replayed.sha256,
                label="run:output",
                message=(
                    f"Run output diverged: hash {recorded.sha256[:19]} recorded, "
                    f"{replayed.sha256[:19]} now (the recorded output is not fully captured, so "
                    "the changed field cannot be shown)"
                ),
            )
        return None

    def _status_divergence(
        self, status: Status, error_info: ErrorInfo | None, error: BaseException | None
    ) -> Divergence | None:
        if error is not None and any(error is r for r in self._raised):
            # Replay's own fail-closed error: its divergence is already recorded.
            return None
        if isinstance(error, DivergenceError) and error.divergence in self._divergences:
            return None
        base = self.baseline
        if base.status != status:
            detail = f" ({error_info.type}: {error_info.message})" if error_info else ""
            recorded_detail = f" ({base.error.type}: {base.error.message})" if base.error else ""
            return Divergence(
                kind="status_mismatch",
                label="run:status",
                expected=base.status,
                actual=status,
                message=(
                    f"Run status diverged: the recording finished {base.status!r}{recorded_detail} "
                    f"but the replay finished {status!r}{detail}"
                ),
            )
        if (
            status == "error"
            and base.error is not None
            and error_info is not None
            and (base.error.type != error_info.type or base.error.message != error_info.message)
        ):
            return Divergence(
                kind="error_mismatch",
                label="run:error",
                expected=f"{base.error.type}: {base.error.message}",
                actual=f"{error_info.type}: {error_info.message}",
                message=(
                    f"Run failed differently: the recording raised {base.error.type}: "
                    f"{base.error.message!r}, the replay raised {error_info.type}: "
                    f"{error_info.message!r}"
                ),
            )
        return None
