"""Structural comparison of canonical JSON values.

Used by both the replay engine (argument matching) and the diff engine so that
divergence messages are identical wherever they are produced. Paths use a
JSONPath-like notation: ``$``, ``$.query``, ``$.items[2].id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ChangeKind = Literal["changed", "added", "removed", "type_changed"]


@dataclass(frozen=True)
class ValueChange:
    path: str
    kind: ChangeKind
    expected: Any = None
    actual: Any = None


def _key_path(path: str, key: str) -> str:
    if key.isidentifier():
        return f"{path}.{key}"
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'{path}["{escaped}"]'


def _type_tag(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def diff_values(
    expected: Any, actual: Any, path: str = "$", *, limit: int = 200
) -> list[ValueChange]:
    """Return every difference between two canonical JSON values (up to ``limit``)."""
    out: list[ValueChange] = []
    _diff(expected, actual, path, out, limit)
    return out


def first_difference(expected: Any, actual: Any, path: str = "$") -> ValueChange | None:
    """Return the first difference in depth-first, key-sorted order, or ``None``."""
    changes = diff_values(expected, actual, path, limit=1)
    return changes[0] if changes else None


def _diff(e: Any, a: Any, path: str, out: list[ValueChange], limit: int) -> None:
    if len(out) >= limit:
        return
    te, ta = _type_tag(e), _type_tag(a)
    if te != ta:
        out.append(ValueChange(path, "type_changed", e, a))
        return
    if te == "object":
        for key in sorted(set(e) | set(a)):
            if len(out) >= limit:
                return
            sub = _key_path(path, key)
            if key not in a:
                out.append(ValueChange(sub, "removed", e[key], None))
            elif key not in e:
                out.append(ValueChange(sub, "added", None, a[key]))
            else:
                _diff(e[key], a[key], sub, out, limit)
        return
    if te == "array":
        for i in range(max(len(e), len(a))):
            if len(out) >= limit:
                return
            sub = f"{path}[{i}]"
            if i >= len(a):
                out.append(ValueChange(sub, "removed", e[i], None))
            elif i >= len(e):
                out.append(ValueChange(sub, "added", None, a[i]))
            else:
                _diff(e[i], a[i], sub, out, limit)
        return
    if te == "number":
        # 1 and 1.0 are the same value once canonicalized to JSON.
        if e != a:
            out.append(ValueChange(path, "changed", e, a))
        return
    if e != a:
        out.append(ValueChange(path, "changed", e, a))
