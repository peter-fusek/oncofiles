"""One googleapiclient service (and so one ``httplib2.Http``) per thread (#579).

The rule: **a googleapiclient service / ``httplib2.Http`` object is never used
by two threads at once and never crosses threads.** httplib2 is not
thread-safe: two threads on one ``Http`` share one TLS connection, and
concurrent reads/writes on one OpenSSL connection fail with ``SSLError:
record layer failure`` at best (2026-11-08) and can crash the interpreter
natively at worst (the suspected cause of the SIGSEGVs of 2026-11-15/16).

The Google clients (``GDriveClient``, ``GmailClient``, ``CalendarClient``) are
cached per patient and reached from many threads: every ``asyncio.to_thread``
call lands on an arbitrary worker of the default executor, APScheduler jobs
run in its pool, curated runs and re-OCR jobs in theirs. Instead of asking
every caller to remember a copy, the client itself hands each thread its own
service: ``ThreadServices.get()`` builds lazily, once per (client, thread).

Cost: ``build()`` uses the discovery document bundled with
google-api-python-client (``static_discovery=True``), so it opens no socket;
it takes about a millisecond and a Drive service holds roughly 0.5 MB. The
real cost is one TLS connection per (client, thread) that makes a request.
A thread's services are released when the thread ends or the client is
dropped (client cache TTL).

Credentials stay shared between a client's threads (and its
``dedicated_copy()``s). They are plain Python state; the only mutation is a
token refresh, which ``serialize_refresh`` puts behind a lock. The refresh
request itself goes out on the refreshing thread's own ``Http``.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_REFRESH_LOCK_ATTR = "_oncofiles_refresh_lock"
_install_lock = threading.Lock()


def serialize_refresh(creds: Any) -> Any:
    """Make ``creds.refresh`` run in one thread at a time. Idempotent.

    A thread that waited while another one refreshed does not refresh again:
    it finds a new token and uses it. A forced refresh (after a 401) still
    happens when the token it saw is the one still in place.
    """
    if creds is None:
        return creds
    with _install_lock:
        if getattr(creds, _REFRESH_LOCK_ATTR, None) is not None:
            return creds
        try:
            original = creds.refresh
            lock = threading.Lock()

            def refresh(request, _original=original, _lock=lock):
                seen = getattr(creds, "token", None)
                with _lock:
                    current = getattr(creds, "token", None)
                    if current and current != seen:
                        return None  # refreshed by another thread while we waited
                    return _original(request)

            setattr(creds, _REFRESH_LOCK_ATTR, lock)
            creds.refresh = refresh
        except Exception:  # an exotic credentials type: leave it as it is
            logger.warning("credentials refresh could not be serialised", exc_info=True)
    return creds


def build_service(api: str, version: str, creds: Any) -> Any:
    """A new service with its own ``Http``. No network: bundled discovery document."""
    from googleapiclient.discovery import build

    return build(
        api,
        version,
        credentials=creds,
        cache_discovery=False,  # there is no usable cache with google-auth anyway
        static_discovery=True,  # never fetch the discovery document over HTTP
    )


def _close_http(service: Any) -> None:
    http = getattr(service, "_http", None)
    if http is not None and hasattr(http, "close"):
        with contextlib.suppress(Exception):
            http.close()


class ThreadServices:
    """The services of ONE client: one per thread, built on first use there."""

    def __init__(self, api: str, version: str, creds: Any) -> None:
        self.api = api
        self.version = version
        self.creds = serialize_refresh(creds)
        self._local = threading.local()
        self._pinned: Any = None

    def get(self) -> Any:
        """The calling thread's service."""
        if self._pinned is not None:
            return self._pinned
        service = getattr(self._local, "service", None)
        if service is None:
            service = build_service(self.api, self.version, self.creds)
            self._local.service = service
            logger.debug(
                "built %s %s service for thread %s",
                self.api,
                self.version,
                threading.current_thread().name,
            )
        return service

    def pin(self, service: Any) -> None:
        """Use ``service`` on every thread. Test doubles only (``client._service = fake``)."""
        self._pinned = service

    def rebuild(self) -> Any:
        """Fresh connection for the calling thread only; other threads keep theirs."""
        self.close()
        return self.get()

    def close(self) -> None:
        """Close the calling thread's connection (best effort).

        Never another thread's: closing an ``Http`` under a thread that is
        mid-request is the very thing this module exists to prevent. Other
        threads' connections close when the thread ends or the client goes.
        """
        service = getattr(self._local, "service", None)
        if service is not None:
            _close_http(service)
            del self._local.service


class PerThreadServiceMixin:
    """``self._service`` is the calling thread's service of ``_API``.

    Reading ``_service`` anywhere (methods, helpers that take a client, test
    code) yields the right object for the current thread; assigning it pins a
    test double. Instances made with ``cls.__new__`` (tests, ``from_oauth``)
    get their holder on first use.
    """

    _API: tuple[str, str] = ("", "")

    def _thread_services(self) -> ThreadServices:
        holder = self.__dict__.get("_services")
        if holder is None:
            holder = self.__dict__.setdefault(
                "_services", ThreadServices(*self._API, self.__dict__.get("_creds"))
            )
        return holder

    @property
    def _service(self) -> Any:
        return self._thread_services().get()

    @_service.setter
    def _service(self, service: Any) -> None:
        self._thread_services().pin(service)

    def _rebuild_service(self) -> None:
        """Fresh HTTP connection for the calling thread (after an SSL/connection error)."""
        self._thread_services().rebuild()

    def close(self) -> None:
        """Close the calling thread's HTTP connection (best effort)."""
        self._thread_services().close()
