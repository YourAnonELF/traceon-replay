"""Recording SDK: instrument agent code and record runs to fixtures.

    from traceon.sdk import record_run, tool, model, span, now

    @tool
    def search(query: str) -> list[str]: ...

    with record_run("research-agent", fixture="fixtures/run.json") as run:
        with span("plan"):
            hits = search("replay debugging")
        run.set_output(hits)

For deterministic replay, agent code must take time, randomness and identifiers
from :func:`now`, :func:`random` and :func:`uuid4`, and route every external
effect through a ``@tool`` or ``@model`` function.
"""

from traceon.sdk.api import RunHandle, record_run
from traceon.sdk.decorators import model, now, random, span, tool, uuid4
from traceon.sdk.policy import CapturePolicy, RedactionPolicy
from traceon.sdk.recorder import RecordSession

__all__ = [
    "CapturePolicy",
    "RecordSession",
    "RedactionPolicy",
    "RunHandle",
    "model",
    "now",
    "random",
    "record_run",
    "span",
    "tool",
    "uuid4",
]
