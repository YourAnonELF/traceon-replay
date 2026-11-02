from __future__ import annotations

import json

import pytest

MARKER_TEST = """
import pytest
from agent import run

@pytest.mark.traceon("fixtures/run.json")
def test_agent():
    assert run("weather").startswith("SUMMARIZE")
"""


def _write(pytester: pytest.Pytester, source: str, name: str = "test_agent") -> None:
    pytester.makepyfile(**{name: source})


class TestMarker:
    def test_record_once_then_replay(self, pytester, run_tests):
        _write(pytester, MARKER_TEST)
        first = run_tests("--traceon-record=once")
        first.assert_outcomes(passed=1)
        fixture = pytester.path / "fixtures" / "run.json"
        assert fixture.exists()
        data = json.loads(fixture.read_text(encoding="utf-8"))
        assert [e["name"] for e in data["events"] if e["kind"] != "span"] == ["search", "llm"]
        first.stdout.fnmatch_lines(["traceon: 1 recorded"])

        second = run_tests()
        second.assert_outcomes(passed=1)
        second.stdout.fnmatch_lines(["traceon: 1 replayed"])

    def test_divergence_fails_with_rendered_block(self, pytester, run_tests, monkeypatch):
        _write(pytester, MARKER_TEST)
        run_tests("--traceon-record=once").assert_outcomes(passed=1)
        monkeypatch.setenv("AGENT_SUFFIX", " in Paris")
        result = run_tests()
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(
            [
                "*traceon: first divergence*",
                "x first divergence  argument_mismatch - call #0 - tool:search",
                "    path      $.query",
                '    expected  "weather"',
                '    actual    "weather in Paris"',
            ]
        )
        result.stdout.fnmatch_lines(["traceon: 1 replayed, 1 diverged"])

    def test_missing_fixture_fails_with_hint(self, pytester, run_tests):
        _write(pytester, MARKER_TEST)
        result = run_tests()
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(
            ["*Traceon fixture not found:*run.json", "*pytest --traceon-record=once*"]
        )
        assert not (pytester.path / "fixtures" / "run.json").exists()

    def test_record_all_rerecords(self, pytester, run_tests, monkeypatch):
        _write(pytester, MARKER_TEST)
        run_tests("--traceon-record=once").assert_outcomes(passed=1)
        monkeypatch.setenv("AGENT_SUFFIX", " v2")
        run_tests("--traceon-record=all").assert_outcomes(passed=1)
        run_tests().assert_outcomes(passed=1)  # replay matches the new recording

    def test_ini_option(self, pytester, run_tests):
        _write(pytester, MARKER_TEST)
        pytester.makeini(
            "[pytest]\ntraceon_record = once\nasyncio_mode = auto\n"
            "asyncio_default_fixture_loop_scope = function\n"
        )
        run_tests().assert_outcomes(passed=1)
        assert (pytester.path / "fixtures" / "run.json").exists()

    def test_marker_kwargs_are_passed_to_replay(self, pytester, run_tests):
        _write(
            pytester,
            """
import pytest
from agent import run

@pytest.mark.traceon("fixtures/run.json", order="by_key", overrides={"llm": "PATCHED"})
def test_agent():
    assert run("weather") == "PATCHED"
""",
        )
        run_tests("--traceon-record=once", "-k", "nothing")  # nothing recorded yet
        _write(pytester, MARKER_TEST, name="test_record")
        run_tests("--traceon-record=once", "test_record.py").assert_outcomes(passed=1)
        run_tests("test_agent.py").assert_outcomes(passed=1)

    def test_async_test_with_marker(self, pytester, run_tests):
        _write(
            pytester,
            """
import asyncio
import pytest
from agent import run

@pytest.mark.traceon("fixtures/async.json")
async def test_agent():
    await asyncio.sleep(0)
    assert run("weather").startswith("SUMMARIZE")
""",
        )
        run_tests("--traceon-record=once").assert_outcomes(passed=1)
        run_tests().assert_outcomes(passed=1)

    def test_marker_without_path_is_usage_error(self, pytester, run_tests):
        _write(
            pytester,
            """
import pytest

@pytest.mark.traceon()
def test_agent():
    pass
""",
        )
        result = run_tests()
        assert result.ret != 0
        assert "needs a fixture path" in (result.stdout.str() + result.stderr.str())

    def test_invalid_record_option(self, pytester, run_tests):
        _write(pytester, MARKER_TEST)
        result = run_tests("--traceon-record=sometimes")
        assert result.ret == pytest.ExitCode.USAGE_ERROR


class TestFixtures:
    def test_cassette_records_then_replays(self, pytester, run_tests):
        _write(
            pytester,
            """
from agent import run

def test_research(traceon_cassette):
    expected_mode = "record" if not traceon_cassette.path.exists() else "replay"
    assert traceon_cassette.mode == expected_mode
    with traceon_cassette as handle:
        handle.set_output(run("weather"))
""",
        )
        run_tests("--traceon-record=once").assert_outcomes(passed=1)
        cassette = pytester.path / "cassettes" / "test_agent" / "test_research.json"
        assert cassette.exists()
        data = json.loads(cassette.read_text(encoding="utf-8"))
        assert data["output"]["value"] == "SUMMARIZE DOC ABOUT WEATHER"
        run_tests().assert_outcomes(passed=1)

    def test_cassette_missing_in_none_mode(self, pytester, run_tests):
        _write(
            pytester,
            """
def test_research(traceon_cassette):
    with traceon_cassette:
        pass
""",
        )
        result = run_tests()
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(["*Traceon fixture not found*"])

    def test_cassette_custom_path_and_async(self, pytester, run_tests):
        _write(
            pytester,
            """
from agent import run

async def test_research(traceon_cassette):
    async with traceon_cassette("data/custom.json", input={"q": "weather"}) as handle:
        handle.set_output(run("weather"))
""",
        )
        run_tests("--traceon-record=once").assert_outcomes(passed=1)
        data = json.loads((pytester.path / "data" / "custom.json").read_text(encoding="utf-8"))
        assert data["input"]["value"] == {"q": "weather"}
        run_tests().assert_outcomes(passed=1)

    def test_parametrized_cassette_names_are_safe(self, pytester, run_tests):
        _write(
            pytester,
            """
import pytest
from agent import run

@pytest.mark.parametrize("q", ["a b", "c/d"])
def test_q(traceon_cassette, q):
    with traceon_cassette as handle:
        handle.set_output(run(q))
""",
        )
        run_tests("--traceon-record=once").assert_outcomes(passed=2)
        names = sorted(p.name for p in (pytester.path / "cassettes" / "test_agent").iterdir())
        assert names == ["test_q_a_b.json", "test_q_c_d.json"]

    def test_traceon_replay_factory(self, pytester, run_tests, monkeypatch):
        _write(pytester, MARKER_TEST, name="test_record")
        run_tests("--traceon-record=once", "test_record.py").assert_outcomes(passed=1)
        _write(
            pytester,
            """
from agent import run

def test_replay(traceon_replay):
    with traceon_replay("fixtures/run.json") as handle:
        run("weather")
    assert handle.report.ok
    assert handle.report.calls_served == 2

def test_replay_report_without_raising(traceon_replay):
    with traceon_replay("fixtures/run.json", raise_on_divergence=False,
                        allow_unconsumed=True) as handle:
        pass
    assert handle.report.ok
""",
            name="test_replay",
        )
        run_tests("test_replay.py").assert_outcomes(passed=2)
        monkeypatch.setenv("AGENT_SUFFIX", "!")
        result = run_tests("test_replay.py::test_replay")
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(["*traceon: first divergence*", '*actual    "weather!"*'])

    def test_traceon_replay_missing_file(self, pytester, run_tests):
        _write(
            pytester,
            """
def test_replay(traceon_replay):
    traceon_replay("nope.json")
""",
        )
        result = run_tests()
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(["*Traceon fixture not found*nope.json*"])


class TestAssertNoDivergence:
    def test_helper_with_diff_report(self, pytester, run_tests):
        _write(
            pytester,
            """
import datetime as dt
from traceon.core.schema import Event, Fixture, Payload
from traceon.diff import compare
from traceon.pytest_plugin import assert_no_divergence

def fx(fid, query):
    t = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    ev = Event(event_id="e1", sequence=0, kind="tool", name="search", started_at=t,
               request=Payload.captured({"q": query}))
    return Fixture(fixture_id=fid, run_id="r", name="n", created_at=t, events=[ev])

def test_same():
    assert_no_divergence(compare(fx("a", "x"), fx("b", "x")))

def test_different():
    assert_no_divergence(compare(fx("a", "x"), fx("b", "y")))
""",
        )
        result = run_tests()
        result.assert_outcomes(passed=1, failed=1)
        result.stdout.fnmatch_lines(
            [
                "*TraceonDivergence: Traceon run diverged (1 divergence)*",
                "*x first divergence  argument_mismatch - call #0 - tool:search*",
            ]
        )

    def test_helper_unit(self):
        from traceon.core.schema import Divergence
        from traceon.pytest_plugin import TraceonDivergence, assert_no_divergence

        class Report:
            def __init__(self, divergences: list) -> None:
                self.divergences = divergences

        assert_no_divergence(Report([]))
        report = Report(
            [
                Divergence(
                    kind="missing_call",
                    position=1,
                    label="tool:x",
                    expected="tool:x",
                    message="Call #1 tool:x was recorded but never made",
                ),
                Divergence(
                    kind="output_mismatch",
                    label="run:output",
                    path="$",
                    expected=1,
                    actual=2,
                    message="Run output diverged at $: expected 1, got 2",
                ),
            ]
        )
        with pytest.raises(TraceonDivergence) as info:
            assert_no_divergence(report)
        assert isinstance(info.value, AssertionError)
        assert len(info.value.divergences) == 2
        text = str(info.value)
        assert "Traceon run diverged (2 divergences)" in text
        assert "missing_call - call #1 - tool:x" in text
        assert "(1 more divergence)" in text


class TestGithubSummary:
    def test_markdown_written_when_env_set(self, pytester, run_tests, monkeypatch, tmp_path):
        _write(pytester, MARKER_TEST)
        run_tests("--traceon-record=once").assert_outcomes(passed=1)
        summary = tmp_path / "summary.md"
        summary.write_text("# existing\n", encoding="utf-8")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        monkeypatch.setenv("AGENT_SUFFIX", " x")
        run_tests().assert_outcomes(failed=1)
        text = summary.read_text(encoding="utf-8")
        assert text.startswith("# existing\n")  # appended, not overwritten
        assert "### Traceon: 1 of 1 diverged" in text
        assert "| `test_agent.py::test_agent` | `fixtures/run.json` | replay |" in text
        assert "diverged: argument_mismatch at call #0 tool:search" in text
        assert '```diff\n- "weather"\n+ "weather x"\n```' in text

    def test_passing_summary(self, pytester, run_tests, monkeypatch, tmp_path):
        _write(pytester, MARKER_TEST)
        run_tests("--traceon-record=once").assert_outcomes(passed=1)
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        run_tests().assert_outcomes(passed=1)
        text = summary.read_text(encoding="utf-8")
        assert "### Traceon: 1 fixture, all passing" in text
        assert "| replay | ok |" in text

    def test_no_summary_without_traceon_tests(self, pytester, run_tests, monkeypatch, tmp_path):
        _write(pytester, "def test_plain():\n    pass\n")
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        run_tests().assert_outcomes(passed=1)
        assert not summary.exists()


def test_marker_is_registered(pytester, run_tests):
    result = run_tests("--markers")
    result.stdout.fnmatch_lines(["@pytest.mark.traceon(fixture, *, mode=*"])


def test_help_shows_option(pytester, run_tests):
    result = run_tests("--help")
    result.stdout.fnmatch_lines(["*--traceon-record=*"])
