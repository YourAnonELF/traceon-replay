"""Replay engine: serve recorded calls offline and report the first divergence.

* :func:`replay_run` / :class:`ReplayHandle` -- the user-facing context manager.
* :class:`ReplaySession` -- the :class:`~traceon.core.runtime.Session` that
  answers instrumented calls from a fixture without running them.
* :class:`ReplayReport` -- the outcome: divergences, counts, replayed fixture.
* :class:`NetworkGuard` -- process-wide socket/DNS blocker used during replay.
"""

from traceon.replay.api import ReplayHandle, replay_run
from traceon.replay.guard import NetworkGuard
from traceon.replay.report import ReplayReport
from traceon.replay.session import ReplaySession

__all__ = ["NetworkGuard", "ReplayHandle", "ReplayReport", "ReplaySession", "replay_run"]
