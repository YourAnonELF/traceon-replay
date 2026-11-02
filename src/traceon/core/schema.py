"""Versioned data model for Traceon fixtures, divergences and diff reports.

A *fixture* is a portable, self-contained JSON document describing one agent run:
an ordered list of events (spans, tool calls, model calls, nondeterministic
values) with optionally captured, redacted and size-limited payloads.

Three states of "no value" are kept distinct on purpose:

* ``Payload(state="not_captured")`` -- capture was disabled for this payload;
* ``Payload(state="redacted")``     -- captured, then removed by redaction policy;
* ``Payload(state="captured", value=None)`` -- the real value was ``None``.

``state="hash_only"`` stores only ``sha256`` of the canonical value, which is
enough to detect argument changes without retaining the content (the default
for model prompts).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from traceon.core.limits import DEFAULT_LIMITS

SCHEMA_VERSION: Final = "traceon.fixture/v1"

EventKind = Literal["span", "tool", "model", "value"]
#: Event kinds that are *calls*: matched and served during replay.
CALL_KINDS: frozenset[str] = frozenset({"tool", "model", "value"})
Status = Literal["ok", "error", "cancelled"]
PayloadState = Literal["captured", "not_captured", "redacted", "hash_only"]
FixtureSource = Literal["traceon", "langfuse"]
RunMode = Literal["record", "replay"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Payload(_Model):
    state: PayloadState
    #: Canonical JSON value. Present only when ``state == "captured"``.
    value: Any = None
    #: ``sha256:<hex>`` of the canonical value before truncation, when known.
    sha256: str | None = None
    #: True if ``value`` was cut to fit ``Limits.max_payload_bytes``.
    truncated: bool = False
    #: Size of the canonical JSON before truncation, in bytes.
    size_bytes: int | None = None
    #: JSONPaths inside ``value`` that were replaced by redaction placeholders.
    redacted_paths: list[str] = Field(default_factory=list)

    @classmethod
    def not_captured(cls) -> Payload:
        return cls(state="not_captured")

    @classmethod
    def captured(
        cls,
        value: Any,
        *,
        sha256: str | None = None,
        size_bytes: int | None = None,
        truncated: bool = False,
        redacted_paths: list[str] | None = None,
    ) -> Payload:
        return cls(
            state="captured",
            value=value,
            sha256=sha256,
            size_bytes=size_bytes,
            truncated=truncated,
            redacted_paths=redacted_paths or [],
        )

    @property
    def is_servable(self) -> bool:
        """Whether replay can return this payload as a real value."""
        return self.state == "captured" and not self.truncated

    @model_validator(mode="after")
    def _value_only_when_captured(self) -> Payload:
        if self.state != "captured" and self.value is not None:
            raise ValueError(f"payload in state {self.state!r} must not carry a value")
        return self


class ErrorInfo(_Model):
    #: Qualified exception type name, e.g. ``"builtins.ValueError"``.
    type: str
    #: Exception message after redaction; may be truncated.
    message: str
    #: Formatted stack frames; only present when stack capture is explicitly enabled.
    stack: list[str] | None = None


class Cost(_Model):
    amount: float
    currency: str = "USD"
    #: ``measured`` = reported by the provider; ``estimated`` = computed from a price table.
    kind: Literal["measured", "estimated"]
    #: Where the price came from (URL, table name, date) for estimated costs.
    price_source: str | None = None


class Usage(_Model):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost: Cost | None = None


class Event(_Model):
    event_id: str
    #: ``event_id`` of the enclosing span or call; ``None`` for top-level events.
    parent_id: str | None = None
    #: Global start order within the run, starting at 0 and strictly increasing.
    sequence: int = Field(ge=0)
    kind: EventKind
    name: str = Field(min_length=1, max_length=512)
    started_at: _dt.datetime
    ended_at: _dt.datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    status: Status = "ok"
    #: True if the real call mutates the outside world (payment, email, write...).
    side_effect: bool = False
    #: ``call_key(kind, name, redacted canonical request)``; set for call kinds.
    call_key: str | None = None
    request: Payload = Field(default_factory=Payload.not_captured)
    response: Payload = Field(default_factory=Payload.not_captured)
    error: ErrorInfo | None = None
    usage: Usage | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Environment(_Model):
    python: str | None = None
    platform: str | None = None
    traceon_version: str | None = None
    #: Versions of instrumented libraries / adapters, e.g. ``{"langfuse": "3.2.1"}``.
    adapters: dict[str, str] = Field(default_factory=dict)


class RedactionInfo(_Model):
    policy_id: str = "default"
    #: Human-readable names of the rules that were active (never the secrets themselves).
    rules: list[str] = Field(default_factory=list)
    #: Distinct JSONPaths (relative to an event payload) that were redacted anywhere in the run.
    fields_omitted: list[str] = Field(default_factory=list)


class Fixture(_Model):
    schema_version: Literal["traceon.fixture/v1"] = SCHEMA_VERSION
    fixture_id: str
    run_id: str
    name: str = Field(min_length=1, max_length=512)
    created_at: _dt.datetime
    source: FixtureSource = "traceon"
    mode: RunMode = "record"
    status: Status = "ok"
    environment: Environment = Field(default_factory=Environment)
    redaction: RedactionInfo = Field(default_factory=RedactionInfo)
    input: Payload = Field(default_factory=Payload.not_captured)
    output: Payload = Field(default_factory=Payload.not_captured)
    error: ErrorInfo | None = None
    events: list[Event] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: Non-fatal notes, e.g. importer fields that could not be mapped.
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_structure(self) -> Fixture:
        limits = DEFAULT_LIMITS
        if len(self.events) > limits.max_events:
            raise ValueError(f"fixture has {len(self.events)} events; limit is {limits.max_events}")
        ids: dict[str, Event] = {}
        prev_seq = -1
        for ev in self.events:
            if ev.event_id in ids:
                raise ValueError(f"duplicate event_id {ev.event_id!r}")
            if ev.sequence <= prev_seq:
                raise ValueError(
                    f"event {ev.event_id!r}: sequence {ev.sequence} is not greater than {prev_seq}"
                )
            prev_seq = ev.sequence
            ids[ev.event_id] = ev
        for ev in self.events:
            if ev.parent_id is not None and ev.parent_id not in ids:
                raise ValueError(f"event {ev.event_id!r}: unknown parent_id {ev.parent_id!r}")
        # Span depth (also rejects parent cycles).
        for ev in self.events:
            depth, cur = 0, ev
            while cur.parent_id is not None:
                depth += 1
                if depth > limits.max_span_depth:
                    raise ValueError(
                        f"event {ev.event_id!r}: span depth exceeds {limits.max_span_depth}"
                    )
                cur = ids[cur.parent_id]
        return self

    # Convenience accessors -------------------------------------------------
    def calls(self) -> list[Event]:
        """Events that replay matches and serves (tool, model and value events), in order."""
        return [e for e in self.events if e.kind in CALL_KINDS]

    def event(self, event_id: str) -> Event:
        for e in self.events:
            if e.event_id == event_id:
                return e
        raise KeyError(event_id)

    def children(self, parent_id: str | None) -> list[Event]:
        return [e for e in self.events if e.parent_id == parent_id]


DivergenceKind = Literal[
    "unexpected_call",  # candidate made a call the recording does not have (at this position)
    "missing_call",  # recording has a call the candidate never made
    "kind_mismatch",  # same position, different kind (tool vs model ...)
    "name_mismatch",  # same position, different tool/model name
    "argument_mismatch",  # same call identity, different normalized request
    "output_mismatch",  # final run output differs
    "status_mismatch",  # ok vs error (call-level or run-level)
    "error_mismatch",  # both failed, with different error type/message
    "response_unavailable",  # matching recording exists but its response cannot be served
]


class Divergence(_Model):
    kind: DivergenceKind
    #: Index into the ordered list of *calls* (``Fixture.calls()``) where it occurred.
    position: int | None = None
    baseline_event_id: str | None = None
    candidate_event_id: str | None = None
    #: Human-readable label of the call, e.g. ``"tool:search"``.
    label: str | None = None
    #: JSONPath of the first differing field, for argument/output mismatches.
    path: str | None = None
    expected: Any = None
    actual: Any = None
    message: str


class AlignedPair(_Model):
    status: Literal["same", "changed", "added", "removed"]
    kind: EventKind
    name: str
    baseline_event_id: str | None = None
    candidate_event_id: str | None = None


class RunMetrics(_Model):
    status: Status
    duration_ms: float | None = None
    events: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    errors: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_measured: float | None = None
    cost_estimated: float | None = None
    currency: str | None = None


class DiffReport(_Model):
    baseline_id: str
    candidate_id: str
    baseline_name: str
    candidate_name: str
    #: True when no divergences were found.
    equivalent: bool
    first_divergence: Divergence | None = None
    divergences: list[Divergence] = Field(default_factory=list)
    #: Call-by-call alignment of baseline vs candidate (calls only, not spans).
    alignment: list[AlignedPair] = Field(default_factory=list)
    baseline_metrics: RunMetrics
    candidate_metrics: RunMetrics
