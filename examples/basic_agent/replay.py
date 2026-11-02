"""Replay the recorded research-agent run offline.

    python examples/basic_agent/replay.py

Every tool call, model call, ``now()`` and ``uuid4()`` is answered from the
fixture. The network is blocked for the duration of the replay, and the real
tool and model functions never run.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent import REAL_CALLS, run_agent

from traceon import replay_run

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "research_run.json"
QUESTION = "Why are agent failures hard to reproduce, and how does replay testing help?"


def main() -> int:
    if not FIXTURE.exists():
        print(
            f"{FIXTURE.relative_to(HERE).as_posix()} not found; run record.py first",
            file=sys.stderr,
        )
        return 2

    with replay_run(FIXTURE) as handle:
        output = run_agent(QUESTION)
        handle.set_output(output)

    report = handle.report
    print(f"replay ok: {report.ok}")
    print(f"calls served from the fixture: {report.calls_served}/{report.calls_total_recorded}")
    print(f"real tool/model executions during replay: {sum(REAL_CALLS.values())}")
    print(f"network connection attempts blocked: {report.network_attempts}")
    print(f"request id (replayed): {output['request_id']}")
    print(f"answer: {output['answer']}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
