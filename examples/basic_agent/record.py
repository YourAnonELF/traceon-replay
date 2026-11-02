"""Record one run of the research agent to ``fixtures/research_run.json``.

python examples/basic_agent/record.py
"""

from __future__ import annotations

from pathlib import Path

from agent import REAL_CALLS, run_agent

from traceon import record_run

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "research_run.json"
QUESTION = "Why are agent failures hard to reproduce, and how does replay testing help?"


def main() -> None:
    with record_run("basic-research-agent", fixture=FIXTURE, input={"question": QUESTION}) as run:
        run.set_output(run_agent(QUESTION))

    fixture = run.fixture
    print(
        f"recorded {len(fixture.calls())} calls ({len(fixture.events)} events) -> {FIXTURE.relative_to(HERE).as_posix()}"
    )
    print(f"real tool/model executions: {dict(sorted(REAL_CALLS.items()))}")
    for event in fixture.calls():
        print(
            f"  #{event.sequence:<2} {event.kind:<5} {event.name:<15} request={event.request.state}"
        )


if __name__ == "__main__":
    main()
