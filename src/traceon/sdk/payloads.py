"""The payload pipeline: canonicalize -> redact -> size-limit -> :class:`Payload`.

Every value Traceon stores (call requests and responses, run input and output,
metadata) passes through :func:`prepare_value` and :func:`make_payload`:

1. :func:`traceon.core.canonical.canonicalize` turns the Python value into plain,
   deterministic JSON data (the caller's objects are never mutated);
2. the :class:`~traceon.sdk.policy.RedactionPolicy` scrubs secrets and reports the
   JSONPaths it replaced;
3. ``sha256`` and ``size_bytes`` are computed over the *full* redacted value, so
   call matching and change detection keep working even when the stored value is
   truncated or only its hash is kept;
4. values larger than the payload limit are truncated to a smaller value that is
   still valid JSON (``truncated=True``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Final

from traceon.core.canonical import JSONValue, canonical_json_of_canonical, canonicalize
from traceon.core.schema import Payload
from traceon.sdk.policy import CaptureMode, RedactionPolicy

__all__ = [
    "TRUNCATED_KEY",
    "PreparedValue",
    "build_payload",
    "make_payload",
    "prepare_value",
    "truncate_text",
    "truncate_value",
]

#: Key of the summary object that replaces truncated objects and arrays.
TRUNCATED_KEY: Final = "$truncated"

_ELLIPSIS: Final = "…"


@dataclass(frozen=True)
class PreparedValue:
    """A canonicalized and redacted value plus its size and hash.

    Attributes:
        value: The redacted canonical JSON value (not truncated).
        redacted_paths: JSONPaths that redaction replaced.
        size_bytes: Size of the canonical JSON of ``value`` in UTF-8 bytes.
        sha256: ``"sha256:<hex>"`` of the canonical JSON of ``value``; identical to
            :func:`traceon.core.canonical.content_hash` of ``value``.
        fully_redacted: The whole value was replaced by a single placeholder.
    """

    value: JSONValue
    redacted_paths: list[str]
    size_bytes: int
    sha256: str
    fully_redacted: bool
    text: str


def prepare_value(
    value: Any,
    redaction: RedactionPolicy | None,
    *,
    already_canonical: bool = False,
) -> PreparedValue:
    """Canonicalize ``value`` (unless ``already_canonical``) and redact it.

    ``redaction=None`` skips redaction (used only for Traceon's own nondeterministic
    value helpers, whose outputs are never secrets).
    """
    canonical = value if already_canonical else canonicalize(value)
    if redaction is not None:
        redacted, paths = redaction.apply(canonical)
        fully = bool(paths) and redaction.is_placeholder(redacted)
    else:
        redacted, paths, fully = canonical, [], False
    text = canonical_json_of_canonical(redacted)
    encoded = text.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return PreparedValue(
        value=redacted,
        redacted_paths=paths,
        size_bytes=len(encoded),
        sha256=f"sha256:{digest}",
        fully_redacted=fully,
        text=text,
    )


def make_payload(prepared: PreparedValue, capture: CaptureMode, max_bytes: int) -> Payload:
    """Turn a prepared value into a :class:`Payload` according to ``capture``.

    * ``False``  -> ``state="not_captured"`` (nothing stored, not even a hash);
    * ``"hash"`` -> ``state="hash_only"`` (``sha256`` and ``size_bytes`` only);
    * ``True``   -> ``state="captured"``, or ``state="redacted"`` when redaction
      replaced the whole value; values above ``max_bytes`` are truncated.
    """
    if capture is False:
        return Payload.not_captured()
    if capture == "hash":
        return Payload(
            state="hash_only",
            sha256=prepared.sha256,
            size_bytes=prepared.size_bytes,
            redacted_paths=list(prepared.redacted_paths),
        )
    if capture is not True:
        raise ValueError(f"capture must be True, False or 'hash', got {capture!r}")
    if prepared.fully_redacted:
        return Payload(
            state="redacted",
            sha256=prepared.sha256,
            size_bytes=prepared.size_bytes,
            redacted_paths=list(prepared.redacted_paths),
        )
    value = prepared.value
    truncated = False
    if prepared.size_bytes > max_bytes:
        value = truncate_value(prepared.value, max_bytes, size_bytes=prepared.size_bytes)
        truncated = True
    return Payload.captured(
        value,
        sha256=prepared.sha256,
        size_bytes=prepared.size_bytes,
        truncated=truncated,
        redacted_paths=list(prepared.redacted_paths),
    )


def build_payload(
    value: Any,
    *,
    capture: CaptureMode,
    redaction: RedactionPolicy | None,
    max_bytes: int,
) -> Payload:
    """Run the full pipeline on a raw Python value and return its :class:`Payload`."""
    if capture is False:
        return Payload.not_captured()
    return make_payload(prepare_value(value, redaction), capture, max_bytes)


# --------------------------------------------------------------------------- truncation


def _json_size(value: JSONValue) -> int:
    return len(canonical_json_of_canonical(value).encode("utf-8"))


def truncate_text(text: str, max_bytes: int) -> str:
    """Cut ``text`` so that its JSON encoding fits in ``max_bytes`` (best effort).

    A marker such as ``"…[truncated 1200 of 5000 bytes]"`` is appended.
    """
    total = len(text.encode("utf-8"))
    if _json_size(text) <= max_bytes:
        return text

    def candidate(n: int) -> str:
        omitted = total - len(text[:n].encode("utf-8"))
        return f"{text[:n]}{_ELLIPSIS}[truncated {omitted} of {total} bytes]"

    lo, hi = 0, len(text)
    best = candidate(0)
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = candidate(mid)
        if _json_size(cand) <= max_bytes:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def truncate_value(value: JSONValue, max_bytes: int, *, size_bytes: int | None = None) -> JSONValue:
    """Return a smaller, still valid JSON stand-in for ``value``.

    * strings are cut and end with a ``…[truncated N of M bytes]`` marker;
    * objects and arrays become
      ``{"$truncated": {"type": "object"|"array", "size_bytes": M, "length": N,
      "preview": <leading entries that fit>}}``;
    * other scalars are returned unchanged (they are never large).
    """
    size = size_bytes if size_bytes is not None else _json_size(value)
    if size <= max_bytes:
        return value
    if isinstance(value, str):
        return truncate_text(value, max_bytes)
    if isinstance(value, dict):
        return _truncate_container(value, "object", size, max_bytes)
    if isinstance(value, list):
        return _truncate_container(value, "array", size, max_bytes)
    return value


def _truncate_container(
    value: dict[str, JSONValue] | list[JSONValue], type_name: str, size: int, max_bytes: int
) -> JSONValue:
    preview_obj: dict[str, JSONValue] = {}
    preview_arr: list[JSONValue] = []
    preview: dict[str, JSONValue] | list[JSONValue] = (
        preview_obj if type_name == "object" else preview_arr
    )

    def wrap() -> dict[str, JSONValue]:
        return {
            TRUNCATED_KEY: {
                "type": type_name,
                "size_bytes": size,
                "length": len(value),
                "preview": preview,
            }
        }

    used = _json_size(wrap())
    if used > max_bytes:
        return {TRUNCATED_KEY: {"type": type_name, "size_bytes": size, "length": len(value)}}

    items: list[tuple[str | None, JSONValue]]
    if isinstance(value, dict):
        items = [(k, value[k]) for k in sorted(value)]
    else:
        items = [(None, v) for v in value]

    for key, item in items:
        overhead = 1  # separator between entries
        if key is not None:
            overhead += len(canonical_json_of_canonical(key).encode("utf-8")) + 1  # "key":
        remaining = max_bytes - used - overhead
        if remaining <= 0:
            break
        item_size = _json_size(item)
        cut = False
        if item_size > remaining:
            if not (isinstance(item, str) and remaining > 64):
                break
            item = truncate_text(item, remaining)
            item_size = _json_size(item)
            cut = True
            if item_size > remaining:
                break
        if key is not None:
            preview_obj[key] = item
        else:
            preview_arr.append(item)
        used += overhead + item_size
        if cut:
            break

    # Guard against separator miscounting: drop trailing entries until it fits.
    while _json_size(wrap()) > max_bytes and preview:
        if isinstance(preview, dict):
            preview.pop(next(reversed(preview)))
        else:
            preview.pop()
    return wrap()
