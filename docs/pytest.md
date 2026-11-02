# pytest

Installing `traceon-replay` registers a pytest plugin (entry point
`traceon = traceon.pytest_plugin`). It provides a marker, two fixtures, an
assertion helper, a command-line option and a GitHub Actions job summary.

Relative fixture paths are always resolved against the **test file's
directory**, not the current working directory.

## `@pytest.mark.traceon` -- replay around a whole test

```python
import pytest
from support_agent import triage


@pytest.mark.traceon("fixtures/triage_refund.json")
def test_refund_ticket_goes_to_billing():
    assert triage("T-311")["queue"] == "billing"
```

The test body runs inside `replay_run(...)`. Keyword arguments are passed
through: `@pytest.mark.traceon("f.json", order="by_key", mode="override",
overrides={"classify": {...}}, allow_unconsumed=True)`.

If the replay diverges, the test fails and its report contains a
**traceon: first divergence** section with the call label, JSONPath, expected
and actual value.

## `traceon_replay` -- replay inside a test

A factory: `traceon_replay(path, **replay_kwargs)` returns the replay handle.
Use it when you want to set the run output or inspect the report:

```python
def test_replay_matches_recording(traceon_replay):
    with traceon_replay("fixtures/triage_refund.json") as run:
        run.set_output(triage("T-311"))
    assert run.report.calls_served == 3
```

A missing fixture fails the test with a hint to record it.

## `traceon_cassette` -- record once, replay afterwards

vcrpy-style: the first run records, later runs replay.

```python
def test_research(traceon_cassette):
    with traceon_cassette as run:  # cassettes/<test module>/<test name>.json
        run.set_output(research_agent("What changed in v2?"))


def test_research_custom(traceon_cassette):
    with traceon_cassette("fixtures/research.json", input={"q": "v2"}, order="by_key") as run:
        run.set_output(research_agent("v2"))
```

Whether it records or replays is controlled by `--traceon-record`:

| Value | Behavior |
|---|---|
| `none` (default) | Always replay. A missing fixture fails the test. Use this in CI. |
| `once` | Record fixtures that do not exist yet, replay the others. |
| `all` | Re-record every fixture (markers and cassettes). Review the result with `traceon diff` before committing. |

```bash
pytest --traceon-record=once
```

The default can also be set in configuration: `traceon_record = "once"` under
`[tool.pytest.ini_options]`.

Recording calls the real tools and models, so it needs whatever credentials
and network access your agent normally needs. Replaying needs neither.

## `assert_no_divergence`

```python
from traceon.pytest_plugin import assert_no_divergence


def test_candidate_matches_baseline():
    report = traceon.compare(load_fixture("baseline.json"), load_fixture("candidate.json"))
    assert_no_divergence(report)
```

Accepts a `ReplayReport` or a `DiffReport`; the assertion message is the
rendered first divergence.

## GitHub Actions

When `$GITHUB_STEP_SUMMARY` is set, the plugin appends a Markdown table of
every Traceon test (fixture, mode, result, first divergence) to the job
summary. Nothing else is needed:

```yaml
- run: uv run pytest
```

At the end of the terminal output the plugin prints one line such as
`traceon: 12 replayed, 0 diverged`.

## Async tests

Replay handles are async context managers as well:

```python
async def test_async_agent(traceon_replay):
    async with traceon_replay("fixtures/fanout.json", order="by_key") as run:
        run.set_output(await fanout_agent(["a", "b", "c"]))
```

(Requires an async test runner such as `pytest-asyncio` or `anyio`.)

## Tips

- Commit fixtures next to the tests that use them and review fixture diffs in
  pull requests like any other test data. Run `traceon validate` on them in CI.
- Keep one behavior per fixture. Small fixtures produce precise divergences.
- When behavior changes on purpose, re-record with `--traceon-record=all` and
  commit the code change and the fixture change together.
