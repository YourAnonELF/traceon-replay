# Langfuse import

If your agents are already traced with [Langfuse](https://langfuse.com), you
can turn a trace -- for example the one behind a production incident -- into a
Traceon fixture. You can then inspect it with `traceon inspect`, open it in
Studio, diff it against a local recording, and, when your instrumentation
matches, replay your code against it.

Traceon does not replace Langfuse. Langfuse remains the place where traces are
collected, searched, scored and monitored; Traceon takes a single run out of it
and makes it reproducible offline.

## Usage

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_HOST=https://cloud.langfuse.com     # or https://us.cloud.langfuse.com, or self-hosted

traceon import langfuse --trace-id 7f3c2a9e4b1d4c0e9a8b6d5c4e3f2a1b --out fixtures/incident.json
traceon inspect fixtures/incident.json
```

From a saved JSON file (no network):

```bash
traceon import langfuse --file trace.json --out fixtures/incident.json
```

From Python:

```python
from traceon.core.io import save_fixture
from traceon.integrations.langfuse import fetch_trace, import_trace

data = fetch_trace("7f3c2a9e...")  # dict: trace + observations
fixture = import_trace(data)  # traceon.Fixture, source="langfuse"
save_fixture(fixture, "fixtures/incident.json")
print(fixture.warnings)  # everything that was not mapped
```

`import_trace(data, *, redaction=None, capture_io=True, limits=DEFAULT_LIMITS)`
accepts:

- the body of `GET /api/public/traces/{traceId}` (trace fields plus an
  `observations` list),
- the dict returned by `fetch_trace`,
- a `GET /api/public/v2/observations?traceId=...` response (`{"data": [...],
  "meta": {...}}`); trace-level fields are then rebuilt from the trace context
  repeated on each observation.

## How the API is called

`fetch_trace(trace_id, *, host=None, public_key=None, secret_key=None, timeout=30)`:

- host: `host=`, else `LANGFUSE_BASE_URL`, else `LANGFUSE_HOST`, else
  `https://cloud.langfuse.com` (same precedence as the Langfuse Python SDK);
- credentials: `public_key=` / `secret_key=`, else `LANGFUSE_PUBLIC_KEY` /
  `LANGFUSE_SECRET_KEY`, sent with HTTP basic auth;
- requests `GET /api/public/traces/{traceId}`; if that endpoint is unavailable
  or returns no observations, lists them through
  `GET /api/public/v2/observations` with cursor pagination;
- standard library only (`urllib`), per-request timeout, at most **20 MB** read
  in total;
- redirects are not followed (the credentials are never sent to a second URL);
  plain `http://` is refused except for `localhost` / loopback addresses; hosts
  with embedded credentials are refused;
- errors are raised as `LangfuseError` with a short message (status code and a
  hint). Credentials and the `Authorization` header never appear in messages.

## Mapping

| Langfuse | Traceon |
|---|---|
| Trace | Root `span` event (`event_id: "trace"`), fixture `name`, `created_at`, `input`, `output` |
| Observation | One event, ordered by `startTime`, then observation id |
| `GENERATION`, `EMBEDDING` | `model` event |
| `TOOL`, `RETRIEVER` | `tool` event |
| `SPAN`, `EVENT`, `AGENT`, `CHAIN`, `EVALUATOR`, `GUARDRAIL`, unknown types | `span` event |
| `parentObservationId` | `parent_id` (top-level observations are children of the root span) |
| `input` / `output` | `request` / `response` payloads, redacted with the default `RedactionPolicy` |
| `level: ERROR` + `statusMessage` | `status: "error"`, `error.type = "langfuse.ObservationError"`, redacted message. An error on a top-level observation also fails the run. |

## Field coverage

**Supported** -- mapped onto the fixture structure:

| Field | Becomes |
|---|---|
| trace `id`, `timestamp`, `name`, `input`, `output` | `run_id` / `fixture_id`, `created_at`, `name`, run payloads |
| observation `id`, `type`, `name`, `startTime`, `endTime`, `parentObservationId` | event identity, kind, name, timing, tree |
| observation `input`, `output` | `request`, `response` (+ `call_key` for tool / model events) |
| observation `level`, `statusMessage` | `status`, `error` (ERROR), otherwise `attributes.level` / `attributes.status_message` |
| `usageDetails` (`input`, `output`, `total`; also `prompt_tokens` / `completion_tokens` style keys) | `usage.input_tokens / output_tokens / total_tokens`; the full dict in `attributes.usage_details` |
| `costDetails.total`, `totalCost`, `calculatedTotalCost` | `usage.cost` (`kind: "estimated"`, `price_source: "langfuse"`); the full dict in `attributes.cost_details` |

**Partial** -- kept, but without an equivalent in replay semantics:

| Field | Where it goes / caveat |
|---|---|
| deprecated `usage` object (`input`, `output`, `total`, `unit`) | Used when `usageDetails` is absent, only for `unit` `TOKENS` (other units produce a warning) |
| flat `promptTokens`, `completionTokens`, `totalTokens` (older API) | Fallback token counts |
| `model`, `modelParameters`, `modelId`, `internalModelId`, `promptId`, `promptName`, `promptVersion`, `completionStartTime`, `timeToFirstToken`, `usagePricingTierName`, `inputPrice`, `outputPrice`, `totalPrice`, `version`, `environment` | `event.attributes` (informational) |
| observation `metadata` | `event.attributes.metadata`, redacted |
| trace `sessionId`, `userId`, `release`, `version`, `tags`, `environment`, `public`, `bookmarked`, `htmlPath`, `projectId`, `externalId`, `createdAt`, `updatedAt`, `latency`, `totalCost`, `metadata` | `fixture.metadata["langfuse"]` (metadata redacted) |
| `@observe`-style inputs `{"args": [...], "kwargs": {...}}` | Imported as-is. Traceon records named arguments, so replaying such calls against `@tool` functions only matches if the argument shape is the same. |
| run status | Langfuse traces have no status; Traceon derives `error` only from top-level observations at level `ERROR`. |

**Unsupported** -- listed in `fixture.warnings`, not imported:

| Field | Why |
|---|---|
| trace `scores` | Evaluations are not part of the fixture format. |
| observation `createdAt`, `updatedAt`, `projectId`, `bookmarked`, `public` | Bookkeeping without a fixture equivalent. |
| any field not listed on this page | Reported as unknown, so new Langfuse fields are never dropped silently. |

Malformed input is handled without failing the import where possible, each
time with a warning: missing or duplicate observation ids, parents that are not
in the trace, parent cycles, nesting deeper than 128 levels, unparseable or
naive timestamps (interpreted as UTC), `endTime` before `startTime`, names
longer than 512 characters. Inputs that cannot form a fixture (no trace id, no
timestamp anywhere, more than 50,000 observations) raise `FixtureError`.

## Replaying an imported trace

An imported fixture replays like any other when your code makes the same calls
with the same names and arguments as the observations. In practice:

- name your `@tool` / `@model` functions like the Langfuse observations (or
  pass `name=` to the decorator),
- make sure the observation `input` is the same dict of arguments your
  function receives (for generations this is usually the message list --
  wrap the provider call in a function whose single argument has that shape),
- use `traceon inspect` on the imported fixture to see names and requests,
- if shapes differ, use the imported fixture for inspection and `traceon diff`
  against a local recording instead, or replay with `mode="override"` and
  overrides for the calls that do not line up.

Langfuse does not record nondeterministic values such as `now()` or `uuid4()`,
so agents that use them diverge on the first such call; replay those with
overrides or treat the imported run as read-only.

## Privacy

Imported payloads pass through the same redaction policy as recordings (see
[Security and privacy](security-and-privacy.md)). Langfuse traces often
contain full prompts and user data; review the fixture before committing or
sharing it. `capture_io=False` imports structure, timings, usage and call keys
without any input or output values.
