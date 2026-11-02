"""The result of a replay: :class:`ReplayReport`."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from traceon.core.schema import Divergence, Fixture

__all__ = ["ReplayReport"]


class ReplayReport(BaseModel):
    """Outcome of one replay, available as ``handle.report`` after the block exits.

    ``replayed`` is a fixture (``mode="replay"``) describing what actually happened
    during the replay, shaped like the recording so it can be compared with
    ``traceon diff`` or opened in Studio.
    """

    model_config = ConfigDict(extra="forbid")

    #: ``fixture_id`` of the recording that was replayed.
    fixture_id: str
    #: Fixture describing the replayed run (its own ``fixture_id`` and ``run_id``).
    replayed: Fixture
    #: Every divergence, in the order it was detected.
    divergences: list[Divergence] = Field(default_factory=list)
    first_divergence: Divergence | None = None
    #: Calls answered from the recording (including recorded failures re-raised).
    calls_served: int = 0
    #: Calls answered by an override.
    calls_overridden: int = 0
    #: Number of calls (tool, model and value events) in the recording.
    calls_total_recorded: int = 0
    #: Event ids of recorded calls that were never consumed.
    unconsumed: list[str] = Field(default_factory=list)
    #: Network connections blocked while the replay was active.
    network_attempts: int = 0
    #: ``True`` when there are no divergences.
    ok: bool = True
