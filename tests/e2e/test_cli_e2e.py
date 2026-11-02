"""Record -> inspect -> replay -> diff through the ``traceon`` command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from traceon.cli import main

HERE = Path(__file__).resolve().parent
LANGFUSE_SAMPLE = HERE.parent / "integrations" / "data" / "langfuse_trace.json"
INPUT = json.dumps({"question": "replay", "limit": 2})


@pytest.fixture(autouse=True)
def _in_e2e_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    # `--entry e2e_agents:...` is imported relative to the working directory.
    monkeypatch.chdir(HERE)


def run(*args: str) -> Result:
    return CliRunner().invoke(main, ["--color", "never", *args], catch_exceptions=False)


@pytest.fixture
def baseline(tmp_path: Path) -> Path:
    out = tmp_path / "baseline.json"
    result = run("record", "--entry", "e2e_agents:research", "--out", str(out), "--input", INPUT)
    assert result.exit_code == 0, result.output
    assert out.is_file()
    return out


def test_version() -> None:
    result = run("--version")
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_inspect_and_validate(baseline: Path) -> None:
    result = run("inspect", str(baseline))
    assert result.exit_code == 0, result.output
    assert "search" in result.output and "answer" in result.output

    as_json = run("inspect", str(baseline), "--json")
    assert as_json.exit_code == 0
    json.loads(as_json.output)

    assert run("validate", str(baseline)).exit_code == 0


def test_replay_matches(baseline: Path) -> None:
    result = run("replay", str(baseline), "--entry", "e2e_agents:research")
    assert result.exit_code == 0, result.output


def test_replay_reports_divergence_with_exit_1(baseline: Path) -> None:
    result = run("replay", str(baseline), "--entry", "e2e_agents:research_changed_limit")
    assert result.exit_code == 1, result.output
    assert "argument_mismatch" in result.output
    assert "$.limit" in result.output


def test_replay_save_then_diff(baseline: Path, tmp_path: Path) -> None:
    saved = tmp_path / "replayed.json"
    result = run("replay", str(baseline), "--entry", "e2e_agents:research", "--save", str(saved))
    assert result.exit_code == 0, result.output
    assert run("diff", str(baseline), str(saved)).exit_code == 0


def test_diff_two_recordings(baseline: Path, tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.json"
    rec = run(
        "record",
        "--entry",
        "e2e_agents:research_changed_limit",
        "--out",
        str(candidate),
        "--input",
        INPUT,
    )
    assert rec.exit_code == 0, rec.output

    text = run("diff", str(baseline), str(candidate))
    assert text.exit_code == 1
    assert "tool:search" in text.output

    as_json = run("diff", str(baseline), str(candidate), "--json")
    assert as_json.exit_code == 1
    report = json.loads(as_json.output)
    assert report["equivalent"] is False
    assert report["first_divergence"]["path"] == "$.limit"

    md = run("diff", str(baseline), str(candidate), "--markdown")
    assert md.exit_code == 1
    assert "|" in md.output


def test_invalid_and_missing_fixtures_exit_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": "traceon.fixture/v1", "events": "nope"}', encoding="utf-8")
    assert run("validate", str(bad)).exit_code == 2
    assert run("inspect", str(tmp_path / "missing.json")).exit_code == 2
    assert (
        run("replay", str(tmp_path / "missing.json"), "--entry", "e2e_agents:research").exit_code
        == 2
    )


def test_import_langfuse_file(tmp_path: Path) -> None:
    out = tmp_path / "lf.json"
    result = run("import", "langfuse", "--file", str(LANGFUSE_SAMPLE), "--out", str(out))
    assert result.exit_code == 0, result.output
    assert run("validate", str(out)).exit_code == 0
    fixture = json.loads(out.read_text(encoding="utf-8"))
    assert fixture["source"] == "langfuse"
    assert "lf-demo-should-never-leak" not in out.read_text(encoding="utf-8")
    assert run("inspect", str(out)).exit_code == 0
