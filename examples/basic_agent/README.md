# Basic agent: record once, replay offline

A three-step research agent: `search` (tool) → `fetch_document` (tool, twice) →
`summarize` (model). It also takes a request id from `traceon.uuid4()` and a
timestamp from `traceon.now()`, so you can see nondeterministic values being
recorded and replayed.

The tools and the model are local, deterministic stand-ins. No network access,
no API keys.

## Run it

From the repository root, with Traceon installed (`pip install -e .` or
`uv sync`):

```bash
python examples/basic_agent/record.py
python examples/basic_agent/replay.py
```

`record.py` writes `fixtures/research_run.json`:

```text
recorded 6 calls (9 events) -> fixtures/research_run.json
real tool/model executions: {'fetch_document': 2, 'search': 1, 'summarize': 1}
  #1  value uuid4           request=captured
  #2  value now             request=captured
  #4  tool  search          request=captured
  #5  tool  fetch_document  request=captured
  #6  tool  fetch_document  request=captured
  #8  model summarize       request=hash_only
```

`replay.py` runs the same agent code against the fixture:

```text
replay ok: True
calls served from the fixture: 6/6
real tool/model executions during replay: 0
network connection attempts blocked: 0
request id (replayed): 666e5f31-e0df-4c88-a411-702f70fb9f5c
answer: Agent runs depend on model sampling, ...
```

The request id and timestamp are the recorded ones, the tools and the model
never run, and the network is blocked for the duration of the replay.

Note `request=hash_only` on the model call: by default Traceon stores model
prompts only as a SHA-256 hash. That is enough to detect a changed prompt
during replay without keeping the prompt text in the fixture. Use
`CapturePolicy(model_prompts=True)` to store prompts in full.

## Look at the fixture

```bash
traceon inspect examples/basic_agent/fixtures/research_run.json
traceon studio examples/basic_agent/fixtures/
```

## Replay from the command line

`traceon replay` imports an entry function and calls it with the recorded run
input (`{"question": ...}` becomes `question=...`):

```bash
cd examples/basic_agent
traceon replay fixtures/research_run.json --entry agent:run_agent
```

## What to change next

- Edit `search` in `agent.py` to use `limit=3` and replay again: the replay
  stops at the first changed call and prints the argument path that differs.
- Wrap your own agent's tool and model functions with `@tool` / `@model` and
  record a run the same way.
