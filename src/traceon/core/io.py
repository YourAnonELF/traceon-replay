"""Safe reading and writing of fixture files.

Fixtures are untrusted input. :func:`load_fixture` refuses non-regular files,
enforces the size and JSON-depth limits *before* validation, and only ever
produces plain data (no pickle, no YAML tags, no object hooks).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from traceon.core.errors import FixtureError
from traceon.core.limits import DEFAULT_LIMITS, Limits
from traceon.core.schema import SCHEMA_VERSION, Fixture

PathLike = str | os.PathLike[str]


def _check_depth(value: Any, max_depth: int) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        cur, depth = stack.pop()
        if depth > max_depth:
            raise FixtureError(f"fixture JSON nesting exceeds {max_depth} levels")
        if isinstance(cur, dict):
            stack.extend((v, depth + 1) for v in cur.values())
        elif isinstance(cur, list):
            stack.extend((v, depth + 1) for v in cur)


def parse_fixture(data: str | bytes, *, limits: Limits = DEFAULT_LIMITS) -> Fixture:
    """Parse and validate a fixture from a JSON string or bytes."""
    if len(data) > limits.max_fixture_bytes:
        raise FixtureError(f"fixture is {len(data)} bytes; limit is {limits.max_fixture_bytes}")
    try:
        raw = json.loads(data)
    except RecursionError as exc:
        raise FixtureError("fixture JSON is nested too deeply") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise FixtureError(f"fixture is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise FixtureError("fixture must be a JSON object")
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise FixtureError(
            f"unsupported schema_version {version!r}; this Traceon reads {SCHEMA_VERSION!r}"
        )
    # Fixture payloads are one level below the event, which is two below the root.
    _check_depth(raw, limits.max_json_depth + 8)
    try:
        return Fixture.model_validate(raw)
    except ValidationError as exc:
        raise FixtureError(f"invalid fixture: {exc}") from exc


def load_fixture(path: PathLike, *, limits: Limits = DEFAULT_LIMITS) -> Fixture:
    """Read, bound-check and validate a fixture file."""
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError as exc:
        raise FixtureError(f"fixture not found: {p}") from exc
    if not p.is_file():
        raise FixtureError(f"not a regular file: {p}")
    if st.st_size > limits.max_fixture_bytes:
        raise FixtureError(
            f"fixture {p} is {st.st_size} bytes; limit is {limits.max_fixture_bytes}"
        )
    return parse_fixture(p.read_bytes(), limits=limits)


def dump_fixture(fixture: Fixture, *, indent: int | None = 2) -> str:
    """Serialize a fixture to JSON text (UTF-8, stable key order within models)."""
    return (
        json.dumps(
            fixture.model_dump(mode="json"), indent=indent, ensure_ascii=False, allow_nan=False
        )
        + "\n"
    )


def save_fixture(fixture: Fixture, path: PathLike) -> Path:
    """Atomically write ``fixture`` to ``path`` (creating parent directories)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = dump_fixture(fixture)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return p


def safe_join(base: PathLike, *parts: str) -> Path:
    """Join ``parts`` under ``base`` and refuse any result that escapes ``base``.

    Use for every path derived from untrusted input (HTTP requests, imported IDs).
    """
    root = Path(base).resolve()
    candidate = root.joinpath(*parts).resolve()
    if candidate != root and root not in candidate.parents:
        raise FixtureError(f"path escapes base directory: {'/'.join(parts)!r}")
    return candidate
