"""``replay_run``: replay a recorded agent run offline.

    from traceon.replay import replay_run

    with replay_run("fixtures/failure.json") as handle:
        handle.set_output(agent.run("Summarize the available documents"))
    print(handle.report.ok)

Inside the block every instrumented tool call, model call and nondeterministic
value is answered from the fixture, the network is blocked, and the first
divergence from the recording raises immediately (fail closed). On exit the
replay is compared with the recording as a whole (unconsumed calls, output,
status) and :class:`~traceon.replay.report.ReplayReport` becomes available.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Mapping, Sequence
from types import TracebackType
from typing import Any, Literal

from traceon.core.errors import DivergenceError, ReplayError, TraceonError
from traceon.core.io import load_fixture, save_fixture
from traceon.core.limits import DEFAULT_LIMITS, Limits
from traceon.core.runtime import CallSpec, current_session, use_session
from traceon.core.schema import Fixture
from traceon.replay.guard import NetworkGuard
from traceon.replay.report import ReplayReport
from traceon.replay.session import Redactor, ReplaySession

__all__ = ["ReplayHandle", "ReplayReport", "replay_run"]


def replay_run(
    fixture: str | os.PathLike[str] | Fixture,
    *,
    mode: Literal["strict", "override"] = "strict",
    overrides: Mapping[str, Callable[..., Any] | Any] | None = None,
    order: Literal["strict", "by_key"] = "strict",
    block_network: bool = True,
    allow_unconsumed: bool = False,
    raise_on_divergence: bool = True,
    save_to: str | os.PathLike[str] | None = None,
    limits: Limits = DEFAULT_LIMITS,
    allow_hosts: Sequence[str] = (),
    redaction: Redactor | None = None,
) -> ReplayHandle:
    """Prepare a replay of ``fixture``; use the result as a (sync or async) context manager.

    Args:
        fixture: Path to a fixture file, or an already loaded :class:`Fixture`.
        mode: ``"strict"`` -- every call must match the recording; overrides only
            replace responses. ``"override"`` -- calls answered by an override are
            exempt from argument matching (what-if experiments).
        overrides: Answers for specific calls, keyed by name (``"search"``) or label
            (``"tool:search"``). A callable is called with the call's bound
            arguments as keyword arguments; any other value is returned as-is.
        order: ``"strict"`` -- the n-th call must be the n-th recorded call.
            ``"by_key"`` -- per-(kind, name) FIFO matching for concurrent agents.
        block_network: Block all socket connections and DNS lookups during replay.
        allow_unconsumed: Do not report recorded calls that were never made.
        raise_on_divergence: Raise :class:`DivergenceError` on exit if anything
            diverged. With ``False`` inspect ``handle.report`` instead.
        save_to: Write the replayed fixture here on exit (also when diverged).
        limits: Size limits for loading the fixture and the wall-clock replay limit.
        allow_hosts: Hosts that stay reachable when ``block_network`` is on.
        redaction: Redaction policy used to normalize requests; defaults to the
            SDK's default ``RedactionPolicy`` (the recorder's default).

    Raises:
        FixtureError: the fixture cannot be loaded or validated.
        ValueError: ``mode`` or ``order`` is not a supported value.
    """
    if mode not in ("strict", "override"):
        raise ValueError(f'mode must be "strict" or "override", got {mode!r}')
    if order not in ("strict", "by_key"):
        raise ValueError(f'order must be "strict" or "by_key", got {order!r}')
    baseline = fixture if isinstance(fixture, Fixture) else load_fixture(fixture, limits=limits)
    return ReplayHandle(
        baseline,
        mode=mode,
        overrides=overrides,
        order=order,
        block_network=block_network,
        allow_unconsumed=allow_unconsumed,
        raise_on_divergence=raise_on_divergence,
        save_to=save_to,
        limits=limits,
        allow_hosts=allow_hosts,
        redaction=redaction,
    )


class ReplayHandle:
    """Context manager returned by :func:`replay_run` (``with`` and ``async with``).

    * ``handle.set_output(value)`` -- the run's output, compared with the recording.
    * ``handle.report`` -- :class:`ReplayReport`, available after the block exits.
    * ``handle.session`` -- the active :class:`ReplaySession` (inside the block).

    On exit with divergences and ``raise_on_divergence=True`` a
    :class:`~traceon.core.errors.DivergenceError` is raised, unless an exception
    is already propagating out of the block: then the report is attached to that
    exception as ``exc.traceon_report`` and it propagates unchanged. The raised
    ``DivergenceError`` carries the report as ``traceon_report`` too.
    """

    def __init__(
        self,
        baseline: Fixture,
        *,
        mode: Literal["strict", "override"] = "strict",
        overrides: Mapping[str, Callable[..., Any] | Any] | None = None,
        order: Literal["strict", "by_key"] = "strict",
        block_network: bool = True,
        allow_unconsumed: bool = False,
        raise_on_divergence: bool = True,
        save_to: str | os.PathLike[str] | None = None,
        limits: Limits = DEFAULT_LIMITS,
        allow_hosts: Sequence[str] = (),
        redaction: Redactor | None = None,
    ) -> None:
        self.baseline = baseline
        self._mode = mode
        self._overrides = dict(overrides or {})
        self._order = order
        self._block_network = block_network
        self._allow_unconsumed = allow_unconsumed
        self._raise_on_divergence = raise_on_divergence
        self._save_to = save_to
        self._limits = limits
        self._allow_hosts = tuple(allow_hosts)
        self._redaction = redaction
        self._session: ReplaySession | None = None
        self._guard: NetworkGuard | None = None
        self._stack: contextlib.ExitStack | None = None
        self._report: ReplayReport | None = None
        self._output: Any = None
        self._has_output = False
        self._entered = False

    @property
    def session(self) -> ReplaySession:
        """The replay session (available once the block has been entered)."""
        if self._session is None:
            raise ReplayError("the replay session exists only after entering the replay block")
        return self._session

    @property
    def report(self) -> ReplayReport:
        """The :class:`ReplayReport` (available after the block exits)."""
        if self._report is None:
            raise ReplayError("handle.report is available only after the replay block exits")
        return self._report

    def set_output(self, value: Any) -> None:
        """Record the run's output; compared with the recording's output on exit."""
        if self._report is not None:
            raise ReplayError("set_output() must be called inside the replay block")
        self._output = value
        self._has_output = True

    # ---------------------------------------------------------------- sync
    def __enter__(self) -> ReplayHandle:
        if self._entered:
            raise ReplayError("a ReplayHandle can be entered only once; call replay_run() again")
        active = current_session()
        if active is not None:
            raise TraceonError(
                f"replay_run() cannot start inside an active Traceon {active.mode} session; "
                "finish the surrounding record_run()/replay_run() block first"
            )
        self._entered = True
        session = ReplaySession(
            self.baseline,
            mode=self._mode,
            overrides=self._overrides,
            order=self._order,
            allow_unconsumed=self._allow_unconsumed,
            limits=self._limits,
            redaction=self._redaction,
        )
        stack = contextlib.ExitStack()
        try:
            if self._block_network:
                self._guard = stack.enter_context(NetworkGuard(self._allow_hosts))
            stack.enter_context(use_session(session))
            stack.enter_context(session.span(CallSpec(kind="span", name=self.baseline.name)))
        except BaseException:
            stack.close()
            raise
        self._session = session
        self._stack = stack
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session, stack = self._session, self._stack
        if session is None or stack is None:
            return
        self._stack = None
        stack.__exit__(exc_type, exc, tb)
        attempts = self._guard.attempts if self._guard is not None else 0
        if self._has_output:
            session.set_output(self._output)
        replayed = session.finish(error=exc)
        report = session.report(network_attempts=attempts)
        self._report = report
        if self._save_to is not None:
            save_fixture(replayed, self._save_to)
        if exc is not None:
            with contextlib.suppress(AttributeError, TypeError):
                exc.traceon_report = report  # type: ignore[attr-defined]
            return
        if report.first_divergence is not None and self._raise_on_divergence:
            error = DivergenceError(report.first_divergence, list(report.divergences))
            error.traceon_report = report  # type: ignore[attr-defined]
            raise error

    # --------------------------------------------------------------- async
    async def __aenter__(self) -> ReplayHandle:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, tb)
