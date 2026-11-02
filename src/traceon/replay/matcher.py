"""Matching calls made during replay against the calls in a recording.

Two strategies are provided:

* :class:`StrictMatcher` (``order="strict"``) -- the n-th call made during replay
  must be the n-th recorded call: same kind, same name, same request.
* :class:`KeyedMatcher` (``order="by_key"``) -- recorded calls are queued per
  ``(kind, name)``; a call consumes the oldest unconsumed recorded call with the
  same ``call_key``. Use it for agents that issue calls concurrently
  (``asyncio.gather``, thread pools) where completion order is not deterministic.

Every mismatch is described by a :class:`~traceon.core.schema.Divergence` whose
message pinpoints the call, the JSONPath of the first differing field and the
expected/actual values, using :func:`traceon.core.compare.first_difference` so
messages are identical to those of ``traceon diff``.

Matchers are not thread-safe; :class:`~traceon.replay.session.ReplaySession`
serializes access to them.
"""

from __future__ import annotations

import abc
import difflib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from traceon.core.canonical import canonical_json_of_canonical
from traceon.core.compare import ValueChange, first_difference
from traceon.core.schema import Divergence, Event

__all__ = [
    "CallAttempt",
    "KeyedMatcher",
    "Match",
    "Matcher",
    "StrictMatcher",
    "argument_divergence",
    "change_phrase",
    "describe_value",
    "make_matcher",
    "missing_call_divergence",
    "same_request",
]

_MAX_SHOWN_CHARS = 120


@dataclass(frozen=True)
class CallAttempt:
    """A call made by the code under replay, normalized like the recorder does it."""

    #: 0-based ordinal of this call among all calls made during the replay.
    index: int
    kind: str
    name: str
    #: Canonical, redacted request (what the recorder would have stored).
    request: Any
    #: ``content_hash`` of :attr:`request`.
    request_hash: str
    #: ``call_key(kind, name, request)``.
    call_key: str
    #: Id of the event describing this call in the replayed fixture.
    event_id: str | None = None

    @property
    def label(self) -> str:
        """``"kind:name"``, e.g. ``"tool:search"``."""
        return f"{self.kind}:{self.name}"


@dataclass(frozen=True)
class Match:
    """Outcome of matching one :class:`CallAttempt`.

    ``recorded`` is the consumed recorded call (``None`` if nothing was consumed);
    ``divergence`` is set when the attempt does not match the recording.
    """

    position: int | None
    recorded: Event | None
    divergence: Divergence | None = None


def describe_value(value: Any, *, limit: int = _MAX_SHOWN_CHARS) -> str:
    """Render a canonical JSON value compactly for a human-readable message."""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _label(event: Event) -> str:
    return f"{event.kind}:{event.name}"


def _short_hash(value: str | None) -> str:
    if not value:
        return "<unknown>"
    algo, _, digest = value.partition(":")
    return f"{algo}:{digest[:12]}" if digest else value[:19]


def same_request(recorded: Event, attempt: CallAttempt) -> bool:
    """Whether ``attempt`` carries the same request as ``recorded``.

    Uses ``call_key`` when the recording has one; falls back to the captured
    request (or its hash) for fixtures without call keys (e.g. imported traces).
    """
    if recorded.call_key is not None:
        return recorded.call_key == attempt.call_key
    payload = recorded.request
    if payload.state == "captured" and not payload.truncated:
        return canonical_json_of_canonical(payload.value) == canonical_json_of_canonical(
            attempt.request
        )
    if payload.sha256 is not None:
        return payload.sha256 == attempt.request_hash
    return True


def change_phrase(change: ValueChange, *, noun: str = "argument") -> str:
    """Describe one :class:`~traceon.core.compare.ValueChange` as "expected X, got Y"."""
    if change.kind == "added":
        return f"{noun} added (not in the recording), got {describe_value(change.actual)}"
    if change.kind == "removed":
        return f"{noun} missing, expected {describe_value(change.expected)}"
    return f"expected {describe_value(change.expected)}, got {describe_value(change.actual)}"


def argument_divergence(
    position: int,
    recorded: Event,
    attempt: CallAttempt,
    *,
    note: str | None = None,
) -> Divergence:
    """Build an ``argument_mismatch`` divergence with the most precise detail available."""
    label = attempt.label
    head = f"Call #{position} {label}"
    payload = recorded.request
    path: str | None = None
    expected: Any = None
    actual: Any = None
    if payload.state == "captured":
        change = first_difference(payload.value, attempt.request)
        if change is not None:
            path, expected, actual = change.path, change.expected, change.actual
            message = f"{head} diverged at {change.path}: {change_phrase(change)}"
            if payload.truncated:
                message += " (the recorded request was truncated, so this may be an artifact of truncation)"
        else:
            message = (
                f"{head} diverged: the request differs from the recording (call_key "
                f"{_short_hash(recorded.call_key)} recorded, {_short_hash(attempt.call_key)} now) "
                "but no field-level difference is visible in the captured request"
            )
    elif payload.state == "hash_only":
        path = "$"
        expected, actual = payload.sha256, attempt.request_hash
        message = (
            f"{head} diverged: the request hash differs (recorded {_short_hash(payload.sha256)}, "
            f"replayed {_short_hash(attempt.request_hash)}). Prompt capture was hash-only, so the "
            "changed field cannot be shown; re-record with capture_prompt=True (or "
            "capture_args=True for tools) to get a field-level diff"
        )
    else:
        what = "was redacted" if payload.state == "redacted" else "was not captured"
        path = "$"
        expected, actual = recorded.call_key, attempt.call_key
        message = (
            f"{head} diverged: the request differs from the recording (call_key "
            f"{_short_hash(recorded.call_key)} recorded, {_short_hash(attempt.call_key)} now). "
            f"The recorded request {what}, so the changed field cannot be shown"
        )
    if note:
        message += f". {note}"
    return Divergence(
        kind="argument_mismatch",
        position=position,
        baseline_event_id=recorded.event_id,
        candidate_event_id=attempt.event_id,
        label=label,
        path=path,
        expected=expected,
        actual=actual,
        message=message,
    )


def missing_call_divergence(position: int, recorded: Event) -> Divergence:
    """Build a ``missing_call`` divergence for a recorded call that was never made."""
    label = _label(recorded)
    return Divergence(
        kind="missing_call",
        position=position,
        baseline_event_id=recorded.event_id,
        label=label,
        expected=label,
        message=(
            f"Call #{position} {label} was recorded but never made during replay. The agent "
            "stopped earlier or took a different path; pass allow_unconsumed=True if a shorter "
            "run is expected"
        ),
    )


class Matcher(abc.ABC):
    """Base class: tracks which recorded calls have been consumed."""

    order: Literal["strict", "by_key"]

    def __init__(self, calls: Sequence[Event], *, note: str | None = None) -> None:
        self._calls: list[Event] = list(calls)
        self._consumed: list[bool] = [False] * len(self._calls)
        #: Extra sentence appended to argument-mismatch messages (e.g. a redaction hint).
        self._note = note

    @property
    def calls(self) -> list[Event]:
        """The recorded calls being matched against (``Fixture.calls()``)."""
        return list(self._calls)

    @property
    def consumed_count(self) -> int:
        """Number of recorded calls consumed so far."""
        return sum(self._consumed)

    def unconsumed(self) -> list[tuple[int, Event]]:
        """``(position, event)`` of every recorded call not consumed yet, in order."""
        return [(i, ev) for i, ev in enumerate(self._calls) if not self._consumed[i]]

    def _consume(self, position: int) -> Event:
        self._consumed[position] = True
        return self._calls[position]

    @abc.abstractmethod
    def match(self, attempt: CallAttempt, *, lenient: bool = False) -> Match:
        """Match ``attempt``.

        With ``lenient=True`` (overridden calls in ``mode="override"``) arguments are
        not compared and an unmatched call is not a divergence: the best recorded call
        with the same kind and name is consumed if one is available, otherwise nothing.
        """

    # Hints ---------------------------------------------------------------
    def _recorded_labels(self, kind: str) -> list[str]:
        seen: dict[str, None] = {}
        for ev in self._calls:
            if ev.kind == kind:
                seen.setdefault(_label(ev), None)
        return list(seen)

    def _unknown_name_hint(self, attempt: CallAttempt) -> str:
        labels = self._recorded_labels(attempt.kind)
        if not labels:
            return f"The recording has no {attempt.kind} calls at all"
        hint = f"The recording has no {attempt.label} call (recorded {attempt.kind} calls: {', '.join(labels[:8])}"
        hint += ", ...)" if len(labels) > 8 else ")"
        close = difflib.get_close_matches(attempt.label, labels, n=1)
        if close:
            hint += f"; did you mean {close[0]}?"
        return hint

    def _reorder_hint(self, attempt: CallAttempt, skip: int | None) -> str | None:
        for i, ev in enumerate(self._calls):
            if i == skip or self._consumed[i]:
                continue
            if ev.kind == attempt.kind and ev.name == attempt.name and same_request(ev, attempt):
                return (
                    f"The recording has this exact call at #{i}; if your agent issues calls "
                    'concurrently or in a nondeterministic order, replay with order="by_key"'
                )
        return None

    def _unexpected(self, attempt: CallAttempt, reason: str) -> Divergence:
        return Divergence(
            kind="unexpected_call",
            position=attempt.index,
            candidate_event_id=attempt.event_id,
            label=attempt.label,
            actual=attempt.request,
            message=(
                f"Call #{attempt.index} {attempt.label} is not in the recording: {reason}. Replay "
                "fails closed and never runs the real call; re-record the fixture, or provide "
                f'overrides={{{attempt.label!r}: ...}} with mode="override"'
            ),
        )

    def _with_note(self, *parts: str | None) -> str | None:
        joined = ". ".join(p for p in (*parts, self._note) if p)
        return joined or None


class StrictMatcher(Matcher):
    """The n-th call must match the n-th recorded call."""

    order = "strict"

    def __init__(self, calls: Sequence[Event], *, note: str | None = None) -> None:
        super().__init__(calls, note=note)
        self._cursor = 0

    def match(self, attempt: CallAttempt, *, lenient: bool = False) -> Match:
        pos = self._cursor
        if pos >= len(self._calls):
            if lenient:
                return Match(None, None)
            reason = (
                f"the recording has only {len(self._calls)} calls and all were already replayed"
            )
            return Match(None, None, self._unexpected(attempt, reason))
        rec = self._calls[pos]
        if lenient:
            if rec.kind == attempt.kind and rec.name == attempt.name:
                self._cursor += 1
                return Match(pos, self._consume(pos))
            return Match(None, None)

        self._cursor += 1
        self._consume(pos)
        rec_label = _label(rec)
        if rec.kind != attempt.kind:
            div = Divergence(
                kind="kind_mismatch",
                position=pos,
                baseline_event_id=rec.event_id,
                candidate_event_id=attempt.event_id,
                label=attempt.label,
                expected=rec.kind,
                actual=attempt.kind,
                message=self._join(
                    f"Call #{pos} diverged: expected a {rec.kind} call ({rec_label}), got a "
                    f"{attempt.kind} call ({attempt.label})",
                    self._reorder_hint(attempt, pos),
                ),
            )
            return Match(pos, rec, div)
        if rec.name != attempt.name:
            hint = self._reorder_hint(attempt, pos)
            if hint is None and attempt.label not in self._recorded_labels(attempt.kind):
                hint = self._unknown_name_hint(attempt)
            div = Divergence(
                kind="name_mismatch",
                position=pos,
                baseline_event_id=rec.event_id,
                candidate_event_id=attempt.event_id,
                label=attempt.label,
                expected=rec.name,
                actual=attempt.name,
                message=self._join(
                    f"Call #{pos} diverged: expected {rec_label}, got {attempt.label}", hint
                ),
            )
            return Match(pos, rec, div)
        if not same_request(rec, attempt):
            note = self._with_note(self._reorder_hint(attempt, pos))
            return Match(pos, rec, argument_divergence(pos, rec, attempt, note=note))
        return Match(pos, rec)

    @staticmethod
    def _join(message: str, hint: str | None) -> str:
        return f"{message}. {hint}" if hint else message


class KeyedMatcher(Matcher):
    """Per-``(kind, name)`` FIFO queues; the first unconsumed call with the same key wins."""

    order = "by_key"

    def __init__(self, calls: Sequence[Event], *, note: str | None = None) -> None:
        super().__init__(calls, note=note)
        self._queues: dict[tuple[str, str], list[int]] = {}
        for i, ev in enumerate(self._calls):
            self._queues.setdefault((ev.kind, ev.name), []).append(i)

    def match(self, attempt: CallAttempt, *, lenient: bool = False) -> Match:
        queue = self._queues.get((attempt.kind, attempt.name), [])
        available = [p for p in queue if not self._consumed[p]]
        if not available:
            if lenient:
                return Match(None, None)
            if queue:
                reason = (
                    f"all {len(queue)} recorded {attempt.label} call(s) were already replayed, "
                    "so this one is extra"
                )
            else:
                reason = self._unknown_name_hint(attempt)
                reason = reason[0].lower() + reason[1:]
            return Match(None, None, self._unexpected(attempt, reason))
        for pos in available:
            if same_request(self._calls[pos], attempt):
                return Match(pos, self._consume(pos))
        head = available[0]
        if lenient:
            return Match(head, self._consume(head))
        # Not consumed: under concurrency another in-flight call may still need it.
        return Match(
            head, None, argument_divergence(head, self._calls[head], attempt, note=self._note)
        )


def make_matcher(
    order: Literal["strict", "by_key"], calls: Sequence[Event], *, note: str | None = None
) -> Matcher:
    """Return the matcher implementing ``order``."""
    if order == "strict":
        return StrictMatcher(calls, note=note)
    if order == "by_key":
        return KeyedMatcher(calls, note=note)
    raise ValueError(f'order must be "strict" or "by_key", got {order!r}')
