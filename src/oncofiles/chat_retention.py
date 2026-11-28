"""The #1022 chat history retention as a lifespan loop (the #999 pattern).

Maintainer decision 2026-11-28 (RUNBOOK lifecycle table): chat history moves
to the TRASH 90 days after it was written (or after its conversation was
restored), and its content is PURGED 30 days after that, like documents.
Never a direct hard delete: step one sets ``deleted_at``; step two NULLs
``body_enc`` (the encrypted text), ``citations_json``, ``grounding_json`` and
``caller_email_hash`` and sets ``purged_at``; the row itself stays (the #856
erasure of a patient on request removes it). Predicates and chunking:
``oncofiles.chat_retention_sql`` (shared with ``job_dryrun``).

**Ships off.** ``CHAT_RETENTION_MODE`` (runtime env) = ``off`` (default; the
loop runs and touches nothing) | ``report`` (counts what each step WOULD do,
writes nothing) | ``on`` (does it). An unknown value is ``off`` plus one
warning. Infra condition (issue #1022): ``on`` only after a ``job_dryrun
chat_history_retention`` count on a snapshot copy, the maintainer's
confirmation in the infra session and the RUNBOOK lifecycle row; the infra
session then checks the first real run.

Once per UTC day at ``SLOT_HOUR_UTC``:``SLOT_MINUTE_UTC`` (04:45 UTC, after the
access log and erase copy retentions), under ``_sync_semaphore``, in the
``local_only_task`` zone ``chat_history_retention``, through
``oncofiles.interval_job.DailyJob`` (startup grace, catch-up past the slot, at
most once per process and UTC day; both steps are idempotent). Telemetry on
operator ``/readiness.jobs.chat_history_retention`` (``mode``, counts,
``last_deleted`` = rows purged by the last run, and the storage API's
``chat_history`` counters) and in the admin job list; never ``/health``.
Logs carry counts only, never a patient id or text.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from oncofiles import chat_retention_sql as q
from oncofiles.interval_job import DailyJob

logger = logging.getLogger(__name__)

JOB_ID = "chat_history_retention"
# 04:45 UTC = 05:45 / 06:45 Bratislava: after the access log (04:25) and the
# erase copy (04:35) retentions, outside the curated nightly window.
SLOT_HOUR_UTC = 4
SLOT_MINUTE_UTC = 45

MODE_ENV = "CHAT_RETENTION_MODE"
MODES = ("off", "report", "on")
DEFAULT_MODE = "off"
_warned: set[str] = set()


def retention_mode() -> str:
    """``CHAT_RETENTION_MODE``; missing → off, unknown → off + one warning."""
    raw = os.environ.get(MODE_ENV, "").strip().lower()
    if not raw:
        return DEFAULT_MODE
    if raw not in MODES:
        if raw not in _warned:
            _warned.add(raw)
            logger.warning("%s: unknown value, treated as off", MODE_ENV)
        return DEFAULT_MODE
    return raw


def summarize(c: dict[str, int]) -> dict[str, int]:
    """Named totals of one pass (counts only)."""
    return {
        "trashed": c.get("trashed", 0),
        "purged": c.get("purged", 0),
        "would_trash": c.get("would_trash", 0),
        "would_purge": c.get("would_purge", 0),
    }


class ChatRetentionJob(DailyJob):
    """The daily job plus its mode and the storage API counters in ``status``."""

    def status(self) -> dict:
        from oncofiles import chat_history

        out = super().status()
        out["mode"] = retention_mode()
        out["chat_history"] = chat_history.counters()
        return out


CHAT_RETENTION = ChatRetentionJob(
    JOB_ID, SLOT_HOUR_UTC, SLOT_MINUTE_UTC, summarize=summarize, deleted_key="purged"
)
JOBS = {JOB_ID: CHAT_RETENTION}


async def chat_retention_iteration(
    db, semaphore: asyncio.Semaphore, *, now: datetime | None = None
) -> dict[str, int]:
    """One pass in the current mode; returns counts only, logs counts only."""
    mode = retention_mode()
    if mode == "off":
        return {"mode_off": 1}
    now = now or datetime.now(UTC)
    trash_cut, purge_cut = q.trash_cutoff(now), q.purge_cutoff(now)
    async with semaphore:
        if mode == "report":
            counts = await db.count_chat_retention(trash_cutoff=trash_cut, purge_cutoff=purge_cut)
            logger.info(
                "Chat history retention (report): would trash %d, would purge %d rows",
                counts["would_trash"],
                counts["would_purge"],
            )
            return {"mode_report": 1, **counts}
        # Purge first: a row trashed by this very pass never loses its text today.
        stamp = q.ts(now)
        purged = await db.purge_trashed_chat_messages(now=stamp, purge_cutoff=purge_cut)
        trashed = await db.trash_expired_chat_messages(now=stamp, trash_cutoff=trash_cut)
    logger.info("Chat history retention: trashed %d, purged %d rows", trashed, purged)
    return {"mode_on": 1, "trashed": trashed, "purged": purged}


async def chat_retention_loop(
    db, semaphore: asyncio.Semaphore, *, grace_s: float | None = None
) -> None:
    await CHAT_RETENTION.loop(lambda: chat_retention_iteration(db, semaphore), grace_s=grace_s)


def _reset_for_tests() -> None:
    CHAT_RETENTION.reset()
    _warned.clear()
