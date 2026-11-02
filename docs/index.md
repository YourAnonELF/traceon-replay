# Traceon Replay documentation

Traceon Replay records what an AI agent did -- every tool call, model call and
nondeterministic value -- into a versioned JSON fixture, replays that run
offline with no real side effects, and reports the first point where a new run
behaves differently.

## Start here

| Page | What it covers |
|---|---|
| [Getting started](getting-started.md) | Install, instrument an agent, record and replay your first run (about 10 minutes). |
| [Concepts](concepts.md) | Fixtures, events, call matching, replay modes, and the limits of determinism. |
| [pytest](pytest.md) | `@pytest.mark.traceon`, the `traceon_replay` and `traceon_cassette` fixtures, CI summaries. |
| [CLI](cli.md) | `traceon record / replay / diff / inspect / validate / import / studio`. |

## Reference

| Page | What it covers |
|---|---|
| [Fixture format](fixture-format.md) | Field-by-field reference of `traceon.fixture/v1`; payload states. JSON Schema: [`schemas/fixture.schema.json`](../schemas/fixture.schema.json). |
| [Langfuse import](langfuse.md) | Turning a Langfuse trace into a fixture; field coverage matrix. |
| [Security and privacy](security-and-privacy.md) | Capture defaults, redaction rules, fail-closed replay, handling untrusted fixtures. |
| [Architecture](architecture.md) | Modules, the interception contract, data flow. |

## Project

| Page | What it covers |
|---|---|
| [Roadmap](roadmap.md) | What is planned after 0.1. |
| [FAQ](faq.md) | Common questions, including how Traceon relates to Langfuse and LangSmith. |

Runnable examples live in [`examples/`](../examples): a basic research agent,
a failed run that is reproduced and fixed, and a pytest regression suite.
