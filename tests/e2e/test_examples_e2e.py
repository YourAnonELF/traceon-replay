"""Run the scripts in ``examples/`` as a user would (in a scratch copy)."""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"


@pytest.fixture
def examples(tmp_path: Path) -> Path:
    """A copy of examples/, so recording never overwrites the committed fixtures."""
    target = tmp_path / "examples"
    shutil.copytree(EXAMPLES, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return target


def _env() -> dict[str, str]:
    env = dict(os.environ)
    src = str(ROOT / "src")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH")) if p)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("GITHUB_STEP_SUMMARY", None)
    return env


def python(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=script.parent,
        env=_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def _plugin_args() -> list[str]:
    installed = any(
        ep.value == "traceon.pytest_plugin"
        for ep in importlib.metadata.entry_points(group="pytest11")
    )
    return [] if installed else ["-p", "traceon.pytest_plugin"]


@pytest.mark.parametrize("example", ["basic_agent", "failed_tool_call"])
def test_replay_committed_fixture(examples: Path, example: str) -> None:
    result = python(examples / example / "replay.py")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("example", ["basic_agent", "failed_tool_call", "regression_test"])
def test_record_then_replay(examples: Path, example: str) -> None:
    recorded = python(examples / example / "record.py")
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    if example != "regression_test":
        replayed = python(examples / example / "replay.py")
        assert replayed.returncode == 0, replayed.stdout + replayed.stderr


def test_basic_agent_output(examples: Path) -> None:
    out = python(examples / "basic_agent" / "replay.py").stdout
    assert "replay ok: True" in out
    assert "real tool/model executions during replay: 0" in out


def test_failed_tool_call_output(examples: Path) -> None:
    out = python(examples / "failed_tool_call" / "replay.py").stdout
    assert "reproduced offline: ValueError" in out
    assert "divergences: ['status_mismatch']" in out
    assert "argument_mismatch at tool:lookup_order $.order_id" in out


def test_regression_example_suite(examples: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *_plugin_args(),
            "-q",
            "-p",
            "no:cacheprovider",
            str(examples / "regression_test"),
        ],
        cwd=examples / "regression_test",
        env=_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout


def test_regression_example_catches_a_regression(examples: Path) -> None:
    agent = examples / "regression_test" / "triage_agent.py"
    source = agent.read_text(encoding="utf-8")
    regressed = source.replace('"high" if', '"urgent" if')
    assert regressed != source
    agent.write_text(regressed, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *_plugin_args(),
            "-q",
            "-p",
            "no:cacheprovider",
            str(examples / "regression_test"),
        ],
        cwd=examples / "regression_test",
        env=_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "tool:assign_ticket" in result.stdout
