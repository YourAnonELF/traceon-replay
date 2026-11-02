"""Decorator mechanics: binding, methods, wrapping, passthrough, spans, value helpers."""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import pickle
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sdk_testutil import ServingSession

from traceon.core.runtime import use_session
from traceon.core.schema import Event, Fixture
from traceon.sdk import model, now, random, record_run, span, tool, uuid4


@tool
def search(query: str, limit: int = 5, *tags: str, exact: bool = False, **extra: Any) -> list[str]:
    """Search documents."""
    return [query] * min(limit, 2)


@model("chat-model", provider="test")
async def chat(prompt: str, temperature: float = 0.0) -> str:
    """Call the model."""
    await asyncio.sleep(0)
    return f"echo:{prompt}"


class Agent:
    factor = 3

    @tool
    def scale(self, x: int) -> int:
        return x * self.factor

    @tool
    async def ascale(self, x: int) -> int:
        return x * self.factor

    @staticmethod
    @tool
    def static_inner(x: int) -> int:
        return x + 1

    @tool
    @staticmethod
    def static_outer(x: int) -> int:
        return x + 2

    @classmethod
    @tool
    def class_inner(cls, x: int) -> int:
        return x * cls.factor

    @tool
    @classmethod
    def class_outer(cls, x: int) -> int:
        return x - cls.factor


def calls(fx: Fixture) -> list[Event]:
    return fx.calls()


# --------------------------------------------------------------------------- binding


def test_request_binds_signature_with_defaults() -> None:
    with record_run("bind") as run:
        search("q", 1, "a", "b", exact=True, lang="en")
        search(query="r")
    first, second = calls(run.fixture)
    assert first.request.value == {
        "query": "q",
        "limit": 1,
        "tags": ["a", "b"],
        "exact": True,
        "extra": {"lang": "en"},
    }
    assert second.request.value == {
        "query": "r",
        "limit": 5,
        "tags": [],
        "exact": False,
        "extra": {},
    }


def test_bad_call_raises_natural_type_error_and_records_nothing() -> None:
    with record_run("badcall") as run, pytest.raises(TypeError):
        search()  # type: ignore[call-arg]
    assert calls(run.fixture) == []


async def test_async_model_records_with_custom_name() -> None:
    async with record_run("async-model") as run:
        assert await chat("hi") == "echo:hi"
    ev = calls(run.fixture)[0]
    assert (ev.kind, ev.name) == ("model", "chat-model")
    assert ev.attributes == {"provider": "test"}
    assert ev.request.state == "hash_only"
    assert ev.response.value == "echo:hi"


async def test_methods_drop_self_and_cls() -> None:
    agent = Agent()
    async with record_run("methods") as run:
        assert agent.scale(2) == 6
        assert await agent.ascale(2) == 6
        assert Agent.static_inner(1) == 2
        assert agent.static_outer(1) == 3
        assert Agent.class_inner(2) == 6
        assert agent.class_outer(5) == 2
    evs = calls(run.fixture)
    assert [e.name for e in evs] == [
        "scale",
        "ascale",
        "static_inner",
        "static_outer",
        "class_inner",
        "class_outer",
    ]
    assert [e.request.value for e in evs] == [
        {"x": 2},
        {"x": 2},
        {"x": 1},
        {"x": 1},
        {"x": 2},
        {"x": 5},
    ]


# --------------------------------------------------------------------------- wrapping


def test_functools_wraps_metadata_preserved() -> None:
    assert search.__name__ == "search"
    assert search.__doc__ == "Search documents."
    assert search.__wrapped__ is not None  # type: ignore[attr-defined]
    assert inspect.signature(search) == inspect.signature(search.__wrapped__)  # type: ignore[attr-defined]
    assert inspect.iscoroutinefunction(chat)
    assert search.__traceon__ == {"kind": "tool", "name": "search"}  # type: ignore[attr-defined]


def test_decorated_function_is_picklable_by_name() -> None:
    assert pickle.loads(pickle.dumps(search)) is search


def test_decorator_forms() -> None:
    @tool()
    def a() -> int:
        return 1

    @tool(name="bee")
    def b() -> int:
        return 2

    @model
    def c() -> int:
        return 3

    with record_run("forms") as run:
        a(), b(), c()
    assert [(e.kind, e.name) for e in calls(run.fixture)] == [
        ("tool", "a"),
        ("tool", "bee"),
        ("model", "c"),
    ]


def test_invalid_decorator_use() -> None:
    with pytest.raises(ValueError, match="name"):
        tool(name="")(lambda: None)
    with pytest.raises(TypeError, match="callables"):
        tool(42)  # type: ignore[call-overload]


# --------------------------------------------------------------------------- passthrough


def test_no_session_passthrough() -> None:
    sentinel = object()

    @tool
    def ident(x: Any) -> Any:
        return x

    @tool
    def gen() -> Iterator[int]:
        yield 1

    assert ident(sentinel) is sentinel
    assert inspect.isgenerator(gen())
    assert search("q") == ["q", "q"]
    assert asyncio.run(chat("x")) == "echo:x"


def test_no_session_value_helpers() -> None:
    stamp = now()
    assert stamp.tzinfo is not None and stamp.utcoffset() == dt.timedelta(0)
    assert 0.0 <= random() < 1.0
    assert uuid.UUID(uuid4()).version == 4


def test_session_returns_original_objects() -> None:
    marker = object()

    @tool
    def give() -> Any:
        return marker

    with record_run("identity") as run:
        assert give() is marker
    assert calls(run.fixture)[0].response.value == {"$repr": "builtins.object"}


# --------------------------------------------------------------------------- generators


async def test_generator_tools_are_materialized() -> None:
    @tool
    def numbers(n: int) -> Iterator[int]:
        yield from range(n)

    @tool
    async def anumbers(n: int) -> AsyncIterator[int]:
        for i in range(n):
            await asyncio.sleep(0)
            yield i

    async with record_run("gens") as run:
        assert list(numbers(3)) == [0, 1, 2]
        assert [x async for x in anumbers(2)] == [0, 1]
    evs = calls(run.fixture)
    assert [e.response.value for e in evs] == [[0, 1, 2], [0, 1]]


# --------------------------------------------------------------------------- span


async def test_span_as_decorator_sync_async_and_generator() -> None:
    @span("sync-step", stage=1)
    def step() -> int:
        return search("s")[0] and 1

    @span("async-step")
    async def astep() -> str:
        return await chat("a")

    @span("gen-step")
    def gstep() -> Iterator[int]:
        yield search("g", 1)[0] and 7

    async with record_run("span-deco") as run:
        assert step() == 1
        assert await astep() == "echo:a"
        assert list(gstep()) == [7]
    fx = run.fixture
    spans = {e.name: e for e in fx.events if e.kind == "span"}
    assert spans["sync-step"].attributes == {"stage": 1}
    pairs = [(e.name, fx.event(e.parent_id).name) for e in fx.calls() if e.parent_id]
    assert pairs == [("search", "sync-step"), ("chat-model", "async-step"), ("search", "gen-step")]
    assert all(s.status == "ok" for s in spans.values())


def test_span_without_session_yields_none() -> None:
    with span("nothing") as sid:
        assert sid is None


def test_span_attributes_are_redacted() -> None:
    with (
        record_run("span-attrs") as run,
        span("call", api_key="abc", note="Bearer abcdefghijklmnop"),
    ):
        pass
    ev = run.fixture.events[1]
    assert ev.attributes == {
        "api_key": "[REDACTED:sensitive_key]",
        "note": "Bearer [REDACTED:bearer_token]",
    }
    assert "$.api_key" in run.fixture.redaction.fields_omitted


def test_span_name_validated() -> None:
    with pytest.raises(ValueError):
        span("")


# --------------------------------------------------------------------------- value helpers


def test_value_events_are_recorded_and_decodable() -> None:
    with record_run("values") as run:
        t, r, u = now(), random(), uuid4()
    evs = calls(run.fixture)
    assert [(e.kind, e.name) for e in evs] == [
        ("value", "now"),
        ("value", "random"),
        ("value", "uuid4"),
    ]
    assert evs[0].response.value == {"$datetime": t.isoformat()}
    assert evs[1].response.value == r
    assert evs[2].response.value == u

    # A replay-like session serving the recorded values yields identical Python values.
    with use_session(ServingSession(run.fixture)):
        assert (now(), random(), uuid4()) == (t, r, u)


def test_random_respects_stdlib_seed() -> None:
    import random as stdlib_random

    stdlib_random.seed(1234)
    with record_run("seeded") as run:
        first = random()
    stdlib_random.seed(1234)
    assert random() == first
    assert calls(run.fixture)[0].response.value == first
