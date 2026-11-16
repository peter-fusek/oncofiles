"""Dashboard access audit trail (#868, #531 follow-up).

Three events, each written to ``dashboard_access_log`` (migration 090):

- ``sign_in``: a dashboard session was issued;
- ``acl_refused``: the dashboard patient ACL refused an authenticated caller
  (unknown patient, or a patient the caller is not caregiver of). Sign-in is
  open to any Google account, so a stranger could loop foreign ids: at most
  one row per caller + target patient + route per minute, and at most
  ``REFUSAL_DAILY_CAP`` rows per caller per UTC day. Beyond that only the
  counters move (``acl_refusals_suppressed``);
- ``patient_read``: an admin read a patient they are not caregiver of, at most
  once per session + patient per hour (in-memory dedupe, keeps rows few).

This is telemetry. ``record`` never raises and never changes the caller's
result: a failed write logs one warning and bumps ``write_failures``. The
caller is a keyed hash (``secrets_keys.caller_email_hash``), never the e-mail.

In-memory counters since process start feed operator ``/readiness``
(``dashboard_access``); public ``/health`` never sees them and never touches
the DB. Refusals and admin cross reads also log one line (persistent app log,
#706), throttled to once per minute per event + caller + patient.

Retention (maintainer decision 2026-11-16, #868): rows older than
``DASHBOARD_ACCESS_LOG_RETENTION_DAYS`` (default 90, minimum 30) are deleted
by the daily ``dashboard_access_log_retention`` job through
``purge_expired_access_log``, in chunks of at most 500 rows. Only this table:
it holds access telemetry, no patient documents or facts. Allowlisted in
``tests/test_no_automatic_deletion.py`` (#734).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

READ_DEDUPE_S = 3600.0
REFUSAL_DEDUPE_S = 60.0
REFUSAL_DAILY_CAP = 200  # rows per caller hash per UTC day
LOG_THROTTLE_S = 60.0
_MAX_KEYS = 10_000  # bound on every in-memory map; expired keys pruned first

RETENTION_ENV = "DASHBOARD_ACCESS_LOG_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 90
MIN_RETENTION_DAYS = 30
PURGE_CHUNK_ROWS = 500  # rows per DELETE + commit (never one long transaction)

_lock = threading.Lock()
_started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
_counters: dict[str, int] = {
    "sign_ins": 0,
    "acl_refusals": 0,
    "acl_refusals_anonymous": 0,
    "acl_refusals_suppressed": 0,
    "admin_cross_reads": 0,
    "write_failures": 0,
}
_read_seen: dict[tuple[str, str], float] = {}
_log_seen: dict[tuple[str, str, str], float] = {}
_refusal_seen: dict[tuple[str, str, str], float] = {}
_refusal_daily: dict[str, tuple[str, int]] = {}  # caller -> (UTC day, rows written)

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


def _utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _refusal_write_allowed(digest: str | None, patient_id: str | None, route: str) -> bool:
    """Dedupe + daily cap for ``acl_refused`` rows (counts toward the cap when True)."""
    caller = digest or "-"
    if not _first_within(_refusal_seen, (caller, patient_id or "-", route), REFUSAL_DEDUPE_S):
        return False
    today = _utc_day()
    with _lock:
        day, written = _refusal_daily.get(caller, (today, 0))
        if day != today:
            written = 0
        if written >= REFUSAL_DAILY_CAP:
            return False
        if caller not in _refusal_daily and len(_refusal_daily) >= _MAX_KEYS:
            for key in [k for k, (d, _) in _refusal_daily.items() if d != today]:
                del _refusal_daily[key]
            if len(_refusal_daily) >= _MAX_KEYS:
                return False  # still full today: suppress rather than grow
        _refusal_daily[caller] = (today, written + 1)
        return True


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
        if event == "acl_refused" and not _refusal_write_allowed(digest, patient_id, route):
            _bump("acl_refusals_suppressed")
            return
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


def retention_days() -> int:
    """Retention in days from ``DASHBOARD_ACCESS_LOG_RETENTION_DAYS``.

    Read at call time. Default 90; a value below 30 is clamped to 30 and an
    unparseable one falls back to 90, each with a warning.
    """
    raw = os.environ.get(RETENTION_ENV, "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer, using %d days", RETENTION_ENV, raw, DEFAULT_RETENTION_DAYS
        )
        return DEFAULT_RETENTION_DAYS
    if days < MIN_RETENTION_DAYS:
        logger.warning(
            "%s=%d is below the minimum, clamped to %d days",
            RETENTION_ENV,
            days,
            MIN_RETENTION_DAYS,
        )
        return MIN_RETENTION_DAYS
    return days


async def purge_expired_access_log(
    db: Any, *, days: int | None = None, chunk_size: int = PURGE_CHUNK_ROWS
) -> int:
    """Delete ``dashboard_access_log`` rows older than the retention; return the count.

    Touches no other table. Works in chunks of at most ``PURGE_CHUNK_ROWS``
    rows, each committed on its own, so a large backlog never becomes one long
    transaction on the single shared connection. ``days`` below the minimum is
    clamped like the env value. ``created_at`` is ISO-8601 UTC with ``T`` and
    ``Z`` (migration 090), so the cutoff uses the same format and the string
    comparison is chronological.
    """
    if days is None:
        days = retention_days()
    days = max(MIN_RETENTION_DAYS, int(days))
    chunk = max(1, min(PURGE_CHUNK_ROWS, int(chunk_size)))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = 0
    while True:
        async with db.db.execute(
            """DELETE FROM dashboard_access_log WHERE id IN (
                   SELECT id FROM dashboard_access_log
                   WHERE created_at < ? ORDER BY id LIMIT ?)""",
            (cutoff, chunk),
        ) as cursor:
            deleted = cursor.rowcount or 0
        await db.db.commit()
        total += deleted
        if deleted < chunk:
            return total
        await asyncio.sleep(0)  # let other coroutines use the connection between chunks


def _reset_for_tests() -> None:
    with _lock:
        for k in _counters:
            _counters[k] = 0
        _read_seen.clear()
        _log_seen.clear()
        _refusal_seen.clear()
        _refusal_daily.clear()
