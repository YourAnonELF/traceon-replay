"""The single interception point shared by recording and replay.

Instrumentation (``@traceon.tool``, ``@traceon.model``, ``traceon.span``,
``traceon.now`` ...) never talks to a recorder or a replayer directly. It builds a
:class:`CallSpec` and hands it to the *active session* together with a thunk that
performs the real call:

* With no active session, instrumentation calls the thunk directly (zero overhead
  beyond a context-variable lookup).
* :class:`~traceon.sdk.recorder.RecordSession` runs the thunk and records an event.
* :class:`~traceon.replay.session.ReplaySession` never runs the thunk (unless an
  explicit override says so); it matches the call against the fixture and returns
  the recorded response.

Sessions are bound per ``contextvars`` context, so concurrent asyncio tasks and
threads started with ``contextvars.copy_context()`` see the right session and the
right parent span.
"""

from __future__ import annotations

import abc
import contextlib
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar

if TYPE_CHECKING:
    from traceon.core.schema import EventKind, Fixture, Usage

T = TypeVar("T")


@dataclass(frozen=True)
class CallSpec:
    """Description of one intercepted call, built by instrumentation."""

    kind: EventKind
    name: str
    #: Bound arguments as raw Python values, keyed by parameter name
    #: (``inspect.signature(fn).bind(*args, **kwargs)`` with defaults applied,
    #: ``self``/``cls`` removed). For ``value`` events, ``{}`` or a small hint dict.
    request: Mapping[str, Any] = field(default_factory=dict)
    side_effect: bool = False
    #: Whether the request payload may be stored. ``None`` = session policy decides.
    #: ``"hash"`` stores only the hash (call matching still works).
    capture_request: bool | Literal["hash"] | None = None
    #: Whether the response payload may be stored. ``None`` = session policy decides.
    capture_response: bool | None = None
    #: Converts the canonical JSON response back into the type callers expect
    #: (used only in replay). ``None`` returns the JSON value as-is.
    decode: Callable[[Any], Any] | None = None
    #: Extracts token usage / cost from the real result (record only).
    usage: Callable[[Any], Usage | Mapping[str, Any] | None] | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}"


class Session(abc.ABC):
    """Base class for an active record or replay session."""

    mode: ClassVar[Literal["record", "replay"]]

    @abc.abstractmethod
    def call(self, spec: CallSpec, invoke: Callable[[], T]) -> T:
        """Handle a synchronous call. ``invoke`` performs the real call."""

    @abc.abstractmethod
    async def acall(self, spec: CallSpec, invoke: Callable[[], Awaitable[T]]) -> T:
        """Handle an asynchronous call. ``invoke`` returns the real awaitable."""

    @abc.abstractmethod
    def span(self, spec: CallSpec) -> contextlib.AbstractContextManager[str]:
        """Open a structural span; yields its ``event_id``. Children nest under it."""

    @abc.abstractmethod
    def finish(self, *, output: Any = None, error: BaseException | None = None) -> Fixture:
        """Close the session and return the fixture describing this run."""


_SESSION: ContextVar[Session | None] = ContextVar("traceon_session", default=None)
_PARENT: ContextVar[str | None] = ContextVar("traceon_parent_event", default=None)


def current_session() -> Session | None:
    return _SESSION.get()


def set_session(session: Session | None) -> Token[Session | None]:
    return _SESSION.set(session)


def reset_session(token: Token[Session | None]) -> None:
    _SESSION.reset(token)


@contextlib.contextmanager
def use_session(session: Session) -> Iterator[Session]:
    token = _SESSION.set(session)
    parent_token = _PARENT.set(None)
    try:
        yield session
    finally:
        _PARENT.reset(parent_token)
        _SESSION.reset(token)


def current_parent_id() -> str | None:
    return _PARENT.get()


@contextlib.contextmanager
def push_parent(event_id: str) -> Iterator[str]:
    """Make ``event_id`` the parent of every event started inside the block."""
    token = _PARENT.set(event_id)
    try:
        yield event_id
    finally:
        _PARENT.reset(token)
