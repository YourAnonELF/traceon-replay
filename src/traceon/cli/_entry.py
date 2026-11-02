"""Loading and invoking user entry points given as ``--entry module:function``."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import os
import sys
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any

from traceon.cli._console import CliError
from traceon.core.limits import DEFAULT_LIMITS

#: Sentinel meaning "call the entry with no arguments".
NO_INPUT: Any = object()


def _ensure_cwd_on_path() -> None:
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)


def _import_module(target: str) -> Any:
    if target.endswith(".py"):
        path = Path(target)
        if not path.is_file():
            raise CliError(f"entry file not found: {target}")
        module_name = f"_traceon_entry_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise CliError(f"cannot load entry file {target}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise CliError(f"importing {target} failed: {type(exc).__name__}: {exc}") from exc
        return module
    try:
        return importlib.import_module(target)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing and (target == missing or target.startswith(missing + ".")):
            raise CliError(
                f"cannot import module {target!r}",
                hint="run traceon from your project root, or install the package "
                "(the current directory is added to sys.path)",
            ) from exc
        raise CliError(f"importing {target!r} failed: {exc}") from exc
    except Exception as exc:
        raise CliError(f"importing {target!r} failed: {type(exc).__name__}: {exc}") from exc


def load_entry(spec: str) -> Callable[..., Any]:
    """Resolve ``package.module:function`` (or ``path/to/file.py:function``).

    The current working directory is put first on ``sys.path`` so project modules
    import the same way they do from a script at the project root. Dotted
    attributes such as ``module:Agent.run`` are supported.
    """
    target, sep, attr = spec.rpartition(":")
    if not sep or not target or not attr:
        raise CliError(
            f"invalid --entry {spec!r}",
            hint="use the form package.module:function, e.g. --entry my_agent.main:run",
        )
    _ensure_cwd_on_path()
    obj: Any = _import_module(target)
    for part in attr.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise CliError(f"{target!r} has no attribute {attr!r}") from exc
    if not callable(obj):
        raise CliError(f"--entry {spec!r} is not callable (got {type(obj).__name__})")
    return obj  # type: ignore[no-any-return]


def parse_json_option(value: str, option: str) -> Any:
    """Parse a JSON option value, or ``@path`` to read JSON from a file."""
    if value.startswith("@"):
        path = Path(value[1:])
        try:
            size = path.stat().st_size
            if size > DEFAULT_LIMITS.max_fixture_bytes:
                raise CliError(f"{option} file {path} is too large ({size} bytes)")
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CliError(f"cannot read {option} file {path}: {exc.strerror or exc}") from exc
        source = str(path)
    else:
        text, source = value, option
    try:
        return json.loads(text)
    except ValueError as exc:
        raise CliError(
            f"{source} is not valid JSON: {exc}",
            hint=f"""quote strings as JSON, e.g. {option} '"hello"', or use {option} @file.json""",
        ) from exc


def build_arguments(
    fn: Callable[..., Any], value: Any = NO_INPUT
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Map an input value onto the entry's parameters.

    A JSON object becomes keyword arguments; any other value is passed as the
    single positional argument; no input means no arguments.
    """
    if value is NO_INPUT:
        args: tuple[Any, ...] = ()
        kwargs: dict[str, Any] = {}
    elif isinstance(value, Mapping):
        args, kwargs = (), {str(k): v for k, v in value.items()}
    else:
        args, kwargs = (value,), {}
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return args, kwargs
    try:
        signature.bind(*args, **kwargs)
    except TypeError as exc:
        name = getattr(fn, "__qualname__", repr(fn))
        raise CliError(
            f"input does not match {name}{signature}: {exc}",
            hint="a JSON object is passed as keyword arguments; any other value as "
            "the single positional argument",
        ) from exc
    return args, kwargs


async def _await(awaitable: Any) -> Any:
    return await awaitable


def call_entry(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Call ``fn``; coroutine functions and returned awaitables run to completion.

    Awaitables run with :func:`asyncio.run`, whose task copies the current
    context, so an active record or replay session is visible inside it.
    """
    result = fn(*args, **kwargs)
    if inspect.iscoroutine(result):
        coro: Coroutine[Any, Any, Any] = result
        return asyncio.run(coro)
    if inspect.isawaitable(result):
        return asyncio.run(_await(result))
    return result
