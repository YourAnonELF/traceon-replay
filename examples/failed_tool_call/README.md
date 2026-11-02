# Failed run: reproduce, fix, verify

An order-status agent fails in "production": its last step parses the carrier's
ETA with a `strptime` format that has no UTC offset, and the carrier sends
`2026-11-05T14:00:00+01:00`.

```text
ValueError: unconverted data remains: +01:00
```

This example records that failing run, replays it offline to reproduce the
error, replays the fixed code against the exact same tool results, and then
shows how a deliberately changed tool argument is reported as a divergence.

All tools and the model are local stand-ins: no network, no API keys.

## Run it

```bash
python examples/failed_tool_call/record.py
python examples/failed_tool_call/replay.py
```

`record.py`:

```text
run failed as expected: ValueError: unconverted data remains: +01:00
recorded status=error with 3 calls -> fixtures/eta_parse_failure.json
recorded error: builtins.ValueError: unconverted data remains: +01:00
```

`replay.py`:

```text
1) replay the buggy version
   reproduced offline: ValueError: unconverted data remains: +01:00
2) replay the fixed version against the same recording
   calls served from the fixture: 3/3
   divergences: ['status_mismatch']
   - status_mismatch: Run status diverged: the recording finished 'error' (...) but the replay finished 'ok'
   eta_utc = 2026-11-05T13:00:00+00:00
3) replay a refactor that changes a tool argument
   first divergence: argument_mismatch at tool:lookup_order $.order_id: expected 'A-1042', got 'a-1042'
real tool/model executions across all replays: 0
```

## What each step shows

1. **Reproduce.** `run_agent_v1` is replayed against the fixture. `lookup_order`,
   `track_shipment` and `draft_reply` are answered from the recording, so the
   parser sees exactly the string that broke production, and the same
   `ValueError` is raised -- on a laptop, offline, every time.
2. **Verify the fix.** `run_agent` (fixed parser) is replayed with
   `raise_on_divergence=False`. All three calls match the recording. The only
   divergence is `status_mismatch`: the recording ended in `error`, the replay
   ends in `ok`. That is the evidence that the fix resolves the failure without
   changing anything the agent sends to its tools or its model.
3. **Catch a behavior change.** `run_agent_refactored` lowercases the order id
   before calling `lookup_order`. Strict replay fails closed at that call: the
   real tool is never run, and the divergence names the call
   (`tool:lookup_order`), the JSON path (`$.order_id`) and both values.

## Same thing from the command line

```bash
cd examples/failed_tool_call
traceon replay fixtures/eta_parse_failure.json --entry agent:run_agent_v1         # exit 0: the recorded ValueError is reproduced
traceon replay fixtures/eta_parse_failure.json --entry agent:run_agent_refactored # exit 1, prints the divergence
```
