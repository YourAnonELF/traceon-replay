"""Traceon Replay -- deterministic replay and time-travel debugging for AI agents.

Record an agent run once, then replay it offline: every tool call, model call and
nondeterministic value is served from the recording, nothing reaches the network,
and the first point where behavior changes is reported precisely.

    from traceon import record_run, replay_run, tool

    @tool
    def search(query: str) -> list[str]: ...

    with record_run("research-agent", fixture="fixtures/failure.json"):
        agent.run("Summarize the available documents")

    with replay_run("fixtures/failure.json", mode="strict"):
        agent.run("Summarize the available documents")

Public names are loaded lazily (PEP 562) so ``import traceon`` stays cheap and
submodules can be imported independently.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from traceon._version import __version__

_EXPORTS: dict[str, str] = {
    # errors
    "TraceonError": "traceon.core.errors",
    "FixtureError": "traceon.core.errors",
    "ReplayError": "traceon.core.errors",
    "DivergenceError": "traceon.core.errors",
    "UnmatchedCallError": "traceon.core.errors",
    "ResponseNotCapturedError": "traceon.core.errors",
    "NetworkBlockedError": "traceon.core.errors",
    "RecordedCallError": "traceon.core.errors",
    # data model & io
    "Fixture": "traceon.core.schema",
    "Divergence": "traceon.core.schema",
    "DiffReport": "traceon.core.schema",
    "load_fixture": "traceon.core.io",
    "save_fixture": "traceon.core.io",
    # recording
    "record_run": "traceon.sdk",
    "tool": "traceon.sdk",
    "model": "traceon.sdk",
    "span": "traceon.sdk",
    "now": "traceon.sdk",
    "random": "traceon.sdk",
    "uuid4": "traceon.sdk",
    "CapturePolicy": "traceon.sdk",
    "RedactionPolicy": "traceon.sdk",
    # replay & diff
    "replay_run": "traceon.replay",
    "ReplayReport": "traceon.replay",
    "compare": "traceon.diff",
}

__all__ = ["__version__", *sorted(_EXPORTS)]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'traceon' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


if TYPE_CHECKING:
    from traceon.core.errors import (
        DivergenceError,
        FixtureError,
        NetworkBlockedError,
        RecordedCallError,
        ReplayError,
        ResponseNotCapturedError,
        TraceonError,
        UnmatchedCallError,
    )
    from traceon.core.io import load_fixture, save_fixture
    from traceon.core.schema import DiffReport, Divergence, Fixture
    from traceon.diff import compare
    from traceon.replay import ReplayReport, replay_run
    from traceon.sdk import (
        CapturePolicy,
        RedactionPolicy,
        model,
        now,
        random,
        record_run,
        span,
        tool,
        uuid4,
    )
