# FAQ

### How is this different from Langfuse or LangSmith?

Langfuse and LangSmith are observability and evaluation platforms: they collect
traces from running systems, store them, let you search, score and monitor
them. Traceon Replay does something narrower: it turns one run into a portable
fixture and lets you **re-execute your code** against it, offline, with every
external call answered from the recording, and tells you where your code's
behavior first changes.

They work together. Keep tracing in Langfuse; when a trace shows a failure,
import it (`traceon import langfuse`) or reproduce it with a local recording,
fix the code, and keep the fixture as a regression test. Traceon has no
backend and does not collect anything.

### How is this different from vcrpy?

vcrpy records HTTP interactions and replays them at the HTTP layer. Traceon
records at the level of your agent's tools and model calls: it knows that call
#3 is `tool:search` with `limit=5`, reports divergences in those terms, handles
`now()` / `random()` / `uuid4()`, stores token usage and cost, hashes prompts
instead of storing them, and blocks the network during replay. The cassette
idea comes from vcrpy (see Acknowledgements in the README).

### Does replay call the model?

No. Replay returns the recorded response. Nothing is re-sampled, no API key is
needed and no request leaves the machine.

### Then how can I test a prompt or model change?

A changed prompt is detected as a divergence (the prompt's hash differs), which
tells you the change has an effect but not what the model would say. To see
the new model output you must call the model: record a new run and compare it
with `traceon diff`. A live comparison mode is on the [roadmap](roadmap.md).
To explore "what if the model had answered X", replay with
`mode="override"` and an override for that model call.

### My agent uses `datetime.now()` / `uuid.uuid4()`. What happens?

Those values are not recorded, so the replayed run sees new values. If they
only end up in logs, nothing breaks. If they reach a tool argument or the
output, replay reports a divergence. Use `traceon.now()` / `traceon.uuid4()` /
`traceon.random()` in agent logic, or exclude the field when diffing
(`--ignore '$..request_id'`).

### Why did replay raise `NetworkBlockedError`?

Some code path talks to the network without going through a `@tool` or
`@model` function, so it is not recorded and cannot be replayed. Wrap that call
in a `@tool`, or, if the connection is intended (a local test database),
replay with `allow_hosts=["localhost"]`.

### Why `ResponseNotCapturedError`?

The matching recorded call exists, but its response was not stored (capture
disabled), was redacted as a whole, or was truncated because it exceeded
`max_payload_bytes`. Re-record with the response captured
(`capture_result=True`, a larger `CapturePolicy(max_payload_bytes=...)`), or
provide an override for that call.

### My agent runs tools concurrently. Do I need anything special?

Use `order="by_key"` (`--by-key`, or `@pytest.mark.traceon(..., order="by_key")`).
Calls are then matched per `(kind, name)` in first-in-first-out order instead
of by global position.

### Can I commit fixtures?

Yes, that is the intended use. Review them first: redaction removes common
secret formats but cannot recognize all personal data. Model prompts are
hashed by default. Run `traceon validate` on committed fixtures in CI.

### What does a fixture cost in size?

Proportional to the payloads you capture. Prompts are hashed by default; each
payload is capped at 256 KiB and a fixture at 50 MiB.

### Can I replay a fixture recorded on another OS or Python version?

Yes. Fixtures are plain JSON and the environment block is informational. Your
code's behavior may still differ across platforms (path separators, locale);
replay then reports it as a divergence.

### Does Traceon support JavaScript / TypeScript agents?

Not today. The fixture format is documented and has a JSON Schema
([`schemas/fixture.schema.json`](../schemas/fixture.schema.json)), so other
language SDKs are possible.

### Is the API stable?

0.x releases may change the Python API; changes are listed in the
[changelog](../CHANGELOG.md). The fixture format is versioned separately
(`traceon.fixture/v1`), and readers refuse versions they do not understand.
