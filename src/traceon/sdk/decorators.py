"""Instrumentation: ``@tool``, ``@model``, ``span`` and nondeterministic value helpers.

Decorated functions behave exactly like the originals when no Traceon session is
active -- the only overhead is one context-variable lookup. Inside
:func:`traceon.sdk.record_run` every call is recorded; inside
``traceon.replay.replay_run`` it is served from the fixture instead of running.

Determinism
-----------
Replay can only reproduce a run if every source of nondeterminism goes through
Traceon. Agent code should therefore use :func:`now`, :func:`random` and
:func:`uuid4` instead of ``datetime.now()``, ``random.random()`` and
``uuid.uuid4()``, and wrap every external effect (HTTP, databases, model APIs)
in a ``@tool`` or ``@model`` function.

Generators
----------
A ``@tool``/``@model`` generator function is consumed eagerly while a session is
active: the yielded items are recorded as one list and the caller receives an
iterator over that list (an async iterator for async generator functions). Outside
a session the original generator is returned untouched.
"""

from __future__ import annotations

import datetime as _dt
import functools
import inspect
import random as _random
import uuid as _uuid
from collections.abc import AsyncIterator, Callable, Generator, Mapping
from types import TracebackType
from typing import Any, Final, Literal, ParamSpec, TypeAlias, TypeVar, cast, overload

from traceon.core.runtime import CallSpec, current_session
from traceon.core.schema import EventKind, Usage

__all__ = ["SpanContext", "model", "now", "random", "span", "tool", "uuid4"]

P = ParamSpec("P")
R = TypeVar("R")

UsageExtractor: TypeAlias = Callable[[Any], Usage | Mapping[str, Any] | None]
Decoder: TypeAlias = Callable[[Any], Any]

_MAX_NAME: Final = 512
_SELF_NAMES: Final = frozenset({"self", "cls"})
_POSITIONAL: Final = (
    inspect.Parameter.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
)


def _check_name(name: str, what: str) -> str:
    if not isinstance(name, str) or not name or len(name) > _MAX_NAME:
        raise ValueError(
            f"{what} name must be a non-empty string of at most {_MAX_NAME} characters"
        )
    return name


def _default_name(fn: Callable[..., Any]) -> str:
    name = getattr(fn, "__name__", None)
    if not isinstance(name, str) or not name:
        name = type(fn).__name__
    return name


class _Binder:
    """Turns ``(*args, **kwargs)`` into the request mapping of a :class:`CallSpec`."""

    __slots__ = ("drop_first", "signature")

    def __init__(self, fn: Callable[..., Any]) -> None:
        try:
            self.signature: inspect.Signature | None = inspect.signature(fn)
        except (TypeError, ValueError):
            self.signature = None
        self.drop_first = False
        if self.signature is not None:
            params = list(self.signature.parameters.values())
            if params and params[0].name in _SELF_NAMES and params[0].kind in _POSITIONAL:
                self.drop_first = True

    def bind(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        """Bind arguments with defaults applied; raises ``TypeError`` on a bad call."""
        if self.signature is None:
            return {"args": list(args), "kwargs": dict(kwargs)}
        bound = self.signature.bind(*args, **kwargs)
        bound.apply_defaults()
        request = dict(bound.arguments)
        if self.drop_first and request:
            request.pop(next(iter(request)))
        return request


def _instrument(
    fn: Callable[..., Any],
    *,
    kind: EventKind,
    name: str | None,
    side_effect: bool,
    capture_request: bool | Literal["hash"] | None,
    capture_response: bool | None,
    decode: Decoder | None,
    usage: UsageExtractor | None,
    attributes: Mapping[str, Any],
) -> Any:
    # Descriptors: ``@tool`` stacked on top of ``@staticmethod`` / ``@classmethod``.
    if isinstance(fn, staticmethod):
        return staticmethod(
            _instrument(
                fn.__func__,
                kind=kind,
                name=name,
                side_effect=side_effect,
                capture_request=capture_request,
                capture_response=capture_response,
                decode=decode,
                usage=usage,
                attributes=attributes,
            )
        )
    if isinstance(fn, classmethod):
        return classmethod(
            _instrument(
                fn.__func__,
                kind=kind,
                name=name,
                side_effect=side_effect,
                capture_request=capture_request,
                capture_response=capture_response,
                decode=decode,
                usage=usage,
                attributes=attributes,
            )
        )
    if not callable(fn):
        raise TypeError(f"@{kind} can only decorate callables, got {type(fn).__name__}")

    call_name = _check_name(name if name is not None else _default_name(fn), f"@{kind}")
    binder = _Binder(fn)
    attrs = dict(attributes)

    def make_spec(request: Mapping[str, Any]) -> CallSpec:
        return CallSpec(
            kind=kind,
            name=call_name,
            request=request,
            side_effect=side_effect,
            capture_request=capture_request,
            capture_response=capture_response,
            decode=decode,
            usage=usage,
            attributes=attrs,
        )

    wrapper: Callable[..., Any]

    if inspect.isasyncgenfunction(fn):

        def agen_wrapper(*args: Any, **kwargs: Any) -> Any:
            session = current_session()
            if session is None:
                return fn(*args, **kwargs)
            try:
                request = binder.bind(args, kwargs)
            except TypeError:
                return fn(*args, **kwargs)
            spec = make_spec(request)

            async def collect() -> list[Any]:
                return [item async for item in fn(*args, **kwargs)]

            async def replay_items() -> AsyncIterator[Any]:
                items = await session.acall(spec, collect)
                for item in items:
                    yield item

            return replay_items()

        wrapper = agen_wrapper

    elif inspect.iscoroutinefunction(fn):

        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            session = current_session()
            if session is None:
                return await fn(*args, **kwargs)
            try:
                request = binder.bind(args, kwargs)
            except TypeError:
                return await fn(*args, **kwargs)
            return await session.acall(make_spec(request), lambda: fn(*args, **kwargs))

        wrapper = async_wrapper

    elif inspect.isgeneratorfunction(fn):

        def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
            session = current_session()
            if session is None:
                return fn(*args, **kwargs)
            try:
                request = binder.bind(args, kwargs)
            except TypeError:
                return fn(*args, **kwargs)
            items = session.call(make_spec(request), lambda: list(fn(*args, **kwargs)))
            return iter(items)

        wrapper = gen_wrapper

    else:

        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            session = current_session()
            if session is None:
                return fn(*args, **kwargs)
            try:
                request = binder.bind(args, kwargs)
            except TypeError:
                return fn(*args, **kwargs)
            return session.call(make_spec(request), lambda: fn(*args, **kwargs))

        wrapper = sync_wrapper

    functools.update_wrapper(wrapper, fn)
    setattr(wrapper, "__traceon__", {"kind": kind, "name": call_name})  # noqa: B010
    return wrapper


# --------------------------------------------------------------------------- @tool


@overload
def tool(fn: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def tool(
    name: str | None = None,
    *,
    side_effect: bool = False,
    capture_args: bool | Literal["hash"] | None = None,
    capture_result: bool | None = None,
    decode: Decoder | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def tool(
    name: Callable[..., Any] | str | None = None,
    *,
    side_effect: bool = False,
    capture_args: bool | Literal["hash"] | None = None,
    capture_result: bool | None = None,
    decode: Decoder | None = None,
) -> Any:
    """Record (or, during replay, serve) calls to a tool function.

    Works bare (``@tool``) or with options (``@tool("search", side_effect=True)``),
    on sync and async functions, generators, methods, classmethods and
    staticmethods. The request is the function's bound arguments with defaults
    applied; ``self``/``cls`` are dropped.

    Args:
        name: Call name used for matching during replay (default: ``fn.__name__``).
        side_effect: Mark calls that change the outside world (payments, emails,
            writes). Replay never executes them unless explicitly overridden.
        capture_args: Override ``CapturePolicy.tool_args`` (``True``/``False``/``"hash"``).
        capture_result: Override ``CapturePolicy.tool_results``.
        decode: Converts the recorded canonical JSON result back into the type
            callers expect during replay, e.g. ``lambda v: Weather(**v)``.
    """
    options: dict[str, Any] = {
        "kind": "tool",
        "side_effect": side_effect,
        "capture_request": capture_args,
        "capture_response": capture_result,
        "decode": decode,
        "usage": None,
        "attributes": {},
    }
    if name is not None and not isinstance(name, str):
        return _instrument(name, name=None, **options)
    call_name = name

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        return cast("Callable[P, R]", _instrument(func, name=call_name, **options))

    return decorate


# --------------------------------------------------------------------------- @model


@overload
def model(fn: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def model(
    name: str | None = None,
    *,
    provider: str | None = None,
    usage: UsageExtractor | None = None,
    capture_prompt: bool | Literal["hash"] | None = None,
    capture_result: bool | None = None,
    decode: Decoder | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def model(
    name: Callable[..., Any] | str | None = None,
    *,
    provider: str | None = None,
    usage: UsageExtractor | None = None,
    capture_prompt: bool | Literal["hash"] | None = None,
    capture_result: bool | None = None,
    decode: Decoder | None = None,
) -> Any:
    """Record (or, during replay, serve) calls to a model (LLM) function.

    Same calling conventions as :func:`tool`. By default the prompt (request) is
    stored only as a hash (``CapturePolicy.model_prompts="hash"``) and the response
    in full.

    Args:
        name: Call name (default: ``fn.__name__``).
        provider: Free-form provider label stored in the event attributes.
        usage: ``callable(result) -> Usage | Mapping | None`` extracting token usage.
            When omitted, ``result.usage`` / ``result["usage"]`` is auto-detected
            (OpenAI ``prompt_tokens``/``completion_tokens``, Anthropic
            ``input_tokens``/``output_tokens``).
        capture_prompt: Override ``CapturePolicy.model_prompts``.
        capture_result: Override ``CapturePolicy.model_results``.
        decode: Converts the recorded JSON response back into the expected type
            during replay.
    """
    options: dict[str, Any] = {
        "kind": "model",
        "side_effect": False,
        "capture_request": capture_prompt,
        "capture_response": capture_result,
        "decode": decode,
        "usage": usage,
        "attributes": {"provider": provider} if provider is not None else {},
    }
    if name is not None and not isinstance(name, str):
        return _instrument(name, name=None, **options)
    call_name = name

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        return cast("Callable[P, R]", _instrument(func, name=call_name, **options))

    return decorate


# --------------------------------------------------------------------------- span


class SpanContext:
    """A structural span: context manager (sync and async) and decorator.

    Created by :func:`span`. As a context manager it yields the span's
    ``event_id`` (``None`` when no session is active). An instance tracks one
    active block at a time; as a decorator it opens a fresh span per call, so a
    decorated function is safe to call concurrently.
    """

    __slots__ = ("_attributes", "_stack", "name")

    def __init__(self, name: str, attributes: Mapping[str, Any]) -> None:
        self.name = _check_name(name, "span")
        self._attributes = dict(attributes)
        self._stack: list[Any] = []

    def _spec(self) -> CallSpec:
        return CallSpec(kind="span", name=self.name, attributes=self._attributes)

    def __enter__(self) -> str | None:
        session = current_session()
        if session is None:
            self._stack.append(None)
            return None
        cm = session.span(self._spec())
        event_id = cm.__enter__()
        self._stack.append(cm)
        return event_id

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        cm = self._stack.pop()
        if cm is None:
            return False
        return bool(cm.__exit__(exc_type, exc, tb))

    async def __aenter__(self) -> str | None:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, tb)

    def __call__(self, fn: Callable[P, R]) -> Callable[P, R]:
        """Use the span as a decorator: every call runs inside a new span."""
        name, attributes = self.name, self._attributes
        func: Callable[..., Any] = fn
        wrapper: Callable[..., Any]

        if inspect.isasyncgenfunction(func):

            async def agen_wrapper(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
                async with SpanContext(name, attributes):
                    async for item in func(*args, **kwargs):
                        yield item

            wrapper = agen_wrapper
        elif inspect.iscoroutinefunction(func):

            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async with SpanContext(name, attributes):
                    return await func(*args, **kwargs)

            wrapper = async_wrapper
        elif inspect.isgeneratorfunction(func):

            def gen_wrapper(*args: Any, **kwargs: Any) -> Generator[Any, Any, Any]:
                with SpanContext(name, attributes):
                    return (yield from func(*args, **kwargs))

            wrapper = gen_wrapper
        else:

            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                with SpanContext(name, attributes):
                    return func(*args, **kwargs)

            wrapper = sync_wrapper

        functools.update_wrapper(wrapper, func)
        return cast("Callable[P, R]", wrapper)


def span(name: str, **attributes: Any) -> SpanContext:
    """Group events under a named span (``kind="span"``).

    Usable as ``with span("plan"):``, ``async with span("plan"):`` or as a
    decorator ``@span("plan")``. Keyword arguments are stored (canonicalized and
    redacted) as the span's attributes. Spans are structural only: replay does not
    match them.
    """
    return SpanContext(name, attributes)


# --------------------------------------------------------------------------- values


def _decode_datetime(value: Any) -> _dt.datetime:
    if isinstance(value, dict) and isinstance(value.get("$datetime"), str):
        value = value["$datetime"]
    if not isinstance(value, str):
        raise TypeError(f"cannot decode a datetime from {type(value).__name__}")
    return _dt.datetime.fromisoformat(value)


def _decode_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"cannot decode a float from {type(value).__name__}")
    return float(value)


def _decode_str(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"cannot decode a string from {type(value).__name__}")
    return value


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _uuid4_str() -> str:
    return str(_uuid.uuid4())


_NOW_SPEC: Final = CallSpec(kind="value", name="now", decode=_decode_datetime)
_RANDOM_SPEC: Final = CallSpec(kind="value", name="random", decode=_decode_float)
_UUID4_SPEC: Final = CallSpec(kind="value", name="uuid4", decode=_decode_str)


def now() -> _dt.datetime:
    """Return the current time as a timezone-aware UTC ``datetime``.

    Use this instead of ``datetime.now()`` in agent code: the value is recorded
    (as ``{"$datetime": "<iso>"}``) and served back unchanged during replay, which
    keeps time-dependent behavior deterministic.
    """
    session = current_session()
    if session is None:
        return _utcnow()
    return session.call(_NOW_SPEC, _utcnow)


def random() -> float:
    """Return a random float in ``[0.0, 1.0)`` (from the stdlib ``random`` module).

    Use this instead of ``random.random()`` in agent code so the value is recorded
    and replayed deterministically. Seeding the stdlib generator still affects it.
    """
    session = current_session()
    if session is None:
        return _random.random()
    return session.call(_RANDOM_SPEC, _random.random)


def uuid4() -> str:
    """Return a random UUID4 as a string.

    Use this instead of ``uuid.uuid4()`` in agent code so the identifier is
    recorded and replayed deterministically.
    """
    session = current_session()
    if session is None:
        return _uuid4_str()
    return session.call(_UUID4_SPEC, _uuid4_str)
