from __future__ import annotations

import importlib.metadata
import textwrap

import pytest

AGENT = textwrap.dedent(
    """
    import os

    from traceon.sdk import model, tool

    @tool
    def search(query: str) -> list:
        return [f"doc about {query}"]

    @model(provider="fake")
    def llm(prompt: str) -> str:
        return prompt.upper()

    def run(question: str) -> str:
        hits = search(question + os.environ.get("AGENT_SUFFIX", ""))
        return llm("summarize " + hits[0])
    """
)


def _plugin_autoloaded() -> bool:
    """True when the installed distribution registers the plugin via ``pytest11``."""
    eps = importlib.metadata.entry_points(group="pytest11")
    return any(ep.value == "traceon.pytest_plugin" for ep in eps)


@pytest.fixture
def run_tests(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch):
    """Run pytest in-process in the pytester sandbox with the Traceon plugin enabled."""
    pytester.makepyfile(agent=AGENT)
    pytester.makeini(
        "[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n"
    )
    monkeypatch.delenv("AGENT_SUFFIX", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    extra = [] if _plugin_autoloaded() else ["-p", "traceon.pytest_plugin"]

    def run(*args: str) -> pytest.RunResult:
        return pytester.runpytest(*extra, "-p", "no:cacheprovider", *args)

    return run
