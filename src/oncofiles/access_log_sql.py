"""The #868 access log retention predicate, shared by production and the dry run.

``dashboard_audit.purge_expired_access_log`` (run daily by the
``dashboard_access_log_retention`` loop, ``oncofiles.access_log_loop``) deletes
the ``dashboard_access_log`` rows matched by :data:`EXPIRED_WHERE` with the
cutoff of :func:`cutoff`; ``oncofiles.job_dryrun dashboard_access_log_retention``
(the #999 "check before" tool) counts the SAME rows on a read-only copy of the
production DB, so its numbers cannot drift from what the job deletes.

This module imports nothing from ``oncofiles`` (no config, no DB driver): the
dry run loads it without touching the environment. The retention bounds live
here too (``dashboard_audit`` re-exports them) so both sides clamp alike.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

DEFAULT_RETENTION_DAYS = 90
MIN_RETENTION_DAYS = 30
# ``created_at`` is ISO-8601 UTC with ``T`` and ``Z`` (migration 090), so a
# cutoff in the same format compares chronologically as a string.
CUTOFF_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Rows older than the cutoff (param: the cutoff text).
EXPIRED_WHERE = "created_at < ?"


def effective_days(days: int) -> int:
    """``days`` clamped to the minimum (an explicit value as well as the env's)."""
    return max(MIN_RETENTION_DAYS, int(days))


def cutoff(now: datetime, days: int) -> str:
    """The cutoff text for ``now`` and a retention of ``days`` (clamped)."""
    return (now.astimezone(UTC) - timedelta(days=effective_days(days))).strftime(CUTOFF_FORMAT)
