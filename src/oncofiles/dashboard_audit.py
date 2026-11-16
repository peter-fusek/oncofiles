"""Dashboard access audit trail (#868, #531 follow-up).

Three events, each written to ``dashboard_access_log`` (migration 090):

- ``sign_in``: a dashboard session was issued;
- ``acl_refused``: the dashboard patient ACL refused an authenticated caller
  (unknown patient, or a patient the caller is not caregiver of);
- ``patient_read``: an admin read a patient they are not caregiver of, at most
  once per session + patient per hour (in-memory dedupe, keeps rows few).

This is telemetry. ``record`` never raises and never changes the caller's
result: a failed write logs one warning and bumps ``write_failures``. The
caller is a keyed hash (``secrets_keys.caller_email_hash``), never the e-mail.

In-memory counters since process start feed operator ``/readiness``
(``dashboard_access``); public ``/health`` never sees them and never touches
the DB. Refusals and admin cross reads also log one line (persistent app log,
#706), throttled to once per minute per event + caller + patient.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

READ_DEDUPE_S = 3600.0
LOG_THROTTLE_S = 60.0
_MAX_KEYS = 10_000  # bound on both in-memory maps; expired keys pruned first

_lock = threading.Lock()
_started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
_counters: dict[str, int] = {
    "sign_ins": 0,
    "acl_refusals": 0,
    "acl_refusals_anonymous": 0,
    "admin_cross_reads": 0,
    "write_failures": 0,
}
_read_seen: dict[tuple[str, str], float] = {}
_log_seen: dict[tuple[str, str, str], float] = {}

_COUNTER_FOR_EVENT = {
    "sign_in": "sign_ins",
    "acl_refused": "acl_refusals",
    "patient_read": "admin_cross_reads",
}


def caller_hash(email: str | None) -> str | None:
    """Keyed hash of the caller (full hex) for the DB row, or None. Never raises."""
    try:
        from oncofiles.secrets_keys import caller_email_hash

        return caller_email_hash(email or "")
    except Exception:
        return None


def _short(digest: str | None) -> str:
    return digest[:12] if digest else "-"


def _prune(seen: dict, now: float, ttl: float) -> None:
    if len(seen) < _MAX_KEYS:
        return
    for key in [k for k, t in seen.items() if now - t >= ttl]:
        del seen[key]
    if len(seen) >= _MAX_KEYS:  # still full: drop the oldest half
        for key in sorted(seen, key=seen.get)[: len(seen) // 2]:
            del seen[key]


def _first_within(seen: dict, key: tuple, ttl: float) -> bool:
    """True when *key* was not seen within *ttl* seconds (and marks it seen)."""
    now = time.monotonic()
    with _lock:
        last = seen.get(key)
        if last is not None and now - last < ttl:
            return False
        _prune(seen, now, ttl)
        seen[key] = now
        return True


def should_record_read(session_key: str, patient_id: str) -> bool:
    """Dedupe for ``patient_read``: once per session + patient per hour."""
    return _first_within(_read_seen, (session_key, patient_id), READ_DEDUPE_S)


def _bump(name: str) -> None:
    with _lock:
        _counters[name] = _counters.get(name, 0) + 1


def count_anonymous_refusal() -> None:
    """A refusal without any caller identity: counted, never written to the DB
    (unauthenticated requests must not be able to grow the table)."""
    _bump("acl_refusals_anonymous")
    if _first_within(_log_seen, ("acl_refused", "-", "-"), LOG_THROTTLE_S):
        logger.warning("dashboard access: event=acl_refused caller=- (no identity)")


async def record(
    db: Any,
    event: str,
    *,
    email: str | None,
    is_admin: bool,
    patient_id: str | None,
    route: str,
) -> None:
    """Count, log (throttled) and write one audit row. Never raises."""
    try:
        digest = caller_hash(email)
        if event in _COUNTER_FOR_EVENT:
            _bump(_COUNTER_FOR_EVENT[event])
        if event in ("acl_refused", "patient_read") and _first_within(
            _log_seen, (event, _short(digest), patient_id or "-"), LOG_THROTTLE_S
        ):
            logger.warning(
                "dashboard access: event=%s caller=%s admin=%d patient=%s route=%s",
                event,
                _short(digest),
                1 if is_admin else 0,
                patient_id or "-",
                route,
            )
        await db.insert_dashboard_access(
            event=event,
            caller_hash=digest,
            is_admin=is_admin,
            target_patient_id=patient_id,
            route=route,
        )
    except Exception as exc:
        try:
            _bump("write_failures")
            logger.warning(
                "dashboard access: audit write failed (%s) event=%s", type(exc).__name__, event
            )
        except Exception:
            pass


def route_name(request: Any) -> str:
    """Name of the endpoint handling *request* (no path params, no query)."""
    try:
        endpoint = request.scope.get("endpoint")
        name = getattr(endpoint, "__name__", None)
        if isinstance(name, str) and name:
            return name[:120]
        path = request.url.path
        if isinstance(path, str) and path:
            return path[:120]
    except Exception:
        pass
    return "unknown"


def counters() -> dict:
    """In-memory counters since process start, for operator ``/readiness``."""
    with _lock:
        return {"since": _started_at, **_counters}


def _reset_for_tests() -> None:
    with _lock:
        for k in _counters:
            _counters[k] = 0
        _read_seen.clear()
        _log_seen.clear()
