"""Counts-only activity log of one patient for the dashboard (``GET /api/activity``).

Two sources, merged newest first:

* real curated runs (``curated_runs`` via ``Database.list_curated_run_activity``:
  no dry runs or previews, none of the non-run jobs),
* legacy Drive syncs (``sync_history`` via ``Database.get_sync_history``).

An event is a time, a kind, a normalised status, the trigger code, a duration
and counts. It never carries a file name, a run message, an error text or a
document value: the builders below read named columns only and never pass a
row through.

Pure functions: rows in, events out. No DB, no I/O.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

KIND_CURATED_RUN = "curated_run"
KIND_DRIVE_SYNC = "drive_sync"

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_RUNNING = "running"
STATUS_INTERRUPTED = "interrupted"
STATUSES = (STATUS_OK, STATUS_FAILED, STATUS_RUNNING, STATUS_INTERRUPTED)

# Stored status → what the dashboard shows. Anything not listed (a curated
# run's ``aborted``, ``failed``, an unknown value) is ``failed``.
_STATUS = {
    "ok": STATUS_OK,
    "completed": STATUS_OK,  # sync_history
    "running": STATUS_RUNNING,
    "interrupted": STATUS_INTERRUPTED,  # the process stopped during the run
    "timeout": STATUS_INTERRUPTED,  # sync_history: a stale "running" row
}

# A trigger is a code written by our own code (auto, nightly, manual, …).
# Anything that does not look like one is reported as "other", never echoed.
_TRIGGER_RE = re.compile(r"[a-z0-9_.:-]{1,32}")
TRIGGER_OTHER = "other"

# A real curated run also writes a ``sync_history`` row with this trigger. The
# ``curated_runs`` row is the record; the mirror would show the run twice.
CURATED_SYNC_TRIGGER = "curated"

EVENT_KEYS = frozenset({"at", "kind", "status", "trigger", "duration_s", "counts"})
CURATED_COUNT_KEYS = frozenset({"documents", "filed", "review", "not_processed"})
SYNC_COUNT_KEYS = frozenset({"new", "errors"})


def normalise_status(raw) -> str:
    return _STATUS.get(str(raw or "").strip().lower(), STATUS_FAILED)


def trigger_code(raw) -> str:
    value = str(raw or "").strip().lower()
    return value if _TRIGGER_RE.fullmatch(value) else TRIGGER_OTHER


def _parse_time(value) -> datetime | None:
    """A stored timestamp as an aware UTC datetime (a naive one is UTC)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso_utc(value: datetime | None) -> str | None:
    """``2027-03-04T05:06:07Z``; a naive datetime is taken as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _seconds(value) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return round(seconds, 1) if seconds >= 0 else None


def curated_run_event(row: dict) -> tuple[datetime | None, dict]:
    """One ``list_curated_run_activity`` row as ``(start time, event)``."""
    started = _parse_time(row.get("started_at"))
    finished = _parse_time(row.get("finished_at"))
    duration = (
        _seconds((finished - started).total_seconds())
        if started is not None and finished is not None
        else None
    )
    return started, {
        "at": iso_utc(started),
        "kind": KIND_CURATED_RUN,
        "status": normalise_status(row.get("status")),
        "trigger": trigger_code(row.get("trigger")),
        "duration_s": duration,
        "counts": {
            "documents": _count(row.get("documents")),
            "filed": _count(row.get("filed")),
            "review": _count(row.get("review")),
            "not_processed": _count(row.get("not_processed")),
        },
    }


def drive_sync_event(row: dict) -> tuple[datetime | None, dict] | None:
    """One ``sync_history`` row as ``(start time, event)``.

    None for the mirror row of a curated run (already listed as that run).
    """
    raw_trigger = str(row.get("sync_trigger") or "").strip().lower()
    if raw_trigger == CURATED_SYNC_TRIGGER:
        return None
    started = _parse_time(row.get("started_at"))
    return started, {
        "at": iso_utc(started),
        "kind": KIND_DRIVE_SYNC,
        "status": normalise_status(row.get("status")),
        "trigger": trigger_code(raw_trigger),
        "duration_s": _seconds(row.get("duration_s")),
        "counts": {
            "new": _count(row.get("from_gdrive_new")),
            "errors": _count(row.get("from_gdrive_errors")) + _count(row.get("to_gdrive_errors")),
        },
    }


def merge(curated_runs: list[dict], drive_syncs: list[dict], *, limit: int) -> list[dict]:
    """Both sources as one list, newest first, at most ``limit`` events.

    An event without a readable start time sorts last.
    """
    timed = [curated_run_event(dict(r)) for r in curated_runs]
    timed += [e for r in drive_syncs if (e := drive_sync_event(dict(r))) is not None]
    floor = datetime.min.replace(tzinfo=UTC)
    timed.sort(key=lambda pair: pair[0] or floor, reverse=True)
    return [event for _, event in timed[: max(0, limit)]]
