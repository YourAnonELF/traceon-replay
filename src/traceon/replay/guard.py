"""Process-wide network guard used while a replay is active.

Replay must never reach the outside world: every tool and model call is served
from the fixture. :class:`NetworkGuard` enforces that by patching the socket
entry points every Python networking stack funnels through:

* ``socket.socket.connect`` and ``socket.socket.connect_ex`` (also used by
  ``ssl.SSLSocket`` and asyncio's socket transports);
* ``socket.create_connection`` (``http.client``, ``urllib``, most SDKs);
* ``socket.getaddrinfo`` (DNS resolution, including asyncio's resolver);
* ``sock_connect`` of asyncio's selector and proactor event loops (the Windows
  proactor connects through ``ConnectEx`` without calling ``socket.connect``, and
  asyncio skips ``getaddrinfo`` for IP literals).

Third-party event loops (e.g. uvloop) and C extensions that open sockets
natively are not covered.

Guards are reference counted and thread-safe: the patches are installed when the
first guard starts and the original functions are restored when the last guard
stops. While several guards are active, a connection is allowed only if *every*
active guard allows it, so nesting can only make the policy stricter.

``socket.socketpair()`` keeps working: on platforms without a native
implementation (Windows) it connects two loopback sockets internally, which is
process-local plumbing (asyncio's self-pipe) rather than network access.
"""

from __future__ import annotations

import asyncio.proactor_events
import asyncio.selector_events
import socket
import threading
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Any

from traceon.core.errors import NetworkBlockedError

__all__ = ["NetworkGuard", "active_guards"]


_lock = threading.RLock()
_active: list[NetworkGuard] = []
#: Originals captured when the patches were installed: name -> (owner, had_own_attr, value).
_originals: dict[str, tuple[Any, bool, Any]] = {}
_bypass = threading.local()


def active_guards() -> list[NetworkGuard]:
    """Return the guards that are currently active (a snapshot)."""
    with _lock:
        return list(_active)


def _normalize_host(host: Any) -> str | None:
    if host is None:
        return None
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    text = str(host).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return text.lower()


def _split_address(address: Any) -> tuple[str | None, int | None]:
    """Extract ``(host, port)`` from a socket address of any family."""
    if isinstance(address, tuple) and address:
        host = _normalize_host(address[0])
        port = address[1] if len(address) > 1 and isinstance(address[1], int) else None
        return host, port
    # AF_UNIX paths and other non-tuple addresses.
    return _normalize_host(address), None


def _format_target(host: str | None, port: Any) -> str:
    shown = host if host is not None else "<unspecified host>"
    if ":" in shown and not shown.startswith("/"):
        shown = f"[{shown}]"
    return f"{shown}:{port}" if port is not None else shown


def _bypassed() -> bool:
    return bool(getattr(_bypass, "depth", 0))


def _check(host: str | None, port: Any, operation: str) -> None:
    """Raise :class:`NetworkBlockedError` unless every active guard allows ``host``."""
    if _bypassed():
        return
    with _lock:
        guards = list(_active)
    if not guards or all(g.allows(host) for g in guards):
        return
    target = _format_target(host, port)
    for guard in guards:
        guard._record_attempt(target)
    hint_host = host if host is not None else "<host>"
    raise NetworkBlockedError(
        f"Traceon replay blocked network access to {target} ({operation}). Replay runs offline: "
        "every tool and model call must be served from the fixture, so nothing may reach the "
        f"network. If this connection is expected, wrap it in a @tool so it is recorded, allow it "
        f"with allow_hosts=[{hint_host!r}], or replay with block_network=False."
    )


def _original(name: str) -> Any:
    return _originals[name][2]


def _guarded_connect(self: socket.socket, address: Any, /) -> Any:
    host, port = _split_address(address)
    _check(host, port, "socket.connect")
    return _original("connect")(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any, /) -> Any:
    host, port = _split_address(address)
    _check(host, port, "socket.connect_ex")
    return _original("connect_ex")(self, address)


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
    host, port = _split_address(address)
    _check(host, port, "socket.create_connection")
    return _original("create_connection")(address, *args, **kwargs)


def _guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    normalized = _normalize_host(host)
    _check(normalized, port, "socket.getaddrinfo")
    result = _original("getaddrinfo")(host, port, *args, **kwargs)
    if not _bypassed():
        # An allowed hostname resolves to addresses that must be connectable too.
        resolved: set[str] = set()
        for entry in result:
            sockaddr = entry[4] if len(entry) > 4 else None
            if isinstance(sockaddr, tuple) and sockaddr:
                ip = _normalize_host(sockaddr[0])
                if ip is not None:
                    resolved.add(ip)
        with _lock:
            for guard in _active:
                if guard.allows(normalized):
                    guard._allow_resolved(resolved)
    return result


async def _guarded_selector_sock_connect(self: Any, sock: socket.socket, address: Any) -> Any:
    host, port = _split_address(address)
    _check(host, port, "loop.sock_connect")
    return await _original("selector_sock_connect")(self, sock, address)


async def _guarded_proactor_sock_connect(self: Any, sock: socket.socket, address: Any) -> Any:
    host, port = _split_address(address)
    _check(host, port, "loop.sock_connect")
    return await _original("proactor_sock_connect")(self, sock, address)


def _guarded_socketpair(*args: Any, **kwargs: Any) -> Any:
    depth = getattr(_bypass, "depth", 0)
    _bypass.depth = depth + 1
    try:
        return _original("socketpair")(*args, **kwargs)
    finally:
        _bypass.depth = depth


#: key -> (owner, attribute, replacement)
_PATCHES: dict[str, tuple[Any, str, Callable[..., Any]]] = {
    "connect": (socket.socket, "connect", _guarded_connect),
    "connect_ex": (socket.socket, "connect_ex", _guarded_connect_ex),
    "create_connection": (socket, "create_connection", _guarded_create_connection),
    "getaddrinfo": (socket, "getaddrinfo", _guarded_getaddrinfo),
    "socketpair": (socket, "socketpair", _guarded_socketpair),
    "selector_sock_connect": (
        asyncio.selector_events.BaseSelectorEventLoop,
        "sock_connect",
        _guarded_selector_sock_connect,
    ),
    "proactor_sock_connect": (
        asyncio.proactor_events.BaseProactorEventLoop,
        "sock_connect",
        _guarded_proactor_sock_connect,
    ),
}


def _install() -> None:
    for key, (owner, attr, replacement) in _PATCHES.items():
        if not hasattr(owner, attr):
            continue
        _originals[key] = (owner, attr in vars(owner), getattr(owner, attr))
        setattr(owner, attr, replacement)


def _uninstall() -> None:
    for key, (owner, had_own, value) in list(_originals.items()):
        attr = _PATCHES[key][1]
        if had_own:
            setattr(owner, attr, value)
        else:
            delattr(owner, attr)
        del _originals[key]


class NetworkGuard:
    """Block outbound network access while active.

    Use as a context manager or call :meth:`start` / :meth:`stop`. ``allow_hosts``
    lists host names or IP literals (case-insensitive) that remain reachable;
    addresses an allowed host name resolves to are allowed as well.

    Blocked attempts raise :class:`~traceon.core.errors.NetworkBlockedError` and
    are counted in :attr:`attempts` (process-wide: attempts from any thread are
    counted by every active guard).
    """

    def __init__(self, allow_hosts: Iterable[str] = ()) -> None:
        self._allow: frozenset[str] = frozenset(
            h for h in (_normalize_host(x) for x in allow_hosts) if h is not None
        )
        self._resolved: set[str] = set()
        self._attempts = 0
        self._blocked: list[str] = []
        self._active = False

    @property
    def allow_hosts(self) -> frozenset[str]:
        """Normalized host names / IPs this guard lets through."""
        return self._allow

    @property
    def attempts(self) -> int:
        """Number of blocked network attempts observed while this guard was active."""
        return self._attempts

    @property
    def blocked_targets(self) -> list[str]:
        """``host:port`` descriptions of every blocked attempt, in order."""
        with _lock:
            return list(self._blocked)

    @property
    def active(self) -> bool:
        """Whether this guard is currently installed."""
        return self._active

    def allows(self, host: str | None) -> bool:
        """Whether a connection to ``host`` is permitted by this guard."""
        if host is None:
            return False
        return host in self._allow or host in self._resolved

    def _allow_resolved(self, addresses: set[str]) -> None:
        self._resolved |= addresses

    def _record_attempt(self, target: str) -> None:
        with _lock:
            self._attempts += 1
            self._blocked.append(target)

    def start(self) -> NetworkGuard:
        """Activate the guard (idempotent). Installs the patches if it is the first one."""
        with _lock:
            if self._active:
                return self
            if not _active:
                _install()
            _active.append(self)
            self._active = True
        return self

    def stop(self) -> None:
        """Deactivate the guard (idempotent). Restores the originals if it was the last one."""
        with _lock:
            if not self._active:
                return
            _active.remove(self)
            self._active = False
            if not _active:
                _uninstall()

    def __enter__(self) -> NetworkGuard:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
