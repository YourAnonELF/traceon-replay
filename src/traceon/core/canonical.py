"""Canonicalization and hashing.

Every value Traceon stores or compares first passes through :func:`canonicalize`,
which turns arbitrary Python objects into plain JSON data with a stable shape:

* dict keys become strings and are sorted on serialization;
* tuples, lists, sets and frozensets become lists (sets are sorted by their
  canonical JSON so ordering is deterministic);
* floats that JSON cannot express become ``{"$float": "nan" | "inf" | "-inf"}``;
* bytes become ``{"$bytes": <base64>}``;
* datetimes, dates, times, UUIDs, Decimals and paths become tagged strings;
* dataclasses and pydantic models become ``{"$type": <qualname>, ...fields}``;
* anything else becomes ``{"$repr": <qualified type name>}`` -- never the object's
  ``repr()``, which often embeds memory addresses and would break determinism.

The output is *data only*. Nothing produced here is ever turned back into an
executable object, so loading a fixture cannot run code.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import decimal
import enum
import hashlib
import json
import math
import pathlib
import uuid
from collections.abc import Mapping
from typing import Any

from traceon.core.limits import DEFAULT_LIMITS

JSONValue = Any  # None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]

_DEPTH_MARKER = {"$truncated": "max_depth"}


def _type_name(obj: object) -> str:
    t = type(obj)
    return f"{t.__module__}.{t.__qualname__}"


def canonicalize(value: Any, *, max_depth: int = DEFAULT_LIMITS.max_json_depth) -> JSONValue:
    """Return a JSON-safe, deterministic representation of ``value``."""
    return _canon(value, max_depth, 0, set())


def _canon(value: Any, max_depth: int, depth: int, seen: set[int]) -> JSONValue:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return {"$float": "nan"}
        if math.isinf(value):
            return {"$float": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, enum.Enum):
        return _canon(value.value, max_depth, depth, seen)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, _dt.datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, _dt.date):
        return {"$date": value.isoformat()}
    if isinstance(value, _dt.time):
        return {"$time": value.isoformat()}
    if isinstance(value, _dt.timedelta):
        return {"$timedelta": value.total_seconds()}
    if isinstance(value, decimal.Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, uuid.UUID):
        return {"$uuid": str(value)}
    if isinstance(value, pathlib.PurePath):
        return {"$path": value.as_posix()}

    if depth >= max_depth:
        return dict(_DEPTH_MARKER)

    oid = id(value)
    if oid in seen:
        return {"$cycle": _type_name(value)}
    seen.add(oid)
    try:
        if isinstance(value, Mapping):
            return {str(k): _canon(v, max_depth, depth + 1, seen) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_canon(v, max_depth, depth + 1, seen) for v in value]
        if isinstance(value, (set, frozenset)):
            items = [_canon(v, max_depth, depth + 1, seen) for v in value]
            return sorted(items, key=canonical_json_of_canonical)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            out: dict[str, JSONValue] = {"$type": _type_name(value)}
            for f in dataclasses.fields(value):
                out[f.name] = _canon(getattr(value, f.name), max_depth, depth + 1, seen)
            return out
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump) and not isinstance(value, type):
            try:
                dumped = model_dump(mode="json")
            except Exception:
                return {"$repr": _type_name(value)}
            out = {"$type": _type_name(value)}
            if isinstance(dumped, Mapping):
                out.update(
                    {str(k): _canon(v, max_depth, depth + 1, seen) for k, v in dumped.items()}
                )
            else:
                out["value"] = _canon(dumped, max_depth, depth + 1, seen)
            return out
        return {"$repr": _type_name(value)}
    finally:
        seen.discard(oid)


def canonical_json_of_canonical(value: JSONValue) -> str:
    """Serialize an *already canonical* value to compact, key-sorted JSON."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_json(value: Any) -> str:
    """Canonicalize ``value`` and serialize it to compact, key-sorted JSON."""
    return canonical_json_of_canonical(canonicalize(value))


def content_hash(value: Any) -> str:
    """Return ``"sha256:<hex>"`` of the canonical JSON of ``value``."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def call_key(kind: str, name: str, request: Any) -> str:
    """Stable identity of a call: kind + name + canonical (already-redacted) request."""
    return content_hash({"kind": kind, "name": name, "request": request})
