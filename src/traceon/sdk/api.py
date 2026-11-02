"""``record_run``: the entry point for recording an agent run."""

from __future__ import annotations

import contextlib
import os
import warnings
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

from traceon.core.errors import TraceonError
from traceon.core.io import save_fixture
from traceon.core.runtime import CallSpec, current_session, use_session
from traceon.core.schema import Fixture
from traceon.sdk.policy import CapturePolicy, RedactionPolicy
from traceon.sdk.recorder import RecordSession

__all__ = ["RunHandle", "record_run"]


class RunHandle:
    """A recording in progress (or finished). Returned by :func:`record_run`.

    Use it as a context manager, sync or async::

        with record_run("support-agent", fixture="fixtures/ticket-42.json") as run:
            answer = agent.handle(ticket)
            run.set_output(answer)
        print(run.fixture.events)

    On exit the fixture is built and, if a path was given, written atomically --
    also when the body raised: the error is recorded on the run and re-raised.
    A handle can be entered only once.
    """

    def __init__(
        self,
        name: str,
        fixture: str | os.PathLike[str] | None = None,
        *,
        input: Any = None,
        capture: CapturePolicy | None = None,
        redaction: RedactionPolicy | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._session = RecordSession(
            name,
            input=input,
            capture=capture,
            redaction=redaction,
            metadata=metadata,
        )
        self._path = Path(fixture) if fixture is not None else None
        self._fixture: Fixture | None = None
        self._entered = False
        self._session_cm: contextlib.AbstractContextManager[Any] | None = None
        self._root_cm: contextlib.AbstractContextManager[str] | None = None

    # ------------------------------------------------------------------ properties
    @property
    def name(self) -> str:
        """The run name (``Fixture.name``)."""
        return self._session.name

    @property
    def run_id(self) -> str:
        """Identifier of this run (``Fixture.run_id``); available immediately."""
        return self._session.run_id

    @property
    def session(self) -> RecordSession:
        """The underlying :class:`~traceon.sdk.recorder.RecordSession`."""
        return self._session

    @property
    def path(self) -> Path | None:
        """Where the fixture is (or will be) written, if a path was given."""
        return self._path

    @property
    def fixture(self) -> Fixture:
        """The recorded fixture. Raises :class:`TraceonError` before the run has exited."""
        if self._fixture is None:
            raise TraceonError(
                f"the fixture of run {self.name!r} is only available after the "
                "record_run block has exited"
            )
        return self._fixture

    def set_output(self, value: Any) -> None:
        """Set the run-level output stored in ``Fixture.output``."""
        if self._fixture is not None:
            raise TraceonError(
                f"run {self.name!r} has already finished; set_output() came too late"
            )
        self._session.set_output(value)

    # ------------------------------------------------------------------ context manager
    def __enter__(self) -> RunHandle:
        if self._entered:
            raise TraceonError("a RunHandle can only be entered once; call record_run() again")
        active = current_session()
        if active is not None:
            raise TraceonError(
                f"record_run({self.name!r}) cannot start inside another active Traceon "
                f"session (mode={active.mode!r}); nested runs are not supported. Use "
                "traceon.span(...) to group work inside the current run instead."
            )
        self._entered = True
        session_cm = use_session(self._session)
        session_cm.__enter__()
        try:
            root_cm = self._session.span(CallSpec(kind="span", name=self.name))
            root_cm.__enter__()
        except BaseException:
            session_cm.__exit__(None, None, None)
            raise
        self._session_cm = session_cm
        self._root_cm = root_cm
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        root_cm, session_cm = self._root_cm, self._session_cm
        self._root_cm = self._session_cm = None
        try:
            if root_cm is not None:
                root_cm.__exit__(exc_type, exc, tb)
        finally:
            if session_cm is not None:
                session_cm.__exit__(None, None, None)
        self._fixture = self._session.finish(error=exc)
        if self._path is not None:
            try:
                save_fixture(self._fixture, self._path)
            except OSError as save_exc:
                if exc is None:
                    raise TraceonError(
                        f"could not write fixture to {self._path}: {save_exc}"
                    ) from save_exc
                warnings.warn(
                    f"traceon: could not write fixture to {self._path}: {save_exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )

    async def __aenter__(self) -> RunHandle:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, tb)


def record_run(
    name: str,
    fixture: str | os.PathLike[str] | None = None,
    *,
    input: Any = None,
    capture: CapturePolicy | None = None,
    redaction: RedactionPolicy | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> RunHandle:
    """Record one agent run.

    Every ``@tool``/``@model`` call, ``span`` and ``now``/``random``/``uuid4`` value
    made inside the block (in this context, its asyncio tasks, and threads started
    with ``contextvars.copy_context().run``) becomes an event under a root span
    named ``name``.

    Args:
        name: Run name (``Fixture.name``), 1-512 characters.
        fixture: Optional path; the fixture is written there atomically on exit,
            including when the body raised.
        input: Run input stored in ``Fixture.input`` (``None`` = no input).
        capture: Which payloads to store (default :class:`CapturePolicy`).
        redaction: How to scrub secrets (default :class:`RedactionPolicy`).
        metadata: Free-form metadata stored (redacted) in ``Fixture.metadata``.

    Raises:
        TraceonError: On entry, if another Traceon session is already active.
    """
    return RunHandle(
        name,
        fixture,
        input=input,
        capture=capture,
        redaction=redaction,
        metadata=metadata,
    )
