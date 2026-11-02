"""Re-record ``fixtures/triage_refund.json`` (the fixture the tests replay).

    python examples/regression_test/record.py

The fixture is committed to the repository. Re-record it only when the change
in behavior is intended, and review the diff first:

    traceon diff fixtures/triage_refund.json /tmp/new.json
"""

from __future__ import annotations

from pathlib import Path

from triage_agent import triage

from traceon import record_run

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "triage_refund.json"


def main() -> None:
    with record_run("ticket-triage", fixture=FIXTURE, input={"ticket_id": "T-311"}) as run:
        run.set_output(triage("T-311"))
    print(f"recorded {len(run.fixture.calls())} calls -> {FIXTURE.relative_to(HERE).as_posix()}")


if __name__ == "__main__":
    main()
