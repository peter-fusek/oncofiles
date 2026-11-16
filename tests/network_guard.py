"""No test may talk to the outside world.

``install()`` replaces the low-level socket entry points for the whole test
process, so the rule holds in every thread (``asyncio.to_thread`` workers,
thread pools, plain threads) and under TLS (``ssl.SSLSocket.connect`` ends in
``socket.socket.connect``):

* ``socket.socket.connect`` / ``connect_ex`` / ``sendto`` — only loopback
  addresses (``127.0.0.0/8``, ``::1``, ``localhost``) and Unix domain sockets;
* ``socket.create_connection`` — same rule, checked before any lookup;
* ``socket.getaddrinfo`` / ``gethostbyname`` / ``gethostbyname_ex`` — only
  ``localhost``, no host, or an IP literal (a literal needs no DNS query; the
  connect rule still decides whether it may be used), so a hostname never
  reaches a resolver.

Everything else raises ``NetworkAccessBlockedError`` naming the call and the
host/port — never a payload. Application code often catches ``Exception``
around I/O, so each refusal is also recorded and the autouse fixture in
``conftest.py`` fails the test at teardown when a refusal was swallowed.

There is no opt-out marker on purpose: a test that needs a server starts one
on loopback or stubs the client.
"""

from __future__ import annotations

import ipaddress
import socket
import threading

_LOCAL_NAMES = frozenset({"localhost", "localhost.", "ip6-localhost", "ip6-loopback"})


class NetworkAccessBlockedError(RuntimeError):
    """A test tried to open a non-loopback connection or resolve an outside name.

    Deliberately not an ``OSError``: retry and fallback code written for
    network errors must not treat the refusal as a flaky connection.
    """


_lock = threading.Lock()
_blocked: list[str] = []
_originals: dict[str, object] = {}


def _text(host) -> str | None:
    if host is None:
        return None
    if isinstance(host, bytes | bytearray):
        host = bytes(host).decode("ascii", "replace")
    return str(host)


def _ip(host: str):
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


def is_loopback_host(host) -> bool:
    """True for ``localhost`` and loopback IP literals; False for everything else."""
    text = _text(host)
    if text is None:
        return False
    if text.lower() in _LOCAL_NAMES:
        return True
    ip = _ip(text)
    return ip is not None and ip.is_loopback


def _refuse(call: str, host, port=None) -> None:
    where = f"{_text(host)!r}" + (f" port {port}" if port is not None else "")
    message = (
        f"tests must not reach the network: {call} to {where} was refused "
        "(only loopback and Unix sockets are allowed; stub the client or use a "
        "local server on 127.0.0.1)"
    )
    with _lock:
        _blocked.append(message)
    raise NetworkAccessBlockedError(message)


def _check_address(call: str, sock: socket.socket, address) -> None:
    family = sock.family
    if family == getattr(socket, "AF_UNIX", object()):
        return
    if family in (socket.AF_INET, socket.AF_INET6):
        host, port = (address[0], address[1]) if isinstance(address, tuple) else (address, None)
        if not is_loopback_host(host):
            _refuse(call, host, port)
        return
    # Any other family (Bluetooth, raw packets, …) is not something a test needs.
    _refuse(call, f"<address family {int(family)}>")


def _check_lookup(call: str, host) -> None:
    text = _text(host)
    if text is None or text == "" or text.lower() in _LOCAL_NAMES or _ip(text) is not None:
        return
    _refuse(call, text)


def _guarded_connect(self, address, *args, **kwargs):
    _check_address("socket.connect", self, address)
    return _originals["connect"](self, address, *args, **kwargs)


def _guarded_connect_ex(self, address, *args, **kwargs):
    _check_address("socket.connect_ex", self, address)
    return _originals["connect_ex"](self, address, *args, **kwargs)


def _guarded_sendto(self, data, *args):
    # sendto(data, address) or sendto(data, flags, address); the data is never read here.
    _check_address("socket.sendto", self, args[-1] if args else None)
    return _originals["sendto"](self, data, *args)


def _guarded_create_connection(address, *args, **kwargs):
    host, port = address
    if not is_loopback_host(host):
        _refuse("socket.create_connection", host, port)
    return _originals["create_connection"](address, *args, **kwargs)


def _guarded_getaddrinfo(host, *args, **kwargs):
    _check_lookup("socket.getaddrinfo", host)
    return _originals["getaddrinfo"](host, *args, **kwargs)


def _guarded_gethostbyname(hostname):
    _check_lookup("socket.gethostbyname", hostname)
    return _originals["gethostbyname"](hostname)


def _guarded_gethostbyname_ex(hostname):
    _check_lookup("socket.gethostbyname_ex", hostname)
    return _originals["gethostbyname_ex"](hostname)


_CLASS_PATCHES = {
    "connect": _guarded_connect,
    "connect_ex": _guarded_connect_ex,
    "sendto": _guarded_sendto,
}
_MODULE_PATCHES = {
    "create_connection": _guarded_create_connection,
    "getaddrinfo": _guarded_getaddrinfo,
    "gethostbyname": _guarded_gethostbyname,
    "gethostbyname_ex": _guarded_gethostbyname_ex,
}


def install() -> None:
    """Patch the socket module for the whole process.

    Idempotent, and it puts the guard back when something replaced a guarded
    function without restoring it.
    """
    for name, guarded in _CLASS_PATCHES.items():
        _originals.setdefault(name, getattr(socket.socket, name))
        setattr(socket.socket, name, guarded)
    for name, guarded in _MODULE_PATCHES.items():
        _originals.setdefault(name, getattr(socket, name))
        setattr(socket, name, guarded)


def uninstall() -> None:
    for name in _CLASS_PATCHES:
        if name in _originals:
            setattr(socket.socket, name, _originals.pop(name))
    for name in _MODULE_PATCHES:
        if name in _originals:
            setattr(socket, name, _originals.pop(name))


def is_installed() -> bool:
    return all(
        getattr(socket.socket, name) is guarded for name, guarded in _CLASS_PATCHES.items()
    ) and all(getattr(socket, name) is guarded for name, guarded in _MODULE_PATCHES.items())


def drain_blocked() -> list[str]:
    """Return and forget the refusals recorded so far (any thread)."""
    with _lock:
        seen = list(_blocked)
        _blocked.clear()
    return seen


def failure_message() -> str | None:
    """What the per-test fixture reports at teardown; ``None`` when all is well.

    Catches a refusal that the code under test swallowed.
    """
    blocked = drain_blocked()
    if blocked:
        return "network access attempted during this test:\n  " + "\n  ".join(blocked)
    return None
