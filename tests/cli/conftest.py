from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest
from click.testing import CliRunner, Result

from traceon.cli import main
from traceon.core.io import save_fixture


def _load_factory() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "diff" / "fixture_factory.py"
    spec = importlib.util.spec_from_file_location("traceon_tests_fixture_factory", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


factory = _load_factory()

AGENT_SOURCE = textwrap.dedent(
    """
    import asyncio
    import os

    from traceon.sdk import model, now, span, tool

    @tool
    def search(query: str) -> list:
        return [f"doc about {query}", "doc 2"]

    @model(provider="fake")
    def llm(prompt: str) -> dict:
        return {"text": prompt.upper(), "usage": {"input_tokens": 10, "output_tokens": 5}}

    def run(question: str) -> str:
        now()
        with span("research"):
            hits = search(question + os.environ.get("AGENT_SUFFIX", ""))
        return llm("summarize " + ", ".join(hits))["text"]

    async def arun(question: str) -> str:
        await asyncio.sleep(0)
        return run(question)

    def positional(value):
        return search(str(value))

    def no_args():
        return search("fixed")

    def boom(question: str) -> str:
        search(question)
        raise ValueError("agent exploded")

    NOT_CALLABLE = 42
    """
)


@pytest.fixture
def invoke():
    """Run the CLI in-process; returns the click Result (stdout/stderr separated)."""

    def run(*args: str, env: dict[str, str] | None = None) -> Result:
        runner = CliRunner()
        return runner.invoke(main, list(args), env=env, catch_exceptions=False)

    return run


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A temporary project directory containing ``agent_mod.py`` as the cwd."""
    (tmp_path / "agent_mod.py").write_text(AGENT_SOURCE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delenv("AGENT_SUFFIX", raising=False)
    monkeypatch.delitem(sys.modules, "agent_mod", raising=False)
    yield tmp_path
    sys.modules.pop("agent_mod", None)


@pytest.fixture
def fixtures_dir(tmp_path):
    """Writes a baseline, an identical candidate and a diverging candidate."""
    d = tmp_path / "fx"
    base = save_fixture(factory.simple_run("fx_base"), d / "base.json")
    same = save_fixture(factory.simple_run("fx_same"), d / "same.json")
    changed = save_fixture(
        factory.simple_run("fx_changed", query="weather in Paris", extra_call=True),
        d / "changed.json",
    )
    return {"base": base, "same": same, "changed": changed, "dir": d}


@pytest.fixture
def fxf() -> ModuleType:
    """The shared fixture factory module (``tests/diff/fixture_factory.py``)."""
    return factory
