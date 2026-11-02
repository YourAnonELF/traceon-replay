# Concepts

## Runs, fixtures and events

A **run** is one execution of an agent, from `record_run(...)` (or
`traceon record`) to the end of the block. A **fixture** is the JSON document
describing that run. It is versioned (`schema_version: "traceon.fixture/v1"`),
self-contained, and safe to commit to a repository once you have reviewed it.

A fixture holds an ordered list of **events**:

| Kind | Produced by | Replayed? |
|---|---|---|
| `span` | the root of every run, `span("name")` blocks and decorators | No. Structure only: grouping, timings, the tree in `traceon inspect` and Studio. |
| `tool` | functions decorated with `@tool` | Yes |
| `model` | functions decorated with `@model` | Yes |
| `value` | `traceon.now()`, `traceon.random()`, `traceon.uuid4()` | Yes |

Tool, model and value events are **calls**. `Fixture.calls()` returns them in
order; a call's *position* (the `#3` in "call #3") is its 0-based index in that
list.

Each event stores its parent (`parent_id`), a global start `sequence`, start
and end times, status (`ok` / `error` / `cancelled`), the recorded **request**
(the function's bound arguments) and **response** (its return value), an error
description if it raised, and token usage / cost for model calls. See
[Fixture format](fixture-format.md).

## The interception point

`@tool`, `@model`, `span` and the value helpers never talk to a recorder or a
replayer directly. They describe the call (`CallSpec`: kind, name, arguments,
capture options, an optional `decode` function) and hand it, together with a
function that performs the real call, to the **active session**:

- no session: the real function runs, nothing else happens;
- `RecordSession`: the real function runs and an event is appended;
- `ReplaySession`: the real function does **not** run; the call is matched
  against the fixture and the recorded response is returned.

Sessions live in a `contextvars` context variable, so concurrent asyncio tasks
see the right session and the right parent span. Threads see it when started
with `contextvars.copy_context().run(...)`.

## Canonical values

Before Traceon stores or compares anything it converts it to **canonical JSON**:
dict keys become strings, tuples and sets become lists (sets sorted), `NaN` and
infinities become `{"$float": "nan"}`, bytes become `{"$bytes": <base64>}`,
datetimes, UUIDs, decimals and paths become tagged strings, dataclasses and
pydantic models become objects with a `$type`, and any other object becomes
`{"$repr": "<module.Type>"}` (never its `repr()`, which often contains memory
addresses). The conversion only produces data; loading a fixture can never
construct an object or run code.

During replay a tool or model returns the recorded canonical JSON. If your code
expects a richer type, pass `decode=` to the decorator:

```python
@tool(decode=lambda v: Weather(**v))
def get_weather(city: str) -> Weather: ...
```

## Call matching

Every call gets a **call key**: the SHA-256 of `{kind, name, request}` where
`request` is the canonical, redacted argument dict. The key is computed even
when the request itself is not stored (for example when model prompts are kept
as a hash only), so changed arguments are still detected.

During replay the n-th call is compared with the n-th recorded call:

1. same kind (`kind_mismatch` otherwise),
2. same name (`name_mismatch`),
3. same call key (`argument_mismatch`).

For an argument mismatch Traceon reports the first differing JSON path, the
expected and the actual value (`$.limit: expected 3, got 5`). If the recorded
request was stored only as a hash, the report says that the hash differs.

A call with no recorded counterpart is an `unexpected_call`. Recorded calls the
replay never made are reported as `missing_call` when the replay ends (pass
`allow_unconsumed=True` if a shorter run is expected). If both runs have an
output, the outputs are compared (`output_mismatch`), and the run status is
compared (`status_mismatch`).

### Order

- `order="strict"` (default): calls must happen in the recorded order.
- `order="by_key"`: one first-in-first-out queue per `(kind, name)`. Use it for
  agents that run tool calls concurrently with `asyncio.gather` or a thread
  pool, where the start order is not stable.

## Replay is fail-closed

When a call does not match, replay records the divergence and raises
`UnmatchedCallError` (a `DivergenceError`) **inside the agent**, at the call
site. The real tool or model is never called as a fallback. Other cases:

| Situation | What replay does |
|---|---|
| The recorded call raised | Raises `RecordedCallError` with the original type name and message. The original exception class is not reconstructed. |
| The recorded response was not captured, redacted or truncated | Raises `ResponseNotCapturedError` and records `response_unavailable`. Re-record with capture enabled, or supply an override. |
| The code opens a socket | Raises `NetworkBlockedError` (see [Security and privacy](security-and-privacy.md#network-blocking)). |
| The replay runs longer than `Limits.max_replay_seconds` (600 s) | Aborts. |

When the `replay_run` block exits with divergences, `DivergenceError` is raised
(unless `raise_on_divergence=False`, or another exception is already
propagating; in that case the report is attached to it as
`exc.traceon_report`). `handle.report` is a `ReplayReport` with every
divergence, counters (`calls_served`, `calls_overridden`, `network_attempts`
...) and `replayed`, a fixture describing the replay itself (save it with
`save_to=` and compare it with `traceon diff`).

## Replay modes and overrides

`overrides` maps a call name (`"search"`) or label (`"tool:search"`) to either
a constant or a callable. A callable receives the call's bound arguments as
keyword arguments.

- `mode="strict"` (default): an overridden call must still match the recorded
  arguments; only its response is replaced. Use this to inject a different
  tool result into an otherwise identical run.
- `mode="override"`: overridden calls skip argument matching. Use this for
  what-if experiments ("what if the model had answered X?"). Calls that are not
  overridden are still matched strictly.

```python
with replay_run(
    "fixtures/triage.json", mode="override", overrides={"classify": {"queue": "general"}}
) as handle:
    triage("T-311")
```

## Diffing two runs

`traceon diff` / `traceon.compare(baseline, candidate)` compares two fixtures
without running any code. It aligns the two call sequences with a
longest-common-subsequence over `(kind, name)`, so an inserted or removed call
does not shift every later call into a mismatch. The result is a `DiffReport`:
an alignment (same / changed / added / removed), every divergence, the first
divergence, and per-run metrics (calls, errors, tokens, cost, duration).
`ignore_paths` (`--ignore '$..request_id'`) excludes volatile fields.

## Determinism: what replay can and cannot promise

Replay is deterministic for the code **between** instrumented calls: given the
same recorded tool results, model responses, times, random numbers and ids,
your agent logic runs the same way every time. That is what makes a production
failure reproducible on a laptop and a regression test stable in CI.

It does not go further than that:

- **Model internals are not reconstructed.** Traceon stores what a model
  returned, not why. Replaying a model call returns the recorded response; it
  does not re-sample, and it cannot tell you what the model would answer to a
  changed prompt.
- **Live reruns are nondeterministic.** Running the agent again for real
  (`record_run` a second time) calls real models and tools, which can and will
  answer differently. Compare such runs with `traceon diff` and expect
  differences; use `--ignore` for fields you know are volatile.
- **Only instrumented boundaries are controlled.** Calls that bypass `@tool` /
  `@model`, direct use of `datetime.now()`, `random`, `uuid`, environment
  variables, files read from disk, or global state that differs between
  machines are not recorded. Network access is blocked during replay, which
  usually exposes forgotten boundaries quickly.
- **Concurrency.** With `order="by_key"` concurrent calls of the same name are
  matched first-in-first-out. If two concurrent calls with the same name start
  in a different order than when recorded, their arguments will not match.
- **Changed code paths.** After a change that makes the agent call something
  new, replay has nothing to serve for that call. That is reported as a
  divergence, by design. Supply an override, or re-record.
- **Imported traces** (Langfuse) are replayable only when your instrumented call
  names and argument shapes match what the other tool captured. See
  [Langfuse import](langfuse.md).
