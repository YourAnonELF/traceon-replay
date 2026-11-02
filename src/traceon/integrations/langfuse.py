"""Import Langfuse traces as Traceon fixtures.

Two entry points:

* :func:`fetch_trace` downloads one trace (with its observations) from the
  Langfuse public API using only :mod:`urllib` and HTTP basic auth.
* :func:`import_trace` converts that JSON -- or a JSON export saved earlier --
  into a :class:`~traceon.core.schema.Fixture` with ``source="langfuse"``.

Mapping rules
-------------
* The trace becomes the root ``span`` event; every observation becomes an event
  parented by ``parentObservationId`` (or the root span when it has none).
* Observation types: ``GENERATION`` and ``EMBEDDING`` -> ``model``; ``TOOL`` and
  ``RETRIEVER`` -> ``tool``; ``SPAN``, ``EVENT``, ``AGENT``, ``CHAIN``,
  ``EVALUATOR``, ``GUARDRAIL`` and anything unknown -> ``span``.
* Events are ordered by ``startTime`` and then by observation id.
* ``input`` / ``output`` become request / response payloads after passing
  through the same redaction policy the SDK uses when recording.
* ``usageDetails`` (or the deprecated ``usage`` object) and ``costDetails`` /
  ``totalCost`` become :class:`~traceon.core.schema.Usage`. Langfuse costs are
  either calculated from a price table or supplied by the instrumenting code,
  so they are always marked ``kind="estimated"`` with ``price_source="langfuse"``.
* ``level == "ERROR"`` marks the event as failed; ``statusMessage`` becomes the
  (redacted) error message.
* Anything that has no place in the fixture format is listed in
  ``fixture.warnings`` -- the import never drops data silently.

Imported fixtures are inspectable and diffable like recorded ones. Replaying
application code against them works when the application's instrumented call
names and arguments match what Langfuse captured; see ``docs/langfuse.md``.
"""

from __future__ import annotations

import base64
import datetime as _dt
import importlib
import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Protocol

from traceon._version import __version__
from traceon.core.canonical import call_key, canonical_json_of_canonical, canonicalize, content_hash
from traceon.core.errors import FixtureError, TraceonError
from traceon.core.limits import DEFAULT_LIMITS, Limits
from traceon.core.schema import (
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

__all__ = [
    "DEFAULT_HOST",
    "MAX_RESPONSE_BYTES",
    "LangfuseError",
    "Redactor",
    "fetch_trace",
    "import_trace",
    "observation_kind",
]

#: Langfuse Cloud (EU). Override with ``host=`` or ``LANGFUSE_BASE_URL`` / ``LANGFUSE_HOST``.
DEFAULT_HOST: Final = "https://cloud.langfuse.com"
#: Upper bound on the total bytes read from the Langfuse API for one trace.
MAX_RESPONSE_BYTES: Final = 20 * 1024 * 1024

_MODEL_TYPES: Final = frozenset({"GENERATION", "EMBEDDING"})
_TOOL_TYPES: Final = frozenset({"TOOL", "RETRIEVER"})
_ROOT_EVENT_ID: Final = "trace"
_MAX_ID_LEN: Final = 128
_MAX_NAME_LEN: Final = 512
_MAX_MESSAGE_LEN: Final = 4096
_PAGE_SIZE: Final = 100
_V2_FIELDS: Final = "core,basic,time,io,metadata,model,usage,prompt,metrics,trace_context"
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")

# Trace-level fields -------------------------------------------------------
#: Descriptive trace fields copied into ``fixture.metadata["langfuse"]``.
_TRACE_META_FIELDS: Final[Mapping[str, str]] = {
    "id": "trace_id",
    "sessionId": "session_id",
    "userId": "user_id",
    "release": "release",
    "version": "version",
    "tags": "tags",
    "environment": "environment",
    "public": "public",
    "bookmarked": "bookmarked",
    "htmlPath": "html_path",
    "projectId": "project_id",
    "externalId": "external_id",
    "createdAt": "created_at",
    "updatedAt": "updated_at",
}
#: Fields mapped onto fixture structure, or derived from observations.
_TRACE_HANDLED: Final = frozenset(
    {"timestamp", "name", "input", "output", "metadata", "observations", "latency", "totalCost"}
)
#: Fields whose content has no equivalent in the fixture format.
_TRACE_NOT_IMPORTED: Final[Mapping[str, str]] = {
    "scores": "scores/evaluations are not part of the fixture format",
}

# Observation-level fields -------------------------------------------------
_OBS_CORE: Final = frozenset(
    {
        "id",
        "type",
        "name",
        "startTime",
        "endTime",
        "parentObservationId",
        "input",
        "output",
        "level",
        "statusMessage",
        "metadata",
    }
)
_OBS_USAGE: Final = frozenset(
    {
        "usage",
        "usageDetails",
        "costDetails",
        "totalCost",
        "calculatedInputCost",
        "calculatedOutputCost",
        "calculatedTotalCost",
        "inputCost",
        "outputCost",
        "promptTokens",
        "completionTokens",
        "totalTokens",
        "inputUsage",
        "outputUsage",
        "totalUsage",
        "unit",
    }
)
#: Observation fields copied into ``event.attributes`` under the given key.
_OBS_ATTRIBUTES: Final[Mapping[str, str]] = {
    "model": "model",
    "modelParameters": "model_parameters",
    "completionStartTime": "completion_start_time",
    "version": "version",
    "environment": "environment",
    "promptId": "prompt_id",
    "promptName": "prompt_name",
    "promptVersion": "prompt_version",
    "modelId": "model_id",
    "internalModelId": "internal_model_id",
    "timeToFirstToken": "time_to_first_token_s",
    "usagePricingTierName": "pricing_tier",
    "inputPrice": "input_price",
    "outputPrice": "output_price",
    "totalPrice": "total_price",
}
#: Fields that duplicate trace-level data or are derived (``latency`` = end - start).
_OBS_REDUNDANT: Final = frozenset(
    {
        "traceId",
        "latency",
        "isRootObservation",
        "traceName",
        "tags",
        "release",
        "userId",
        "sessionId",
    }
)


class LangfuseError(TraceonError):
    """Fetching a trace from the Langfuse API failed.

    Messages never contain credentials or the ``Authorization`` header.
    """


class Redactor(Protocol):
    """The part of :class:`traceon.sdk.RedactionPolicy` the importer relies on."""

    policy_id: str

    def apply(self, value: Any) -> tuple[Any, list[str]]: ...


def observation_kind(observation_type: str | None) -> EventKind:
    """Map a Langfuse observation type to a Traceon event kind."""
    t = (observation_type or "").upper()
    if t in _MODEL_TYPES:
        return "model"
    if t in _TOOL_TYPES:
        return "tool"
    return "span"


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_trace(
    data: Mapping[str, Any],
    *,
    redaction: Redactor | None = None,
    capture_io: bool = True,
    limits: Limits = DEFAULT_LIMITS,
) -> Fixture:
    """Convert Langfuse trace JSON into a Traceon fixture.

    ``data`` is the body of ``GET /api/public/traces/{traceId}`` (trace fields
    plus an ``observations`` list), the dict returned by :func:`fetch_trace`, or
    a ``GET /api/public/v2/observations`` response (``{"data": [...], "meta":
    ...}``) for a single trace.

    ``redaction`` defaults to ``traceon.sdk.RedactionPolicy()``; every
    ``input``, ``output``, ``metadata`` and ``statusMessage`` passes through it.
    With ``capture_io=False`` inputs and outputs are not stored at all (call
    keys are still computed from the redacted request).

    Raises :class:`~traceon.core.errors.FixtureError` when ``data`` is not a
    Langfuse trace or exceeds ``limits``.
    """
    if not isinstance(data, Mapping):
        raise FixtureError(f"Langfuse trace must be a JSON object, got {type(data).__name__}")
    policy = redaction if redaction is not None else _default_redaction()
    return _Importer(policy, capture_io=capture_io, limits=limits).run(data)


def _default_redaction() -> Redactor:
    """Instantiate the SDK's default ``RedactionPolicy`` (imported lazily)."""
    try:
        module = importlib.import_module("traceon.sdk.policy")
    except ImportError:  # pragma: no cover - layout fallback
        module = importlib.import_module("traceon.sdk")
    policy: Redactor = module.RedactionPolicy()
    return policy


def _is_placeholder(policy: Redactor, value: Any) -> bool:
    check = getattr(policy, "is_placeholder", None)
    return bool(check(value)) if callable(check) else False


def _rule_names(policy: Redactor) -> list[str]:
    names = getattr(policy, "rules", None)
    if isinstance(names, (list, tuple)):
        return [str(n) for n in names]
    out: list[str] = []
    for pattern in getattr(policy, "key_patterns", ()) or ():
        out.append(f"key:{pattern}")
    for entry in getattr(policy, "value_patterns", ()) or ():
        if isinstance(entry, (tuple, list)) and entry:
            out.append(str(entry[0]))
    return out


class _Importer:
    def __init__(self, policy: Redactor, *, capture_io: bool, limits: Limits) -> None:
        self.policy = policy
        self.capture_io = capture_io
        self.limits = limits
        self.warnings: list[str] = []
        self.omitted: set[str] = set()
        self.unmapped_obs_fields: Counter[str] = Counter()
        self.naive_timestamps = False

    # -- helpers ---------------------------------------------------------
    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def redact(self, value: Any, prefix: str = "") -> Any:
        """Canonicalize + redact a value that is not an event payload (metadata, messages)."""
        canon = canonicalize(value, max_depth=self.limits.max_json_depth)
        redacted, paths = self.policy.apply(canon)
        for p in paths:
            self.omitted.add(prefix + p[1:] if prefix and p.startswith("$") else p)
        return redacted

    def payload(self, value: Any, *, present: bool) -> tuple[Payload, Any]:
        """Return ``(payload, redacted_canonical_value)`` for an input/output field."""
        if not present or value is None:
            return Payload.not_captured(), None
        canon = canonicalize(value, max_depth=self.limits.max_json_depth)
        redacted, paths = self.policy.apply(canon)
        self.omitted.update(paths)
        if not self.capture_io:
            return Payload.not_captured(), redacted
        text = canonical_json_of_canonical(redacted)
        raw = text.encode("utf-8")
        digest = content_hash(redacted)
        if paths == ["$"] and _is_placeholder(self.policy, redacted):
            return Payload(
                state="redacted", sha256=digest, size_bytes=len(raw), redacted_paths=["$"]
            ), redacted
        if len(raw) > self.limits.max_payload_bytes:
            # Same truncation format as recorded fixtures (still-valid JSON stand-in).
            payloads = importlib.import_module("traceon.sdk.payloads")
            preview = payloads.truncate_value(
                redacted, self.limits.max_payload_bytes, size_bytes=len(raw)
            )
            return Payload(
                state="captured",
                value=preview,
                sha256=digest,
                size_bytes=len(raw),
                truncated=True,
                redacted_paths=sorted(paths),
            ), redacted
        return Payload.captured(
            redacted, sha256=digest, size_bytes=len(raw), redacted_paths=sorted(paths)
        ), redacted

    def parse_time(self, value: Any, where: str) -> _dt.datetime | None:
        if value is None:
            return None
        parsed = _parse_datetime(value)
        if parsed is None:
            self.warn(f"{where}: unparseable timestamp {str(value)[:64]!r} ignored")
            return None
        if parsed.tzinfo is None:
            self.naive_timestamps = True
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed

    # -- main ------------------------------------------------------------
    def run(self, data: Mapping[str, Any]) -> Fixture:
        trace, observations = self.split(data)
        trace_id = _str_or_none(trace.get("id"))
        if trace_id is None:
            trace_id = next(
                (s for s in (_str_or_none(o.get("traceId")) for o in observations) if s), None
            )
        if trace_id is None:
            raise FixtureError("Langfuse trace has no 'id' (and no observation carries 'traceId')")
        if len(observations) + 1 > self.limits.max_events:
            raise FixtureError(
                f"Langfuse trace has {len(observations)} observations; the fixture limit is "
                f"{self.limits.max_events - 1}"
            )

        prepared = self.prepare_observations(observations)
        trace_start = self.parse_time(trace.get("timestamp"), "trace.timestamp")
        starts = [p["start"] for p in prepared if p["start"] is not None]
        if trace_start is None:
            trace_start = min(starts) if starts else None
        if trace_start is None:
            raise FixtureError(
                "Langfuse trace has no 'timestamp' and no observation has 'startTime'"
            )
        for p in prepared:
            if p["start"] is None:
                self.warn(
                    f"observation {p['raw_id']!r}: missing startTime; using the trace timestamp"
                )
                p["start"] = trace_start
        prepared.sort(key=lambda p: (p["start"], p["raw_id"]))

        ends = [p["end"] for p in prepared if p["end"] is not None]
        trace_end = max([trace_start, *ends]) if ends else None

        name = (
            _str_or_none(trace.get("name"))
            or next(
                (s for s in (_str_or_none(o.get("traceName")) for o in observations) if s), None
            )
            or "langfuse-trace"
        )
        name = self.clip_name(name, "trace.name")

        events: list[Event] = [
            Event(
                event_id=_ROOT_EVENT_ID,
                parent_id=None,
                sequence=0,
                kind="span",
                name=name,
                started_at=trace_start,
                ended_at=trace_end,
                duration_ms=_duration_ms(trace_start, trace_end),
                attributes={"langfuse.type": "TRACE"},
            )
        ]
        parents = self.resolve_parents(prepared)
        run_status: Status = "ok"
        run_error: ErrorInfo | None = None
        for seq, p in enumerate(prepared, start=1):
            event = self.build_event(p, seq, parents[p["event_id"]])
            events.append(event)
            if event.parent_id == _ROOT_EVENT_ID and event.status == "error" and run_error is None:
                run_status, run_error = "error", event.error

        in_payload, _ = self.payload(trace.get("input"), present="input" in trace)
        out_payload, _ = self.payload(trace.get("output"), present="output" in trace)
        metadata = self.trace_metadata(trace)

        self.check_trace_fields(trace)
        if self.unmapped_obs_fields:
            fields = ", ".join(f"{k} ({n})" for k, n in sorted(self.unmapped_obs_fields.items()))
            self.warn(f"observation fields not imported (count of observations): {fields}")
        if self.naive_timestamps:
            self.warn("timestamps without a timezone were interpreted as UTC")

        safe_id = _safe_id(trace_id)
        return Fixture(
            fixture_id=f"lf_{safe_id}",
            run_id=safe_id,
            name=name,
            created_at=trace_start,
            source="langfuse",
            mode="record",
            status=run_status,
            environment=Environment(traceon_version=__version__),
            redaction=RedactionInfo(
                policy_id=str(getattr(self.policy, "policy_id", "custom")),
                rules=_rule_names(self.policy),
                fields_omitted=sorted(self.omitted),
            ),
            input=in_payload,
            output=out_payload,
            error=run_error,
            events=events,
            metadata=metadata,
            warnings=list(self.warnings),
        )

    def split(self, data: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
        raw_obs: Any
        if "observations" in data:
            trace, raw_obs = data, data.get("observations")
        elif isinstance(data.get("data"), list):
            # GET /api/public/v2/observations response: no trace object at all.
            raw_obs = data.get("data")
            trace = _trace_from_observations(raw_obs)
            self.warn(
                "input is an observations list without a trace object; trace name, input and "
                "output were not available"
            )
        else:
            trace, raw_obs = data, []
            self.warn("trace has no 'observations' list; the fixture contains only the root span")
        if raw_obs is None:
            raw_obs = []
        if not isinstance(raw_obs, list):
            raise FixtureError("Langfuse 'observations' must be a list")
        observations: list[Mapping[str, Any]] = []
        for i, obs in enumerate(raw_obs):
            if isinstance(obs, Mapping):
                observations.append(obs)
            else:
                self.warn(f"observations[{i}] is not an object; skipped")
        return trace, observations

    def prepare_observations(
        self, observations: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        used: set[str] = {_ROOT_EVENT_ID}
        raw_to_event: dict[str, str] = {}
        for i, obs in enumerate(observations):
            raw_id = _str_or_none(obs.get("id")) or f"observation-{i}"
            if obs.get("id") is None:
                self.warn(f"observations[{i}] has no 'id'; assigned {raw_id!r}")
            event_id = _safe_id(raw_id)
            if event_id in used:
                base, n = event_id, 2
                while f"{base}~{n}" in used:
                    n += 1
                event_id = f"{base}~{n}"
                self.warn(
                    f"observation id {raw_id!r} is duplicated or reserved; renamed to {event_id!r}"
                )
            used.add(event_id)
            raw_to_event.setdefault(raw_id, event_id)
            prepared.append(
                {
                    "obs": obs,
                    "raw_id": raw_id,
                    "event_id": event_id,
                    "start": self.parse_time(
                        obs.get("startTime"), f"observation {raw_id!r} startTime"
                    ),
                    "end": self.parse_time(obs.get("endTime"), f"observation {raw_id!r} endTime"),
                }
            )
        for p in prepared:
            parent_raw = _str_or_none(p["obs"].get("parentObservationId"))
            parent: str | None = None
            if parent_raw is not None:
                parent = raw_to_event.get(parent_raw)
                if parent is None:
                    self.warn(
                        f"observation {p['raw_id']!r}: parent {parent_raw!r} is not in the trace; "
                        "attached to the root span"
                    )
                elif parent == p["event_id"]:
                    self.warn(
                        f"observation {p['raw_id']!r} lists itself as parent; attached to the root span"
                    )
                    parent = None
            p["parent"] = parent
        return prepared

    def resolve_parents(self, prepared: Sequence[dict[str, Any]]) -> dict[str, str]:
        """Return event_id -> parent event_id, breaking cycles and over-deep chains."""
        parent_of: dict[str, str | None] = {p["event_id"]: p["parent"] for p in prepared}
        max_chain = self.limits.max_span_depth - 1  # the root span adds one level
        for p in prepared:
            seen: list[str] = []
            cur: str | None = p["event_id"]
            while cur is not None:
                if cur in seen:
                    self.warn(
                        f"parent cycle through observation {cur!r}; attached it to the root span"
                    )
                    parent_of[cur] = None
                    break
                seen.append(cur)
                if len(seen) > max_chain:
                    self.warn(
                        f"observation {p['raw_id']!r} is nested deeper than "
                        f"{self.limits.max_span_depth} levels; attached to the root span"
                    )
                    parent_of[p["event_id"]] = None
                    break
                cur = parent_of[cur]
        return {eid: (pid if pid is not None else _ROOT_EVENT_ID) for eid, pid in parent_of.items()}

    def build_event(self, p: Mapping[str, Any], sequence: int, parent_id: str) -> Event:
        obs: Mapping[str, Any] = p["obs"]
        raw_id: str = p["raw_id"]
        obs_type = _str_or_none(obs.get("type")) or "SPAN"
        kind = observation_kind(obs_type)
        name = self.clip_name(
            _str_or_none(obs.get("name")) or obs_type.lower(), f"observation {raw_id!r} name"
        )

        request, redacted_request = self.payload(obs.get("input"), present="input" in obs)
        response, _ = self.payload(obs.get("output"), present="output" in obs)

        level = _str_or_none(obs.get("level"))
        status: Status = "error" if (level or "").upper() == "ERROR" else "ok"
        error: ErrorInfo | None = None
        status_message = obs.get("statusMessage")
        if status == "error":
            message = "observation level ERROR"
            if status_message is not None:
                redacted_msg = self.redact(str(status_message), prefix="$.error.message")
                message = str(redacted_msg)[:_MAX_MESSAGE_LEN]
            error = ErrorInfo(type="langfuse.ObservationError", message=message)

        attributes: dict[str, Any] = {"langfuse.type": obs_type, "langfuse.id": raw_id}
        if level is not None:
            attributes["level"] = level
        if status_message is not None and status != "error":
            attributes["status_message"] = self.redact(
                str(status_message), prefix="$.attributes.status_message"
            )
        if obs.get("metadata") is not None:
            attributes["metadata"] = self.redact(obs["metadata"], prefix="$.attributes.metadata")
        for field, key in _OBS_ATTRIBUTES.items():
            value = obs.get(field)
            if value is not None:
                attributes[key] = canonicalize(value, max_depth=self.limits.max_json_depth)
        usage = self.usage(obs, kind, attributes)

        for field, value in obs.items():
            if value is None or field in _OBS_CORE or field in _OBS_USAGE:
                continue
            if field in _OBS_ATTRIBUTES or field in _OBS_REDUNDANT:
                continue
            self.unmapped_obs_fields[field] += 1

        start: _dt.datetime = p["start"]
        end: _dt.datetime | None = p["end"]
        duration = _duration_ms(start, end)
        if end is not None and duration is None:
            self.warn(f"observation {raw_id!r}: endTime is before startTime; duration omitted")

        return Event(
            event_id=p["event_id"],
            parent_id=parent_id,
            sequence=sequence,
            kind=kind,
            name=name,
            started_at=start,
            ended_at=end,
            duration_ms=duration,
            status=status,
            call_key=call_key(kind, name, redacted_request) if kind != "span" else None,
            request=request,
            response=response,
            error=error,
            usage=usage,
            attributes=attributes,
        )

    def usage(
        self, obs: Mapping[str, Any], kind: EventKind, attributes: dict[str, Any]
    ) -> Usage | None:
        details = _num_dict(obs.get("usageDetails"))
        raw_legacy = obs.get("usage")
        legacy: Mapping[str, Any] = raw_legacy if isinstance(raw_legacy, Mapping) else {}
        legacy_unit = _str_or_none(legacy.get("unit")) or _str_or_none(obs.get("unit"))
        legacy_tokens = legacy_unit is None or legacy_unit.upper() == "TOKENS"

        def pick(*candidates: Any) -> int | None:
            for c in candidates:
                n = _int(c)
                if n is not None:
                    return n
            return None

        input_tokens = pick(
            details.get("input"),
            details.get("input_tokens"),
            details.get("prompt_tokens"),
            legacy.get("input") if legacy_tokens else None,
            obs.get("promptTokens"),
            obs.get("inputUsage"),
        )
        output_tokens = pick(
            details.get("output"),
            details.get("output_tokens"),
            details.get("completion_tokens"),
            legacy.get("output") if legacy_tokens else None,
            obs.get("completionTokens"),
            obs.get("outputUsage"),
        )
        total_tokens = pick(
            details.get("total"),
            details.get("total_tokens"),
            legacy.get("total") if legacy_tokens else None,
            obs.get("totalTokens"),
            obs.get("totalUsage"),
        )
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        if legacy and not legacy_tokens:
            self.warn(
                f"usage with unit {legacy_unit!r} is not token-based; not mapped to token counts"
            )

        costs = _num_dict(obs.get("costDetails"))
        amount = _float(costs.get("total"))
        if amount is None:
            amount = next(
                (
                    v
                    for v in (_float(obs.get(k)) for k in ("totalCost", "calculatedTotalCost"))
                    if v is not None
                ),
                None,
            )
        if amount is None:
            amount = _float(legacy.get("totalCost"))
        if amount is None and costs:
            amount = sum(costs.values())

        if details:
            attributes["usage_details"] = details
        if costs:
            attributes["cost_details"] = costs

        cost = (
            Cost(amount=amount, currency="USD", kind="estimated", price_source="langfuse")
            if amount is not None
            else None
        )
        numbers = (input_tokens, output_tokens, total_tokens)
        if cost is None and all(n is None for n in numbers):
            return None
        if kind != "model" and (cost is None or cost.amount == 0) and all(not n for n in numbers):
            return None
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost=cost,
        )

    def trace_metadata(self, trace: Mapping[str, Any]) -> dict[str, Any]:
        info: dict[str, Any] = {}
        for field, key in _TRACE_META_FIELDS.items():
            value = trace.get(field)
            if value is not None:
                info[key] = canonicalize(value, max_depth=self.limits.max_json_depth)
        if trace.get("metadata") is not None:
            info["metadata"] = self.redact(trace["metadata"], prefix="$.metadata.langfuse.metadata")
        latency = _float(trace.get("latency"))
        if latency is not None:
            info["latency_s"] = latency
        total_cost = _float(trace.get("totalCost"))
        if total_cost is not None:
            info["total_cost_usd"] = total_cost
        return {"langfuse": info}

    def check_trace_fields(self, trace: Mapping[str, Any]) -> None:
        for field, value in trace.items():
            if field in _TRACE_META_FIELDS or field in _TRACE_HANDLED:
                continue
            if field in _TRACE_NOT_IMPORTED:
                if value:
                    count = f" ({len(value)})" if isinstance(value, (list, dict)) else ""
                    self.warn(
                        f"trace field '{field}'{count} not imported: {_TRACE_NOT_IMPORTED[field]}"
                    )
                continue
            if value is not None:
                self.warn(f"unknown trace field '{field}' not imported")

    def clip_name(self, name: str, where: str) -> str:
        if len(name) > _MAX_NAME_LEN:
            self.warn(f"{where} longer than {_MAX_NAME_LEN} characters was truncated")
            return name[:_MAX_NAME_LEN]
        return name


# -- small pure helpers -------------------------------------------------------

#: Trace-context fields denormalized onto v2 observations -> trace field names.
_V2_TRACE_CONTEXT: Final[Mapping[str, str]] = {
    "traceId": "id",
    "traceName": "name",
    "tags": "tags",
    "release": "release",
    "userId": "userId",
    "sessionId": "sessionId",
    "environment": "environment",
}


def _trace_from_observations(observations: Sequence[Any]) -> dict[str, Any]:
    """Rebuild trace-level fields from the trace context repeated on v2 observations."""
    trace: dict[str, Any] = {}
    for obs in observations:
        if not isinstance(obs, Mapping):
            continue
        for src, dst in _V2_TRACE_CONTEXT.items():
            if dst not in trace and obs.get(src) is not None:
                trace[dst] = obs[src]
    return trace


_FRACTION = re.compile(r"\.(\d+)")


def _parse_datetime(value: Any) -> _dt.datetime | None:
    """Parse the ISO-8601 forms Langfuse emits (``Z`` suffix, 0-9 fractional digits)."""
    if isinstance(value, _dt.datetime):
        return value
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Python 3.10's fromisoformat accepts only 3 or 6 fractional digits.
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _duration_ms(start: _dt.datetime, end: _dt.datetime | None) -> float | None:
    if end is None:
        return None
    ms = (end - start).total_seconds() * 1000.0
    return round(ms, 3) if ms >= 0 else None


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        try:
            f = float(value)
        except ValueError:
            return None
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    return None


def _num_dict(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, float] = {}
    for k, v in value.items():
        f = _float(v) if not isinstance(v, str) else None
        if f is not None:
            out[str(k)] = int(f) if isinstance(v, int) else f
    return out


def _safe_id(raw: str) -> str:
    cleaned = _ID_UNSAFE.sub("_", raw)
    if len(cleaned) > _MAX_ID_LEN or not cleaned:
        return content_hash(raw)[7 : 7 + 32]
    return cleaned


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so credentials are never replayed to another URL."""

    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _open(request: urllib.request.Request, timeout: float) -> Any:
    """Open ``request``; the single seam tests replace to avoid the network."""
    return _OPENER.open(request, timeout=timeout)


_LOOPBACK_NAMES: Final = frozenset({"localhost"})


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() in _LOOPBACK_NAMES or hostname.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _resolve_host(host: str | None) -> str:
    raw = (
        host
        or os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or DEFAULT_HOST
    )
    parsed = urllib.parse.urlsplit(raw.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LangfuseError(f"invalid Langfuse host {raw!r}: expected https://host[:port][/path]")
    if parsed.username is not None or parsed.password is not None:
        raise LangfuseError(
            "Langfuse host must not embed credentials; use LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY"
        )
    if parsed.query or parsed.fragment:
        raise LangfuseError(
            f"Langfuse host must not contain a query or fragment: {parsed.hostname}"
        )
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise LangfuseError(
            f"refusing to send Langfuse credentials over plain http to {parsed.hostname}; "
            "use https (plain http is allowed only for localhost)"
        )
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


class _Client:
    def __init__(self, base: str, public_key: str, secret_key: str, timeout: float) -> None:
        self.base = base
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
        self._auth = f"Basic {token}"
        self.timeout = timeout
        self.remaining = MAX_RESPONSE_BYTES

    def get_json(
        self, path: str, query: Mapping[str, Any] | None = None, *, allow_status: Iterable[int] = ()
    ) -> tuple[int, Any]:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", f"traceon-replay/{__version__}")
        request.add_unredirected_header("Authorization", self._auth)
        try:
            response = _open(request, self.timeout)
        except urllib.error.HTTPError as exc:
            code = exc.code
            location = exc.headers.get("Location") if exc.headers is not None else None
            exc.close()
            if code in set(allow_status):
                return code, None
            raise LangfuseError(_http_error_message(code, self.base, location)) from None
        except (TimeoutError, OSError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(reason, TimeoutError):
                raise LangfuseError(
                    f"Langfuse at {self.base} did not respond within {self.timeout}s"
                ) from None
            raise LangfuseError(f"could not reach Langfuse at {self.base}: {reason}") from None
        with response:
            status = int(getattr(response, "status", 200))
            body = self._read(response)
        try:
            return status, json.loads(body)
        except (ValueError, RecursionError):
            raise LangfuseError(
                f"Langfuse at {self.base} returned a response that is not valid JSON"
            ) from None

    def _read(self, response: Any) -> bytes:
        limit_mb = MAX_RESPONSE_BYTES // (1024 * 1024)
        length = response.headers.get("Content-Length") if response.headers is not None else None
        if length is not None and length.isdigit() and int(length) > self.remaining:
            raise LangfuseError(f"Langfuse response exceeds the {limit_mb} MB import limit")
        data: bytes = response.read(self.remaining + 1)
        if len(data) > self.remaining:
            raise LangfuseError(f"Langfuse response exceeds the {limit_mb} MB import limit")
        self.remaining -= len(data)
        return data

    def list_observations(self, trace_id: str, max_events: int) -> list[Any]:
        observations: list[Any] = []
        cursor: str | None = None
        max_pages = max_events // _PAGE_SIZE + 2
        for _ in range(max_pages):
            _, body = self.get_json(
                "/api/public/v2/observations",
                {"traceId": trace_id, "fields": _V2_FIELDS, "limit": _PAGE_SIZE, "cursor": cursor},
            )
            if not isinstance(body, Mapping) or not isinstance(body.get("data"), list):
                raise LangfuseError("unexpected response from Langfuse /api/public/v2/observations")
            observations.extend(body["data"])
            meta = body.get("meta")
            cursor = meta.get("cursor") if isinstance(meta, Mapping) else None
            if not cursor or not body["data"]:
                return observations
        raise LangfuseError(f"trace {trace_id!r} has more than {max_events} observations")


def _http_error_message(code: int, base: str, location: str | None) -> str:
    if code in (401, 403):
        return (
            f"Langfuse rejected the credentials (HTTP {code}) at {base}; check LANGFUSE_PUBLIC_KEY, "
            "LANGFUSE_SECRET_KEY and that the host matches the project's region"
        )
    if 300 <= code < 400:
        target = urllib.parse.urlsplit(location).netloc if location else "another URL"
        return f"Langfuse at {base} redirected (HTTP {code}) to {target}; set the host to the final API URL"
    if code == 429:
        return f"Langfuse rate limit reached (HTTP 429) at {base}; retry later"
    return f"Langfuse API request failed with HTTP {code} at {base}"


def fetch_trace(
    trace_id: str,
    *,
    host: str | None = None,
    public_key: str | None = None,
    secret_key: str | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    """Download one trace with all observations from the Langfuse public API.

    Credentials default to ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``; the
    host defaults to ``LANGFUSE_BASE_URL``, then ``LANGFUSE_HOST``, then
    :data:`DEFAULT_HOST`. Requests use HTTP basic auth, never follow redirects,
    refuse plain ``http`` except for localhost, and read at most
    :data:`MAX_RESPONSE_BYTES` in total.

    Uses ``GET /api/public/traces/{traceId}``. If that endpoint is unavailable or
    returns no observations, the observations are listed through
    ``GET /api/public/v2/observations?traceId=...``.

    Returns the trace JSON (with an ``observations`` list) ready for
    :func:`import_trace`. Raises :class:`LangfuseError` on any failure; error
    messages never include credentials.
    """
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise LangfuseError("trace id must be a non-empty string")
    trace_id = trace_id.strip()
    if len(trace_id) > 256 or any(ord(c) < 32 for c in trace_id):
        raise LangfuseError("trace id is too long or contains control characters")
    if not timeout or timeout <= 0:
        raise LangfuseError("timeout must be a positive number of seconds")
    base = _resolve_host(host)
    pk = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = secret_key or os.environ.get("LANGFUSE_SECRET_KEY")
    if not pk or not sk:
        raise LangfuseError(
            "Langfuse credentials missing: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY "
            "(or pass public_key= / secret_key=)"
        )
    client = _Client(base, pk, sk, timeout)
    status, body = client.get_json(
        f"/api/public/traces/{urllib.parse.quote(trace_id, safe='')}",
        allow_status=(404, 405, 410),
    )
    if (
        status == 200
        and isinstance(body, dict)
        and isinstance(body.get("observations"), list)
        and body["observations"]
    ):
        return body
    observations = client.list_observations(trace_id, DEFAULT_LIMITS.max_events)
    if status != 200 and not observations:
        raise LangfuseError(f"Langfuse trace {trace_id!r} not found at {base}")
    trace: dict[str, Any] = (
        dict(body) if status == 200 and isinstance(body, dict) else {"id": trace_id}
    )
    trace["observations"] = observations
    return trace
