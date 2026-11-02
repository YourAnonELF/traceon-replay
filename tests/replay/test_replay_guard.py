"""Network guard tests. Nothing here reaches the network: blocked attempts are
rejected before any packet is sent, and allowed ones target a local listener."""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Iterator

import pytest

from traceon.core.errors import NetworkBlockedError
from traceon.replay.guard import NetworkGuard, active_guards

# TEST-NET-1 (RFC 5737): reserved for documentation, never routed.
UNROUTABLE = ("192.0.2.1", 9)


@pytest.fixture
def listener() -> Iterator[tuple[str, int]]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        yield server.getsockname()
    finally:
        server.close()


def test_blocks_socket_connect_with_host_and_port() -> None:
    with NetworkGuard() as guard, socket.socket() as sock:
        with pytest.raises(NetworkBlockedError, match=r"192\.0\.2\.1:9 \(socket\.connect\)"):
            sock.connect(UNROUTABLE)
        assert guard.attempts == 1
        assert guard.blocked_targets == ["192.0.2.1:9"]


def test_blocks_connect_ex() -> None:
    with NetworkGuard() as guard, socket.socket() as sock:
        with pytest.raises(NetworkBlockedError, match="connect_ex"):
            sock.connect_ex(UNROUTABLE)
        assert guard.attempts == 1


def test_blocks_create_connection() -> None:
    with NetworkGuard() as guard:
        with pytest.raises(NetworkBlockedError, match=r"example\.invalid:443"):
            socket.create_connection(("example.invalid", 443), timeout=0.1)
        assert guard.attempts == 1


def test_blocks_getaddrinfo() -> None:
    with NetworkGuard() as guard:
        with pytest.raises(
            NetworkBlockedError, match=r"api\.example\.invalid:443 \(socket\.getaddrinfo\)"
        ):
            socket.getaddrinfo("api.example.invalid", 443)
        assert guard.attempts == 1


def test_blocks_loopback_by_default(listener: tuple[str, int]) -> None:
    with NetworkGuard(), socket.socket() as sock, pytest.raises(NetworkBlockedError):
        sock.connect(listener)


def test_error_message_is_actionable() -> None:
    with NetworkGuard(), pytest.raises(NetworkBlockedError) as info:
        socket.create_connection(UNROUTABLE)
    message = str(info.value)
    assert "allow_hosts=['192.0.2.1']" in message
    assert "block_network=False" in message and "@tool" in message


def test_ipv6_target_is_bracketed() -> None:
    with (
        NetworkGuard(),
        socket.socket(socket.AF_INET6) as sock,
        pytest.raises(NetworkBlockedError, match=r"\[2001:db8::1\]:80"),
    ):
        sock.connect(("2001:db8::1", 80, 0, 0))


def test_restores_originals_after_stop() -> None:
    before = (socket.getaddrinfo, socket.create_connection, socket.socket.connect)
    guard = NetworkGuard().start()
    assert socket.getaddrinfo is not before[0]
    assert "connect" in socket.socket.__dict__
    guard.stop()
    assert (socket.getaddrinfo, socket.create_connection, socket.socket.connect) == before
    assert "connect" not in socket.socket.__dict__


def test_restores_after_exception_in_block() -> None:
    with pytest.raises(RuntimeError), NetworkGuard():
        raise RuntimeError("boom")
    assert active_guards() == []


def test_nested_guards_are_refcounted() -> None:
    original = socket.getaddrinfo
    outer = NetworkGuard().start()
    inner = NetworkGuard().start()
    inner.stop()
    assert socket.getaddrinfo is not original  # outer still active
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("x.invalid", 80)
    assert outer.attempts == 1 and inner.attempts == 0
    outer.stop()
    assert socket.getaddrinfo is original


def test_start_and_stop_are_idempotent() -> None:
    guard = NetworkGuard()
    guard.start()
    guard.start()
    assert active_guards() == [guard] and guard.active
    guard.stop()
    guard.stop()
    assert active_guards() == [] and not guard.active


def test_allow_hosts_lets_listed_host_through(listener: tuple[str, int]) -> None:
    with NetworkGuard(allow_hosts=["127.0.0.1"]) as guard:
        with socket.create_connection(listener, timeout=2):
            pass
        assert guard.attempts == 0
        with pytest.raises(NetworkBlockedError):
            socket.create_connection(UNROUTABLE)


def test_allowed_hostname_resolution_allows_resolved_addresses(listener: tuple[str, int]) -> None:
    with NetworkGuard(allow_hosts=["LOCALHOST"]) as guard:
        infos = socket.getaddrinfo("localhost", listener[1], socket.AF_INET, socket.SOCK_STREAM)
        assert infos
        assert guard.allows("127.0.0.1")
        with socket.socket() as sock:
            sock.connect(listener)


def test_nested_guard_can_only_restrict(listener: tuple[str, int]) -> None:
    with NetworkGuard(allow_hosts=["127.0.0.1"]) as outer, NetworkGuard() as inner:
        with socket.socket() as sock, pytest.raises(NetworkBlockedError):
            sock.connect(listener)
        assert outer.attempts == 1 and inner.attempts == 1


def test_socketpair_still_works() -> None:
    with NetworkGuard() as guard:
        a, b = socket.socketpair()
        with a, b:
            a.sendall(b"ping")
            assert b.recv(4) == b"ping"
        assert guard.attempts == 0


def test_blocks_other_threads() -> None:
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            socket.create_connection(UNROUTABLE)
        except BaseException as exc:
            errors.append(exc)

    with NetworkGuard() as guard:
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert len(errors) == 1 and isinstance(errors[0], NetworkBlockedError)
    assert guard.attempts == 1


def test_asyncio_open_connection_is_blocked_and_event_loop_works() -> None:
    async def main() -> None:
        await asyncio.open_connection("192.0.2.1", 9)

    with NetworkGuard() as guard, pytest.raises(NetworkBlockedError):
        asyncio.run(main())  # creating the loop (self-pipe) must still work
    assert guard.attempts >= 1


def test_concurrent_start_stop_is_consistent() -> None:
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        for _ in range(50):
            with NetworkGuard():
                pass

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active_guards() == []
