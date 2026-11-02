# Security and privacy

Fixtures are designed to be committed to repositories and attached to bug
reports. That makes two things important: secrets must not get into them, and
a fixture received from someone else must not be able to do anything except be
read.

## Everything stays local

Traceon has no backend, no account and no telemetry. Recording writes a file
you choose; replay, diff, inspect and Studio read local files. The only
outbound connection Traceon itself ever makes is `traceon import langfuse
--trace-id`, to the Langfuse host you configure.

## What is captured by default

`CapturePolicy()` defaults:

| Setting | Default | Notes |
|---|---|---|
| `tool_args` | `True` | Needed for argument matching with readable diffs. |
| `tool_results` | `True` | Needed to replay. |
| `model_prompts` | `"hash"` | Only `sha256` and size. Prompt changes are still detected; prompt text is not stored. |
| `model_results` | `True` | Needed to replay. |
| `run_input`, `run_output` | `True` | |
| `error_messages` | `True` | Redacted and truncated. |
| `stack_traces` | `False` | Stack traces can contain local paths and values. |
| `max_payload_bytes` | 256 KiB | Larger values are truncated (and then not servable). |

Presets: `CapturePolicy.full()` stores model prompts too;
`CapturePolicy.minimal()` keeps only structure, timings, hashes of prompts and
error types (useful for sharing, not for replay). Per-function overrides:
`@tool(capture_args=False | "hash", capture_result=False)`,
`@model(capture_prompt=True, capture_result=False)`.

## Redaction

Every captured payload, run input/output, metadata and error message passes
through a `RedactionPolicy` before it is written. Redaction works on the
canonical JSON value and never modifies your objects.

**Key rules.** If a dictionary key matches one of these patterns, its value is
replaced with `[REDACTED:sensitive_key]`. Keys are normalized first
(`apiKey`, `api-key` and `API_KEY` all become `api_key`), matching is
case-insensitive:

`password|passwd|passphrase`, `pwd`, `secret`, `*_token` / `token`,
`api_key`, `access_key`, `private_key`, `signing_key|encryption_key|master_key`,
`authorization`, `auth`, `cookie`, `session` / `session_id` / `session_key` /
`session_token`, `credential`, `csrf` / `xsrf`, `dsn`, `connection_string`.

**Value rules.** Every string (including dictionary keys) is scanned, and
matches are replaced with `[REDACTED:<rule>]`:

| Rule | Matches |
|---|---|
| `pem_private_key` | `-----BEGIN ... PRIVATE KEY-----` blocks |
| `jwt` | JSON Web Tokens (`eyJ...eyJ...`) |
| `anthropic_api_key` | `sk-ant-...` |
| `openai_api_key` | `sk-...`, `sk-proj-...`, `sk-svcacct-...` |
| `github_token` | `ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_` tokens, `github_pat_...` |
| `slack_token`, `slack_webhook` | `xox?-...`, `https://hooks.slack.com/...` |
| `stripe_key` | `sk_live_...`, `rk_test_...` |
| `google_api_key` | `AIza...` |
| `aws_access_key_id`, `aws_secret_access_key` | `AKIA...` / `ASIA...` ids; 40-character secrets next to an `aws ... secret` label |
| `bearer_token` | the token after `Bearer ` |
| `url_credentials` | the password in `scheme://user:password@host` |
| `secret_assignment` | the value in `password=...`, `api_key: ...`, `client_secret=...` and similar |
| `credit_card` | 13-19 digit numbers with a known issuer prefix that pass the Luhn check |

Email addresses are **not** redacted by default; enable with
`RedactionPolicy(redact_emails=True)`. Add your own rules without dropping the
defaults:

```python
from traceon import RedactionPolicy, record_run

policy = RedactionPolicy().with_rules(
    key_patterns=[r"customer_ssn"],
    value_patterns=[("internal_token", r"\bitk_[a-z0-9]{24}\b")],
)
with record_run("checkout", fixture="fixtures/checkout.json", redaction=policy):
    ...
```

What is recorded about redaction: each payload lists its `redacted_paths`; the
fixture lists the policy id, the rule names and every redacted path
(`fixture.redaction`). Secrets themselves are never stored, not even hashed
separately. A payload that was only partly redacted stays `captured`; a
payload replaced as a whole has `state: "redacted"` and cannot be replayed.

Call keys are computed from the **redacted** request, so replay matches calls
whose secrets differ between machines (for example a different API key in CI).

Values produced by `traceon.now()`, `traceon.random()` and `traceon.uuid4()`
are never redacted (they are not secrets, and replay needs them exactly).

**Limits of redaction.** Pattern-based redaction cannot recognize every secret
or every piece of personal data (names, addresses, free-text prompts). Treat it
as a safety net, not a guarantee: review fixtures before you commit or share
them, prefer `capture_args=False` / `capture_result=False` for calls that
handle sensitive data, and keep model prompts hashed unless you need them.

## Replay is fail-closed

- Real tools and models never run during replay, unless you explicitly provide
  an override for that call. A call that does not match the recording raises
  `UnmatchedCallError` instead of falling through to the real function -- a
  changed `send_payment(...)` call is reported, not executed.
- Network access is blocked for the whole replay (see below).
- A recorded failure is re-raised as `RecordedCallError`; responses that were
  not captured, redacted or truncated raise `ResponseNotCapturedError` rather
  than returning an invented value.

### Network blocking

While `replay_run(block_network=True)` (the default) is active, Traceon patches
`socket.socket.connect`, `connect_ex`, `socket.create_connection`,
`socket.getaddrinfo` and asyncio's selector / proactor `sock_connect`. Any
connection attempt -- including to localhost -- raises `NetworkBlockedError`
and is counted in `report.network_attempts`. Hosts can be allowed explicitly
with `allow_hosts=["localhost"]`, or the guard disabled with
`block_network=False` / `--allow-network`.

The guard is process-wide and reference-counted (restored when the last
replay ends). It does not cover third-party event loops such as uvloop, or C
extensions that open sockets without going through Python's `socket` module.
It is a strong safety net for replay, not a sandbox for untrusted code.

## Handling untrusted fixtures

A fixture is data. Loading one cannot execute code:

- JSON only -- no pickle, no YAML, no object hooks. Canonical values are plain
  data; tags such as `$type` or `$repr` are strings and are never turned back
  into objects.
- Size and depth are checked before validation: 50 MiB per file, 64 levels of
  JSON nesting, 50,000 events, 128 levels of span nesting. Non-regular files
  are refused.
- Validation is strict: unknown fields, duplicate event ids, dangling parents,
  parent cycles and unknown schema versions are errors.
- Paths derived from fixture content are never used to read or write files
  (`safe_join` refuses paths that leave their base directory).
- Studio binds to `127.0.0.1`, serves only the fixtures named on its command
  line, loads no external resources, renders fixture content as text (never as
  HTML) and sends a strict Content Security Policy.

What a malicious fixture *can* do is feed chosen values into your agent during
replay -- exactly as a malicious tool response could in production. If you
pass `decode=` functions to `@tool` / `@model`, they receive fixture data;
keep them to plain constructors (`Weather(**v)`), never `eval` or
deserializers of executable formats.

## Langfuse credentials

`traceon import langfuse` reads `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
from the environment, sends them only to the configured host over HTTPS
(plain HTTP only to loopback), does not follow redirects, caps the response at
20 MB and never includes credentials in error messages. Imported payloads go
through the default redaction policy. See [Langfuse import](langfuse.md).

## Reporting a vulnerability

Please report security issues privately through GitHub Security Advisories;
see [SECURITY.md](../SECURITY.md).
