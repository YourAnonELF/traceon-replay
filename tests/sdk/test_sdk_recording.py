"""End-to-end recording behavior: nesting, ordering, concurrency, errors, usage, limits."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from traceon.core.canonical import call_key
from traceon.core.errors import TraceonError
from traceon.core.io import dump_fixture, parse_fixture
from traceon.core.limits import Limits
from traceon.core.runtime import CallSpec, current_session, use_session
from traceon.core.schema import Event, Fixture, Usage
from traceon.sdk import CapturePolicy, RecordSession, model, record_run, span, tool


@tool
def add(a: int, b: int = 10) -> int:
    return a + b


@tool
async def aadd(a: int, b: int = 10) -> int:
    await asyncio.sleep(0)
    return a + b


@tool
def boom(msg: str) -> None:
    raise ValueError(msg)


def by_name(fx: Fixture, name: str) -> list[Event]:
    return [e for e in fx.events if e.name == name]


def assert_valid(fx: Fixture) -> None:
    assert parse_fixture(dump_fixture(fx)) == fx
    seqs = [e.sequence for e in fx.events]
    assert seqs == list(range(len(seqs)))


# --------------------------------------------------------------------------- structure


def test_root_span_parents_top_level_events() -> None:
    with record_run("root-test") as run:
        add(1)
    fx = run.fixture
    root = fx.events[0]
    assert (root.kind, root.name, root.parent_id, root.sequence) == ("span", "root-test", None, 0)
    assert by_name(fx, "add")[0].parent_id == root.event_id
    assert_valid(fx)


def test_nested_spans_and_parent_ids() -> None:
    with record_run("nest") as run, span("outer") as outer_id:
        with span("inner") as inner_id:
            add(1)
        add(2)
    fx = run.fixture
    outer, inner = by_name(fx, "outer")[0], by_name(fx, "inner")[0]
    assert outer.event_id == outer_id and inner.event_id == inner_id
    assert inner.parent_id == outer.event_id
    first, second = by_name(fx, "add")
    assert first.parent_id == inner.event_id
    assert second.parent_id == outer.event_id
    assert [e.name for e in fx.events] == ["nest", "outer", "inner", "add", "add"]
    assert_valid(fx)


def test_tool_calling_tool_nests_under_caller() -> None:
    @tool
    def outer(x: int) -> int:
        return add(x) * 2

    with record_run("nested-calls") as run:
        assert outer(1) == 22
    fx = run.fixture
    assert by_name(fx, "add")[0].parent_id == by_name(fx, "outer")[0].event_id


def test_event_fields_request_response_call_key_and_timing() -> None:
    with record_run("fields") as run:
        add(2, b=3)
    ev = by_name(run.fixture, "add")[0]
    assert ev.request.value == {"a": 2, "b": 3}
    assert ev.response.value == 5
    assert ev.call_key == call_key("tool", "add", {"a": 2, "b": 3})
    assert ev.status == "ok"
    assert ev.started_at.tzinfo is not None and ev.ended_at is not None
    assert ev.ended_at >= ev.started_at
    assert ev.duration_ms is not None and ev.duration_ms >= 0
    assert ev.event_id.startswith("ev_") and len(ev.event_id) == 15


def test_request_is_snapshotted_before_the_call_mutates_it() -> None:
    @tool
    def mutate(items: list[int]) -> int:
        items.append(99)
        return len(items)

    data = [1, 2]
    with record_run("snapshot") as run:
        mutate(data)
    assert by_name(run.fixture, "mutate")[0].request.value == {"items": [1, 2]}
    assert data == [1, 2, 99]


def test_side_effect_flag_and_custom_name() -> None:
    @tool("send_email", side_effect=True)
    def send(to: str) -> str:
        return "sent"

    with record_run("se") as run:
        send("a@example.com")
    ev = run.fixture.events[1]
    assert (ev.name, ev.side_effect) == ("send_email", True)


# --------------------------------------------------------------------------- concurrency


def test_thread_pool_with_copied_context_keeps_parents_and_unique_sequences() -> None:
    def worker(i: int) -> int:
        with span(f"job-{i}"):
            return add(i, b=i)

    with record_run("threads") as run, span("fanout") as fanout_id, ThreadPoolExecutor(8) as pool:
        futures = [pool.submit(contextvars.copy_context().run, worker, i) for i in range(40)]
        results = sorted(f.result() for f in futures)
    assert results == sorted(2 * i for i in range(40))
    fx = run.fixture
    assert_valid(fx)
    jobs = {e.event_id: e for e in fx.events if e.name.startswith("job-")}
    assert len(jobs) == 40
    assert all(j.parent_id == fanout_id for j in jobs.values())
    adds = by_name(fx, "add")
    assert len(adds) == 40
    for ev in adds:
        parent = jobs[ev.parent_id]  # type: ignore[index]
        assert parent.name == f"job-{ev.request.value['a']}"
        assert parent.sequence < ev.sequence


def test_raw_threads_without_context_are_not_recorded() -> None:
    seen: list[Any] = []

    def worker() -> None:
        seen.append(current_session())
        add(1)

    with record_run("raw-thread") as run:
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert seen == [None]
    assert by_name(run.fixture, "add") == []


async def test_asyncio_gather_keeps_parents() -> None:
    async def job(i: int) -> int:
        async with span(f"task-{i}"):
            await asyncio.sleep(0.001 * (5 - i))
            return await aadd(i)

    async with record_run("gather") as run, span("parallel") as parallel_id:
        results = await asyncio.gather(*(job(i) for i in range(5)))
    assert results == [10, 11, 12, 13, 14]
    fx = run.fixture
    assert_valid(fx)
    tasks = {e.event_id: e for e in fx.events if e.name.startswith("task-")}
    assert all(t.parent_id == parallel_id for t in tasks.values())
    for ev in by_name(fx, "aadd"):
        assert tasks[ev.parent_id].name == f"task-{ev.request.value['a']}"  # type: ignore[index]


# --------------------------------------------------------------------------- errors


def test_exception_recorded_and_reraised() -> None:
    with record_run("errors") as run, pytest.raises(ValueError, match="bad input"):
        boom("bad input")
    ev = by_name(run.fixture, "boom")[0]
    assert ev.status == "error"
    assert ev.error is not None
    assert ev.error.type == "builtins.ValueError"
    assert ev.error.message == "bad input"
    assert ev.error.stack is None
    assert ev.response.state == "not_captured"
    assert run.fixture.status == "ok"


def test_exception_in_span_marks_span_error() -> None:
    with record_run("span-error") as run, pytest.raises(KeyError), span("work"):
        raise KeyError("k")
    ev = by_name(run.fixture, "work")[0]
    assert ev.status == "error" and ev.error is not None and ev.error.type == "builtins.KeyError"


def test_error_message_secrets_redacted_and_long_messages_truncated() -> None:
    token = "ghp" + "_" + "Z" * 36
    with record_run("err-redact") as run:
        with pytest.raises(ValueError):
            boom(f"auth failed for {token}")
        with pytest.raises(ValueError):
            boom("x" * 20_000)
    first, second = by_name(run.fixture, "boom")
    assert first.error is not None and token not in first.error.message
    assert "[REDACTED:github_token]" in first.error.message
    assert second.error is not None and len(second.error.message.encode()) <= 4096
    assert second.error.message.endswith("bytes]")


def test_error_messages_off_and_stack_traces_on() -> None:
    with (
        record_run("no-msg", capture=CapturePolicy(error_messages=False)) as run,
        pytest.raises(ValueError),
    ):
        boom("private detail")
    err = by_name(run.fixture, "boom")[0].error
    assert err is not None and err.message == "[message not captured]"

    token = "xox" + "b-" + "1234567890-abcdef"
    with (
        record_run("stack", capture=CapturePolicy(stack_traces=True)) as run2,
        pytest.raises(ValueError),
    ):
        boom(token)
    err2 = by_name(run2.fixture, "boom")[0].error
    assert err2 is not None and err2.stack
    assert any("Traceback" in line for line in err2.stack)
    assert token not in "".join(err2.stack)


async def test_cancelled_error_recorded_as_cancelled() -> None:
    started = asyncio.Event()

    @tool
    async def slow() -> str:
        started.set()
        await asyncio.sleep(10)
        return "never"

    async with record_run("cancel") as run:
        task = asyncio.ensure_future(slow())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    ev = by_name(run.fixture, "slow")[0]
    assert ev.status == "cancelled"
    assert ev.error is not None and ev.error.type.endswith("CancelledError")


async def test_cancelled_run_status() -> None:
    async def body() -> None:
        async with record_run("cancel-run") as run:
            holder.append(run)
            await asyncio.sleep(10)

    holder: list[Any] = []
    task = asyncio.ensure_future(body())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fx = holder[0].fixture
    assert fx.status == "cancelled"
    assert fx.events[0].status == "cancelled"


# --------------------------------------------------------------------------- usage


def test_usage_auto_detected_openai_dict() -> None:
    @model
    def chat(prompt: str) -> dict[str, Any]:
        return {
            "text": "hi",
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }

    with record_run("usage-openai") as run:
        chat("hello")
    assert by_name(run.fixture, "chat")[0].usage == Usage(
        input_tokens=11, output_tokens=7, total_tokens=18
    )


def test_usage_auto_detected_anthropic_attribute() -> None:
    @model(provider="anthropic")
    def chat(prompt: str) -> SimpleNamespace:
        return SimpleNamespace(content="hi", usage=SimpleNamespace(input_tokens=5, output_tokens=3))

    with record_run("usage-anthropic") as run:
        chat("hello")
    ev = by_name(run.fixture, "chat")[0]
    assert ev.usage == Usage(input_tokens=5, output_tokens=3, total_tokens=8)
    assert ev.attributes["provider"] == "anthropic"


def test_usage_explicit_callable_and_cost() -> None:
    @model(usage=lambda r: {"input_tokens": r["n"], "output_tokens": 1, "cost": 0.25})
    def chat(prompt: str) -> dict[str, int]:
        return {"n": 4}

    @model(usage=lambda r: Usage(total_tokens=99))
    def chat2(prompt: str) -> str:
        return "x"

    with record_run("usage-explicit") as run:
        chat("a")
        chat2("b")
    u1 = by_name(run.fixture, "chat")[0].usage
    assert u1 is not None and u1.total_tokens == 5
    assert u1.cost is not None and (u1.cost.amount, u1.cost.kind) == (0.25, "measured")
    assert by_name(run.fixture, "chat2")[0].usage == Usage(total_tokens=99)


def test_usage_extractor_failure_never_breaks_the_call() -> None:
    def bad(_: Any) -> None:
        raise RuntimeError("nope")

    @model(usage=bad)
    def chat(prompt: str) -> str:
        return "ok"

    with record_run("usage-fail") as run:
        assert chat("x") == "ok"
    ev = by_name(run.fixture, "chat")[0]
    assert ev.status == "ok" and ev.usage is None
    assert ev.attributes["traceon.usage_error"] == "builtins.RuntimeError"


def test_tools_do_not_auto_detect_usage() -> None:
    @tool
    def t() -> dict[str, Any]:
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    with record_run("tool-usage") as run:
        t()
    assert by_name(run.fixture, "t")[0].usage is None


# --------------------------------------------------------------------------- capture flags


def test_capture_flags_produce_distinct_states() -> None:
    @tool(capture_args=False)
    def hidden(x: int) -> None:
        return None

    @tool
    def leak() -> str:
        return "sk-" + "ant-" + "api03-" + "k" * 30

    @model
    def chat(prompt: str) -> str:
        return "reply"

    with record_run("states") as run:
        hidden(1)
        leak()
        chat("hello")
    fx = run.fixture
    h, lk, c = by_name(fx, "hidden")[0], by_name(fx, "leak")[0], by_name(fx, "chat")[0]
    assert h.request.state == "not_captured"
    assert h.response.state == "captured" and h.response.value is None
    assert lk.response.state == "redacted"
    assert c.request.state == "hash_only" and c.request.sha256
    assert c.response.state == "captured"
    # call_key is computed even when the request is not stored.
    assert h.call_key == call_key("tool", "hidden", {"x": 1})
    assert c.call_key == call_key("model", "chat", {"prompt": "hello"})


def test_policy_and_per_call_overrides() -> None:
    @tool(capture_result=True)
    def keep() -> int:
        return 1

    @tool
    def drop() -> int:
        return 2

    @model(capture_prompt=True)
    def chat(prompt: str) -> str:
        return "r"

    policy = CapturePolicy(tool_results=False, model_results=False, run_input=False)
    with record_run("override", input={"q": 1}, capture=policy) as run:
        keep()
        drop()
        chat("p")
    fx = run.fixture
    assert by_name(fx, "keep")[0].response.value == 1
    assert by_name(fx, "drop")[0].response.state == "not_captured"
    chat_ev = by_name(fx, "chat")[0]
    assert chat_ev.request.value == {"prompt": "p"}
    assert chat_ev.response.state == "not_captured"
    assert fx.input.state == "not_captured"


def test_truncated_tool_result_keeps_full_hash() -> None:
    @tool
    def big() -> str:
        return "z" * 50_000

    with record_run("big-run", capture=CapturePolicy(max_payload_bytes=1024)) as run:
        assert big() == "z" * 50_000
    resp = by_name(run.fixture, "big")[0].response
    assert resp.truncated and resp.size_bytes == 50_002
    from traceon.core.canonical import content_hash

    assert resp.sha256 == content_hash("z" * 50_000)
    assert_valid(run.fixture)


# --------------------------------------------------------------------------- session lifecycle


def test_limits_stop_recording_but_not_execution() -> None:
    session = RecordSession("limited", limits=Limits(max_events=3))
    with use_session(session):
        results = [add(i) for i in range(5)]
    assert results == [10, 11, 12, 13, 14]
    fx = session.finish()
    assert len(fx.events) == 3
    assert any("event limit" in w for w in fx.warnings)


def test_finish_twice_raises_and_unfinished_events_are_cancelled() -> None:
    session = RecordSession("manual")
    cm = session.span(CallSpec(kind="span", name="dangling"))
    with use_session(session):
        cm.__enter__()
    fx = session.finish()
    ev = fx.events[0]
    assert ev.status == "cancelled" and ev.attributes["traceon.unfinished"] is True
    assert ev.ended_at is None
    with pytest.raises(TraceonError, match="already finished"):
        session.finish()


def test_calls_after_finish_pass_through() -> None:
    session = RecordSession("late")
    session.finish()
    with use_session(session):
        assert add(1) == 11


def test_direct_session_call_materializes_generators() -> None:
    session = RecordSession("gen")

    def gen() -> Any:
        yield from range(3)

    with use_session(session):
        it = session.call(CallSpec(kind="tool", name="g"), gen)
        assert list(it) == [0, 1, 2]
    ev = session.finish().events[0]
    assert ev.response.value == [0, 1, 2]
    assert ev.attributes["traceon.materialized"] == "generator"


def test_invalid_run_name_rejected() -> None:
    with pytest.raises(ValueError, match="run name"):
        RecordSession("")
    with pytest.raises(ValueError, match="run name"):
        record_run("x" * 513)
