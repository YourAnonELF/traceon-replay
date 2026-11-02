# Command-line interface

```text
traceon [--debug] [--color auto|always|never] [--version] COMMAND ...
```

| Exit status | Meaning |
|---|---|
| `0` | Success: the replay matched, the runs are equivalent, the fixtures are valid. |
| `1` | A divergence, a regression or a failed run. |
| `2` | Usage error, or a fixture that cannot be read or is invalid. |

Errors are printed as one concise message. `--debug` adds the full traceback.
`--color auto` disables color when output is piped or `NO_COLOR` is set.

## `traceon record`

Run an entry function once inside `record_run` and write the fixture.

```bash
traceon record --entry my_agent.main:run --out fixtures/run.json \
    --input '{"question": "What changed in v2?"}'
traceon record --entry agent.py:main --out fixtures/run.json --input @input.json
```

| Option | Description |
|---|---|
| `--entry MODULE:FUNC` | Function to run: a module path (`pkg.mod:func`, imported with the current directory on `sys.path`) or a file (`path/to/agent.py:func`). Async functions are awaited. |
| `--out FILE` | Fixture path (written atomically, also when the run fails). |
| `--input JSON\|@FILE` | A JSON object becomes keyword arguments; any other JSON value is passed as the single positional argument. Stored as the run input. |
| `--name TEXT` | Run name (default: the function name). |

The function's return value is stored as the run output.

## `traceon replay`

Call the entry function again inside `replay_run` and report divergences.

```bash
traceon replay fixtures/run.json --entry my_agent.main:run
traceon replay fixtures/run.json --entry my_agent.main:run --by-key
traceon replay fixtures/run.json --entry my_agent.main:run \
    --override 'tool:send_email={"status": "skipped"}' --save fixtures/replayed.json
```

| Option | Description |
|---|---|
| `--entry MODULE:FUNC` | Function to replay. |
| `--strict / --by-key` | Match calls in recorded order (default), or per `(kind, name)` queue for concurrent agents. |
| `--override NAME=JSON` | Serve this constant for a call name (`search`) or label (`tool:search`). Repeatable. |
| `--input JSON\|@FILE` | Input for the entry (default: the input stored in the fixture). |
| `--save FILE` | Write the replayed run as a new fixture (`mode: "replay"`), e.g. to diff it. |
| `--allow-network` | Do not block network access. |

Prints a summary (calls served, network status, time) and, on divergence, the
first divergence with path, expected and actual values plus every other
divergence. Exit status 1 on divergence.

## `traceon diff`

Compare two fixtures call by call without running code.

```bash
traceon diff fixtures/baseline.json fixtures/candidate.json
traceon diff base.json cand.json --ignore '$..request_id' --ignore '$.timestamp'
traceon diff base.json cand.json --markdown >> "$GITHUB_STEP_SUMMARY"
```

| Option | Description |
|---|---|
| `--json` | The full `DiffReport` as JSON. |
| `--markdown` | GitHub-flavored Markdown for CI summaries and PR comments. |
| `--ignore PATH` | JSONPath to ignore in requests, responses and output (`$.timestamp`, `$.items[*].id`, `$..request_id`). Repeatable. |
| `--no-output` | Do not compare the final run outputs. |

Exit status 1 if the runs are not equivalent.

## `traceon inspect`

```bash
traceon inspect fixtures/checkout.json            # span tree with timings, tokens, errors
traceon inspect fixtures/checkout.json --events   # flat table in sequence order
traceon inspect fixtures/checkout.json --json | jq .metrics
```

## `traceon validate`

```bash
traceon validate fixtures/*.json
traceon validate --strict fixtures/checkout.json
```

Checks schema, structure, size and depth limits. `--strict` also fails on
warnings such as missing call keys or inconsistent timings. Exit status 2 if
any fixture is invalid. Useful as a pre-commit or CI step for committed
fixtures.

## `traceon import langfuse`

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-...
traceon import langfuse --trace-id 9f3c... --out fixtures/incident.json
traceon import langfuse --file trace-export.json --out fixtures/incident.json
```

| Option | Description |
|---|---|
| `--trace-id TEXT` | Fetch the trace from the Langfuse public API. |
| `--file FILE` | Read a trace JSON saved earlier (`GET /api/public/traces/{id}` body). |
| `--out FILE` | Where to write the fixture. |
| `--host TEXT` | Langfuse host; otherwise `LANGFUSE_BASE_URL`, `LANGFUSE_HOST`, or `https://cloud.langfuse.com`. |

See [Langfuse import](langfuse.md) for the field mapping.

## `traceon studio`

```bash
traceon studio fixtures/
traceon studio base.json cand.json --port 8080 --no-open
```

A local web UI to browse runs (span tree, payloads, tokens, errors) and diffs.
It binds to `127.0.0.1:7777` by default, serves only the fixtures given on the
command line (files, or directories scanned for fixtures), loads nothing from
the internet and sends a strict Content Security Policy.

| Option | Description |
|---|---|
| `--host TEXT` | Interface to bind (default `127.0.0.1`). Binding to another interface exposes your fixtures to that network. |
| `--port INTEGER` | Port (default 7777; 0 picks a free port). |
| `--no-open` | Do not open a browser. |
