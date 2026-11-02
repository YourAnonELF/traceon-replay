# Getting started

This walk-through takes you from installation to a replayed run and a reported
divergence. It uses no network and no API keys.

## 1. Install

Traceon needs Python 3.10 or newer.

```bash
pip install "git+https://github.com/YourAnonELF/traceon-replay"
# or
uv add "git+https://github.com/YourAnonELF/traceon-replay"
```

The package installs the `traceon` command, the `traceon` Python package and a
pytest plugin. Runtime dependencies are pydantic, click and rich.

## 2. Instrument the agent

Decorate the functions through which your agent touches the outside world:

- `@tool` for tools (search, HTTP APIs, databases, file writes ...),
- `@model` for model calls (OpenAI, Anthropic, local models ...),
- `traceon.now()`, `traceon.random()`, `traceon.uuid4()` instead of
  `datetime.now()`, `random.random()`, `uuid.uuid4()` in agent logic,
- `span("name")` to group steps (optional, for readability).

Save this as `my_agent.py`:

```python
from traceon import model, now, span, tool


@tool
def search(query: str, limit: int = 3) -> list[str]:
    # In a real agent: call a search API.
    return [f"{query} result {i}" for i in range(limit)]


@tool(side_effect=True)
def send_report(to: str, text: str) -> dict:
    # In a real agent: send an email. Never executed during replay.
    return {"status": "sent", "to": to}


@model(provider="demo")
def summarize(question: str, hits: list[str]) -> str:
    # In a real agent: call an LLM.
    return f"{question}: found {len(hits)} sources"


def run(question: str) -> dict:
    with span("research"):
        hits = search(question)
        summary = summarize(question, hits)
    send_report("team@example.com", summary)
    return {"summary": summary, "at": now().isoformat()}
```

Outside a Traceon session the decorators only add a context-variable lookup;
the functions behave exactly as before.

## 3. Record a run

```python
from traceon import record_run
from my_agent import run

with record_run(
    "research", fixture="fixtures/research.json", input={"question": "replay testing"}
) as handle:
    handle.set_output(run("replay testing"))
```

or, without writing any code:

```bash
traceon record --entry my_agent:run --out fixtures/research.json \
    --input '{"question": "replay testing"}'
```

Look at what was recorded:

```bash
traceon inspect fixtures/research.json
```

The fixture is a plain JSON file. Model prompts are stored only as a hash by
default; secrets matching the redaction rules are replaced before anything is
written. See [Security and privacy](security-and-privacy.md).

## 4. Replay it

```python
from traceon import replay_run
from my_agent import run

with replay_run("fixtures/research.json") as handle:
    handle.set_output(run("replay testing"))

print(handle.report.ok, handle.report.calls_served)
```

or:

```bash
traceon replay fixtures/research.json --entry my_agent:run
```

During the replay `search`, `summarize` and `send_report` do not run. Their
recorded results are returned instead, `now()` returns the recorded time, and
every socket connection is refused.

## 5. Make it diverge

Change `limit: int = 3` to `limit: int = 5` in `my_agent.py` and replay again:

```bash
traceon replay fixtures/research.json --entry my_agent:run
```

Replay stops at the first call that does not match the recording and exits
with status 1:

```text
x replay diverged  4 divergences  run
  calls    0/4 served from recording
  network  blocked

x first divergence  argument_mismatch - call #0 - tool:search
    path      $.limit
    expected  3
    actual    5

  other divergences (3)
  call #1 model:summarize   missing_call  ...
  call #2 tool:send_report  missing_call  ...
  call #3 value:now         missing_call  ...
```

The changed call is never forwarded to the real tool. Because the agent stops
there, the remaining recorded calls are reported as `missing_call`. In Python
the same situation raises `traceon.UnmatchedCallError` inside the agent code.
Call numbers are 0-based positions in the list of recorded calls.

## 6. Keep it as a regression test

```python
import pytest
from my_agent import run


@pytest.mark.traceon("fixtures/research.json")
def test_research_agent():
    assert "replay testing" in run("replay testing")["summary"]
```

```bash
pytest
```

See [pytest](pytest.md) for the `traceon_replay` and `traceon_cassette`
fixtures and for GitHub Actions summaries.

## Next steps

- Read [Concepts](concepts.md) to understand call matching and replay modes.
- Open fixtures and diffs in the local web UI: `traceon studio fixtures/`.
- Already use Langfuse? Import a production trace: [Langfuse import](langfuse.md).
- Work through the [examples](../examples).
