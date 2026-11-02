"""Record the failing production run of the order-status agent (version 1).

    python examples/failed_tool_call/record.py

The run raises ``ValueError``. ``record_run`` still writes the fixture -- the
error is part of the recording -- and then re-raises it, which we catch here.
"""

from __future__ import annotations

from pathlib import Path

from agent import run_agent_v1

from traceon import record_run

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "eta_parse_failure.json"
ORDER_ID = "A-1042"


def main() -> None:
    try:
        with record_run("order-status-agent", fixture=FIXTURE, input={"order_id": ORDER_ID}) as run:
            run.set_output(run_agent_v1(ORDER_ID))
    except ValueError as exc:
        print(f"run failed as expected: {type(exc).__name__}: {exc}")

    fixture = run.fixture
    print(
        f"recorded status={fixture.status} with {len(fixture.calls())} calls -> "
        f"{FIXTURE.relative_to(HERE).as_posix()}"
    )
    if fixture.error is not None:
        print(f"recorded error: {fixture.error.type}: {fixture.error.message}")


if __name__ == "__main__":
    main()
