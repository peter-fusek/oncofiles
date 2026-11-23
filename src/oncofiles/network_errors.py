"""Transient network/DNS failures talking to Google (#979).

A home WAN outage makes the OAuth token refresh and every Drive call fail
with name-resolution or connection errors. That is NOT a missing or dead
Google connection: it must never be shown as a disconnect, never set
``needs_reauth`` / a hard-auth reason, and the curated nightly retries later
the same night instead of giving up.

``is_network_unavailable(exc)`` is the one classifier. It walks the
exception chain (``__cause__`` / ``__context__`` and exception args, bounded),
because httpx, httplib2 and google-auth wrap the socket error. Matching is by
type (or, for optional libraries, by class name and module) — never by the
message, so a credential error that happens to mention a host is not caught.
A ``RefreshError`` (``invalid_grant`` and friends) and any HTTP status error
are never network errors: the server answered.
"""

from __future__ import annotations

import errno
import socket

NETWORK_UNAVAILABLE = "network_unavailable"

_MAX_DEPTH = 6

# errno values of an unreachable network (OSError without a specific subclass).
_NET_ERRNOS = frozenset(
    e
    for e in (
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETDOWN", None),
        getattr(errno, "EHOSTDOWN", None),
        getattr(errno, "ETIMEDOUT", None),
    )
    if e is not None
)

# (module prefix, class name) of third-party network errors, matched on the MRO
# so subclasses (httpx.ConnectError < NetworkError < TransportError) count.
_NAMED: frozenset[tuple[str, str]] = frozenset(
    {
        ("httplib2", "ServerNotFoundError"),
        ("httpx", "NetworkError"),  # ConnectError, ReadError, WriteError, CloseError
        ("httpx", "TimeoutException"),  # Connect/Read/Write/PoolTimeout
        ("httpcore", "NetworkError"),
        ("httpcore", "TimeoutException"),
        ("google.auth.exceptions", "TransportError"),
        ("requests.exceptions", "ConnectionError"),
        ("requests.exceptions", "Timeout"),
        ("urllib3.exceptions", "NewConnectionError"),
        ("urllib3.exceptions", "NameResolutionError"),
        ("urllib3.exceptions", "ConnectTimeoutError"),
        ("urllib3.exceptions", "MaxRetryError"),
    }
)

# Never network errors, whatever they wrap: the server answered.
_ANSWERED: frozenset[tuple[str, str]] = frozenset(
    {
        ("google.auth.exceptions", "RefreshError"),
        ("httpx", "HTTPStatusError"),
        ("googleapiclient.errors", "HttpError"),
        ("requests.exceptions", "HTTPError"),
    }
)


def _matches(exc: BaseException, names: frozenset[tuple[str, str]]) -> bool:
    for cls in type(exc).__mro__:
        mod = getattr(cls, "__module__", "") or ""
        for prefix, name in names:
            if cls.__name__ == name and (mod == prefix or mod.startswith(prefix + ".")):
                return True
    return False


def _direct(exc: BaseException) -> bool:
    if isinstance(exc, socket.gaierror | ConnectionError | TimeoutError):
        return True
    if type(exc) is OSError and exc.errno in _NET_ERRNOS:
        return True
    return _matches(exc, _NAMED)


def is_network_unavailable(exc: BaseException | None, _depth: int = 0) -> bool:
    """True when ``exc`` (or what it wraps) is a transient network/DNS failure."""
    if exc is None or _depth > _MAX_DEPTH:
        return False
    if _matches(exc, _ANSWERED):
        return False
    if _direct(exc):
        return True
    nested: list[BaseException] = []
    for inner in (exc.__cause__, exc.__context__):
        if inner is not None and inner is not exc:
            nested.append(inner)
    nested.extend(a for a in getattr(exc, "args", ()) if isinstance(a, BaseException))
    return any(is_network_unavailable(inner, _depth + 1) for inner in nested)
