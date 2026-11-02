# Fixture format (`traceon.fixture/v1`)

A fixture is one UTF-8 JSON object. The pydantic models in
`traceon/core/schema.py` are the source of truth; a generated JSON Schema is
committed at [`schemas/fixture.schema.json`](../schemas/fixture.schema.json)
for tools outside Python (regenerate with `python scripts/gen_schema.py`).

Unknown fields are rejected everywhere (`additionalProperties: false`), so a
typo or a fixture from an incompatible version fails validation instead of
being silently ignored. Check files with `traceon validate`.

## Top level: `Fixture`

| Field | Type | Description |
|---|---|---|
| `schema_version` | `"traceon.fixture/v1"` | Format version. Readers refuse any other value. |
| `fixture_id` | string | Identifier of this document (`fx_...` for recordings, `lf_<trace id>` for Langfuse imports). |
| `run_id` | string | Identifier of the run. A replay gets its own `run_id`. |
| `name` | string, 1-512 chars | Run name given to `record_run` (or the Langfuse trace name). |
| `created_at` | ISO 8601 datetime | When the run started. |
| `source` | `"traceon"` \| `"langfuse"` | Who produced the fixture. |
| `mode` | `"record"` \| `"replay"` | `replay` for fixtures written by `replay_run(save_to=...)` / `traceon replay --save`. |
| `status` | `"ok"` \| `"error"` \| `"cancelled"` | Outcome of the run as a whole. |
| `environment` | `Environment` | Where it was recorded. |
| `redaction` | `RedactionInfo` | Which redaction policy ran and what it removed. |
| `input` | `Payload` | Run input (`record_run(input=...)`). |
| `output` | `Payload` | Run output (`handle.set_output(...)`). |
| `error` | `ErrorInfo` \| null | Run-level error when `status` is `error`. |
| `events` | list of `Event` | Ordered by `sequence`. |
| `metadata` | object | Free-form, redacted (`record_run(metadata=...)`; Langfuse trace details under `"langfuse"`). |
| `warnings` | list of strings | Non-fatal notes, e.g. Langfuse fields that could not be mapped. |

Structural rules checked on load: `event_id`s are unique, `sequence` strictly
increases, every `parent_id` refers to an event in the fixture, and parent
chains are acyclic and at most 128 levels deep.

## `Event`

| Field | Type | Description |
|---|---|---|
| `event_id` | string | Unique within the fixture. |
| `parent_id` | string \| null | Enclosing span or call; `null` only for the root span. |
| `sequence` | integer >= 0 | Global start order within the run. |
| `kind` | `"span"` \| `"tool"` \| `"model"` \| `"value"` | See [Concepts](concepts.md#runs-fixtures-and-events). |
| `name` | string, 1-512 chars | Tool / model / span name; `now`, `random` or `uuid4` for values. |
| `started_at`, `ended_at` | datetime, datetime \| null | Wall-clock timestamps (UTC). |
| `duration_ms` | number \| null | Measured duration. |
| `status` | `"ok"` \| `"error"` \| `"cancelled"` | `cancelled` for `asyncio.CancelledError`. |
| `side_effect` | boolean | Declared with `@tool(side_effect=True)`. |
| `call_key` | string \| null | `sha256:` of `{kind, name, redacted canonical request}`; set for calls. |
| `request` | `Payload` | Bound arguments (`self` / `cls` removed, defaults applied). |
| `response` | `Payload` | Return value. Generators are stored as the list of produced items. |
| `error` | `ErrorInfo` \| null | Set when the call raised. |
| `usage` | `Usage` \| null | Tokens and cost (model calls). |
| `attributes` | object | Extra data: `provider` for models, span attributes, Langfuse fields. |

## `Payload` and its states

Every stored value is wrapped in a `Payload`. Three different kinds of "no
value" are deliberately kept apart:

| `state` | `value` | Meaning | Servable during replay |
|---|---|---|---|
| `captured` | the canonical JSON value (may be `null`) | The value was stored. `null` means the function really returned `None`. | Yes, unless `truncated` |
| `hash_only` | absent | Only `sha256` (and `size_bytes`) is stored: enough to detect a change, not to reproduce the content. Default for model prompts. | No |
| `redacted` | absent | The value was captured and then replaced **as a whole** by the redaction policy. | No |
| `not_captured` | absent | Capture was disabled for this payload. | No |

Other fields:

| Field | Description |
|---|---|
| `sha256` | `sha256:<hex>` of the canonical JSON of the full, redacted value (before truncation). |
| `size_bytes` | Size of that canonical JSON in bytes. |
| `truncated` | `true` if the stored value was cut to `CapturePolicy.max_payload_bytes` (default 256 KiB). Strings end with `…[truncated N of M bytes]`; objects and arrays become `{"$truncated": {"type", "size_bytes", "length", "preview"}}`. Truncated payloads are never served during replay. |
| `redacted_paths` | JSONPaths inside the value that redaction replaced (`$.api_key`, `$.messages[2].content`). A partially redacted value stays `captured`. |

Canonical value tags that can appear inside `value`: `$float`, `$bytes`,
`$datetime`, `$date`, `$time`, `$timedelta`, `$decimal`, `$uuid`, `$path`,
`$type` (dataclass / pydantic model), `$repr` (other objects, type name only),
`$cycle`, `$truncated`. See [Concepts](concepts.md#canonical-values).

## `ErrorInfo`

| Field | Description |
|---|---|
| `type` | Qualified exception type, e.g. `builtins.ValueError`. |
| `message` | Redacted, possibly truncated message. A fixed "not captured" note when `CapturePolicy.error_messages=False`. |
| `stack` | Formatted frames, only when `CapturePolicy.stack_traces=True` (off by default). |

## `Usage` and `Cost`

| Field | Description |
|---|---|
| `input_tokens`, `output_tokens`, `total_tokens` | Integers or null. |
| `cost.amount`, `cost.currency` | Number, default currency `USD`. |
| `cost.kind` | `measured` (reported by the provider) or `estimated` (computed from a price table; Langfuse costs are always `estimated`). |
| `cost.price_source` | Where an estimated price came from. |

## `Environment`

`python`, `platform`, `traceon_version` and `adapters` (versions of
instrumented libraries or importers). Informational only; replay does not
require the same environment.

## `RedactionInfo`

| Field | Description |
|---|---|
| `policy_id` | `RedactionPolicy.policy_id` (`default` unless you set one). |
| `rules` | Names of the active rules, e.g. `sensitive_key`, `openai_api_key`. Never the secrets. |
| `fields_omitted` | Distinct JSONPaths that were redacted anywhere in the run. |

## Limits

Readers enforce these before validation (`traceon.core.limits.Limits`):

| Limit | Default |
|---|---|
| Fixture file size | 50 MiB |
| Single payload (stored, after truncation) | 256 KiB |
| Events per fixture | 50,000 |
| JSON nesting depth of any value | 64 |
| Span depth | 128 |
| Replay wall-clock time | 600 s |

## Minimal example

```json
{
  "schema_version": "traceon.fixture/v1",
  "fixture_id": "fx_4c3540f794a8",
  "run_id": "run_a81789a4a04e",
  "name": "ticket-triage",
  "created_at": "2026-11-02T16:07:16.265340Z",
  "status": "ok",
  "input": {"state": "captured", "value": {"ticket_id": "T-311"}, "sha256": "sha256:7ac7...", "size_bytes": 21},
  "events": [
    {"event_id": "ev_1", "sequence": 0, "kind": "span", "name": "ticket-triage",
     "started_at": "2026-11-02T16:07:16.265340Z"},
    {"event_id": "ev_2", "parent_id": "ev_1", "sequence": 1, "kind": "tool", "name": "fetch_ticket",
     "started_at": "2026-11-02T16:07:16.265900Z", "call_key": "sha256:...",
     "request": {"state": "captured", "value": {"ticket_id": "T-311"}},
     "response": {"state": "captured", "value": {"id": "T-311", "subject": "Charged twice"}}}
  ]
}
```

Fields with defaults (`source`, `mode`, `environment`, `redaction`, `metadata`,
`warnings`, `attributes` ...) may be omitted. A complete recorded fixture is in
[`examples/regression_test/fixtures/triage_refund.json`](../examples/regression_test/fixtures/triage_refund.json).

## Compatibility

`v1` is the first public format. A future incompatible change gets a new
`schema_version`; readers fail with a clear message instead of guessing.
Because unknown fields are rejected, a fixture written by a newer Traceon may
not load in an older one; every format change is listed in the
[changelog](../CHANGELOG.md).
