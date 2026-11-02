from __future__ import annotations

import asyncio.proactor_events
import asyncio.selector_events
import socket
from collections.abc import Iterator

import pytest

from traceon.replay.guard import active_guards


@pytest.fixture(autouse=True)
def _network_guard_is_restored() -> Iterator[None]:
    """Every replay test must leave the socket module exactly as it found it."""
    originals = (socket.getaddrinfo, socket.create_connection, socket.socketpair)
    loops = (
        asyncio.selector_events.BaseSelectorEventLoop.sock_connect,
        asyncio.proactor_events.BaseProactorEventLoop.sock_connect,
    )
    yield
    assert active_guards() == []
    assert "connect" not in socket.socket.__dict__
    assert "connect_ex" not in socket.socket.__dict__
    assert (socket.getaddrinfo, socket.create_connection, socket.socketpair) == originals
    assert (
        asyncio.selector_events.BaseSelectorEventLoop.sock_connect,
        asyncio.proactor_events.BaseProactorEventLoop.sock_connect,
    ) == loops
