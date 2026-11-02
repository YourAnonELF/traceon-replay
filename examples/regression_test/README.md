# Regression tests with pytest

A support-ticket triage agent (`triage_agent.py`) and a test module that replays
a committed fixture (`fixtures/triage_refund.json`). The tests run offline, need
no API keys, and never execute the agent's side effect (`assign_ticket` is
declared with `@tool(side_effect=True)`).

## Run the tests

```bash
pip install -e .            # registers the Traceon pytest plugin
pytest examples/regression_test
```

```text
....                                                                     [100%]
traceon: 4 replayed, 1 diverged
4 passed
```

(The "1 diverged" is the what-if test below, which expects a divergence.)

## What the tests show

| Test | Technique |
|---|---|
| `test_refund_ticket_goes_to_billing` | `@pytest.mark.traceon("fixtures/triage_refund.json")` wraps the whole test body in a strict replay. |
| `test_replay_matches_recording` | The `traceon_replay` fixture gives you the handle, so you can set the run output and inspect `run.report`. |
| `test_side_effects_never_execute_during_replay` | Real tool executions stay at zero during replay. |
| `test_model_change_would_reroute_the_ticket` | A what-if replay: override the model's answer and see which side effect would change (`tool:assign_ticket`, `$.priority`), without executing it. |

Fixture paths in markers and in `traceon_replay(...)` are resolved relative to
the test file.

## When behavior changes on purpose

Re-record the fixture, review the difference, and commit both the code and the
new fixture:

```bash
cp examples/regression_test/fixtures/triage_refund.json /tmp/before.json
python examples/regression_test/record.py
traceon diff /tmp/before.json examples/regression_test/fixtures/triage_refund.json
```

For fixtures that should be created automatically the first time a test runs,
use the `traceon_cassette` fixture with `pytest --traceon-record=once`; see
[docs/pytest.md](../../docs/pytest.md).
