"""Exception hierarchy shared by every Traceon module."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from traceon.core.schema import Divergence, ErrorInfo


class TraceonError(Exception):
    """Base class for all Traceon errors."""


class FixtureError(TraceonError):
    """A fixture is malformed, too large, unsafe, or uses an unknown schema version."""


class ReplayError(TraceonError):
    """Replay could not proceed."""


class DivergenceError(ReplayError):
    """The replayed run diverged from the recording.

    ``divergence`` is the *first* divergence observed; ``divergences`` holds every
    divergence known at the time the error was raised.
    """

    def __init__(self, divergence: Divergence, divergences: list[Divergence] | None = None) -> None:
        self.divergence = divergence
        self.divergences = divergences if divergences is not None else [divergence]
        super().__init__(divergence.message)


class UnmatchedCallError(DivergenceError):
    """A call was made during strict replay that has no matching recording.

    Replay fails closed: the call is never forwarded to the real tool or model.
    """


class ResponseNotCapturedError(ReplayError):
    """The matching recording exists but its response payload was not captured
    (or was redacted), so it cannot be served. Re-record with capture enabled or
    provide an override."""


class NetworkBlockedError(ReplayError):
    """Code under replay attempted to open a network connection."""


class RecordedCallError(TraceonError):
    """Re-raised during replay when the recorded call originally failed.

    The original exception type is not reconstructed (fixtures never contain
    executable objects); ``error_type`` and ``error_message`` carry its identity.
    """

    def __init__(self, info: ErrorInfo) -> None:
        self.info = info
        self.error_type = info.type
        self.error_message = info.message
        super().__init__(f"{info.type}: {info.message}")
