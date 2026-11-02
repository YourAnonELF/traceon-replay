"""Regression tests that replay a committed fixture: offline, deterministic, no keys.

    pytest examples/regression_test

``@pytest.mark.traceon`` and the ``traceon_replay`` fixture come from the
plugin that ``pip install traceon-replay`` registers. Fixture paths are
resolved relative to this file.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from triage_agent import REAL_CALLS, triage

from traceon import UnmatchedCallError

FIXTURE = "fixtures/triage_refund.json"


@pytest.fixture(autouse=True)
def _reset_counters() -> Iterator[None]:
    REAL_CALLS.clear()
    yield
    REAL_CALLS.clear()


@pytest.mark.traceon(FIXTURE)
def test_refund_ticket_goes_to_billing() -> None:
    # The whole test body runs inside a strict replay of the fixture.
    result = triage("T-311")
    assert result["queue"] == "billing"
    assert result["priority"] == "high"


def test_replay_matches_recording(traceon_replay: Any) -> None:
    with traceon_replay(FIXTURE) as run:
        run.set_output(triage("T-311"))
    report = run.report
    assert report.ok, report.first_divergence
    assert report.calls_served == report.calls_total_recorded == 3


def test_side_effects_never_execute_during_replay(traceon_replay: Any) -> None:
    with traceon_replay(FIXTURE):
        triage("T-311")
    assert REAL_CALLS["assign_ticket"] == 0
    assert sum(REAL_CALLS.values()) == 0


def test_model_change_would_reroute_the_ticket(traceon_replay: Any) -> None:
    """What-if: if the model answered "general", the side effect would change."""
    run = traceon_replay(
        FIXTURE,
        mode="override",
        overrides={"classify": {"queue": "general", "confidence": 0.5}},
        raise_on_divergence=False,
    )
    # Replay fails closed: the changed side effect is never executed.
    with run, pytest.raises(UnmatchedCallError):
        triage("T-311")
    divergence = run.report.first_divergence
    assert divergence is not None
    assert divergence.kind == "argument_mismatch"
    assert divergence.label == "tool:assign_ticket"
    # The first differing argument in key order: "general" tickets get "normal" priority.
    assert divergence.path == "$.priority"
    assert (divergence.expected, divergence.actual) == ("high", "normal")
