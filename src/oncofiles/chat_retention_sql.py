"""The #1022 chat history retention predicates, shared by production and the dry run.

Maintainer decision 2026-11-28 (RUNBOOK lifecycle): a ``chat_messages`` row
moves to the TRASH 90 days after it was written (or after its conversation
was restored from the trash), and its content is PURGED 30 days after that.
Never a direct hard delete: step one only sets ``deleted_at``; step two only
NULLs the content columns and sets ``purged_at`` (the row stays, like a purged
quarantine item). ``database._chat`` runs these statements for the
``chat_history_retention`` loop (``oncofiles.chat_retention``), and
``oncofiles.job_dryrun chat_history_retention`` counts the SAME rows on a
read-only copy of the production DB, so the counts cannot drift.

This module imports nothing from ``oncofiles`` (no config, no DB driver): the
dry run loads it without touching the environment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

TRASH_AFTER_DAYS = 90
PURGE_AFTER_TRASH_DAYS = 30
# ``created_at`` / ``restored_at`` / ``deleted_at`` are ISO-8601 UTC with ``T``
# and ``Z`` (migration 106): a cutoff in the same format compares as a string.
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# ``deleted_by`` of a row the retention moved to the trash (a user's delete
# writes ``user``).
RETENTION_DELETED_BY = "retention"
USER_DELETED_BY = "user"

# Step 1, live rows past the 90 days (param: the trash cutoff). A restored
# conversation counts from its restore.
TRASH_WHERE = "deleted_at IS NULL AND purged_at IS NULL AND COALESCE(restored_at, created_at) < ?"
# Step 2, trashed rows past the 30 days in the trash (param: the purge cutoff).
PURGE_WHERE = "deleted_at IS NOT NULL AND purged_at IS NULL AND deleted_at < ?"
# What a purge clears: the text and everything derived from the conversation.
PURGE_SET = (
    "body_enc = NULL, citations_json = NULL, grounding_json = NULL, "
    "caller_email_hash = NULL, purged_at = ?"
)
# At most this many rows per statement (committed per chunk).
CHUNK_ROWS = 500


def ts(now: datetime) -> str:
    return now.astimezone(UTC).strftime(TS_FORMAT)


def trash_cutoff(now: datetime) -> str:
    return ts(now - timedelta(days=TRASH_AFTER_DAYS))


def purge_cutoff(now: datetime) -> str:
    return ts(now - timedelta(days=PURGE_AFTER_TRASH_DAYS))


def trash_sql() -> str:
    """One chunk of step 1 (params: now, deleted_by, trash cutoff, chunk)."""
    return (
        "UPDATE chat_messages SET deleted_at = ?, deleted_by = ? WHERE id IN "
        f"(SELECT id FROM chat_messages WHERE {TRASH_WHERE} ORDER BY id LIMIT ?)"
    )


def purge_sql() -> str:
    """One chunk of step 2 (params: now, purge cutoff, chunk)."""
    return (
        f"UPDATE chat_messages SET {PURGE_SET} WHERE id IN "
        f"(SELECT id FROM chat_messages WHERE {PURGE_WHERE} ORDER BY id LIMIT ?)"
    )
