"""Google Calendar API client for reading medical appointments."""

from __future__ import annotations

import contextlib
import functools
import logging
import ssl
import time
from typing import Any

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}
_MAX_RETRIES = 3
_INITIAL_BACKOFF = 1.0
_CONNECTION_ERRORS = (ssl.SSLError, ConnectionError, OSError, BrokenPipeError, TimeoutError)


def _is_connection_error(exc: Exception) -> bool:
    if isinstance(exc, _CONNECTION_ERRORS):
        return True
    msg = str(exc).lower()
    return any(
        s in msg for s in ("ssl", "record layer", "broken pipe", "connection reset", "eof occurred")
    )


def _retry_on_transient(func):
    """Retry decorator for transient Google API errors (429/5xx) and SSL errors."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(_MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                status = getattr(getattr(e, "resp", None), "status", None)
                is_http_transient = status in _RETRYABLE_STATUS_CODES
                is_conn_error = _is_connection_error(e)
                if not is_http_transient and not is_conn_error:
                    raise
                last_exc = e
                backoff = _INITIAL_BACKOFF * (2**attempt)
                logger.warning(
                    "Calendar API %s failed (%s), retry %d/%d in %.1fs",
                    func.__name__,
                    str(e)[:120],
                    attempt + 1,
                    _MAX_RETRIES,
                    backoff,
                )
                if is_conn_error and args and hasattr(args[0], "_rebuild_service"):
                    with contextlib.suppress(Exception):
                        args[0]._rebuild_service()
                time.sleep(backoff)
        raise last_exc

    return wrapper


class CalendarClient:
    """Read-only Google Calendar API client for scanning medical appointments."""

    def __init__(self, credentials: Any) -> None:
        from googleapiclient.discovery import build

        self._creds = credentials
        self._service = build("calendar", "v3", credentials=credentials)

    def _rebuild_service(self) -> None:
        from googleapiclient.discovery import build

        self._service = build("calendar", "v3", credentials=self._creds)

    @classmethod
    def from_oauth(
        cls,
        access_token: str,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> CalendarClient:
        """Create a CalendarClient from OAuth 2.0 user credentials."""
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
        )
        instance = cls.__new__(cls)
        instance._creds = creds
        instance._service = build("calendar", "v3", credentials=creds)
        return instance

    @_retry_on_transient
    def list_events(
        self,
        time_min: str | None = None,
        time_max: str | None = None,
        query: str | None = None,
        max_results: int = 250,
        page_token: str | None = None,
        calendar_id: str = "primary",
    ) -> dict:
        """List calendar events. time_min/max are RFC3339 strings."""
        params: dict[str, Any] = {
            "calendarId": calendar_id,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        return self._service.events().list(**params).execute()

    @_retry_on_transient
    def get_event(self, event_id: str, calendar_id: str = "primary") -> dict:
        """Get a calendar event by ID."""
        return self._service.events().get(calendarId=calendar_id, eventId=event_id).execute()
