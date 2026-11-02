"""Reproduce the failure offline, verify the fix, and catch a behavior change.

    python examples/failed_tool_call/replay.py

Step 1 replays the buggy version: the same ValueError, without any real tool call.
Step 2 replays the fixed version: every recorded call matches; the only
        difference is the run status (error -> ok), i.e. the fix works on the
        exact data that broke production.
Step 3 replays a refactored version that changes a tool argument: replay fails
        closed at that call and reports the exact path, expected and actual value.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent import REAL_CALLS, run_agent, run_agent_refactored, run_agent_v1

from traceon import DivergenceError, replay_run

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "eta_parse_failure.json"
ORDER_ID = "A-1042"


def step1_reproduce() -> bool:
    print("1) replay the buggy version")
    try:
        with replay_run(FIXTURE) as handle:
            handle.set_output(run_agent_v1(ORDER_ID))
    except ValueError as exc:
        print(f"   reproduced offline: {type(exc).__name__}: {exc}")
        return True
    print("   unexpected: the buggy version did not fail")
    return False


def step2_verify_fix() -> bool:
    print("2) replay the fixed version against the same recording")
    with replay_run(FIXTURE, raise_on_divergence=False) as handle:
        output = run_agent(ORDER_ID)
        handle.set_output(output)
    report = handle.report
    print(f"   calls served from the fixture: {report.calls_served}/{report.calls_total_recorded}")
    kinds = sorted({d.kind for d in report.divergences})
    print(f"   divergences: {kinds}")
    for d in report.divergences:
        print(f"   - {d.kind}: {d.message}")
    print(f"   eta_utc = {output['eta_utc']}")
    # The recording failed and the replay succeeded; nothing else may differ.
    return kinds == ["status_mismatch"]


def step3_catch_changed_argument() -> bool:
    print("3) replay a refactor that changes a tool argument")
    try:
        with replay_run(FIXTURE) as handle:
            handle.set_output(run_agent_refactored(ORDER_ID))
    except DivergenceError as exc:
        d = exc.divergence
        print(
            f"   first divergence: {d.kind} at {d.label} {d.path}: "
            f"expected {d.expected!r}, got {d.actual!r}"
        )
        return d.kind == "argument_mismatch"
    print("   unexpected: no divergence")
    return False


def main() -> int:
    if not FIXTURE.exists():
        print(
            f"{FIXTURE.relative_to(HERE).as_posix()} not found; run record.py first",
            file=sys.stderr,
        )
        return 2
    results = [step1_reproduce(), step2_verify_fix(), step3_catch_changed_argument()]
    print(f"real tool/model executions across all replays: {sum(REAL_CALLS.values())}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
