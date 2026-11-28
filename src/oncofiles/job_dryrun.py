"""Read-only "check before" counts of a background job (#999).

Before a job moves out of APScheduler into its own lifespan loop, the
maintainer wants to know what its first run in production would do. Infra runs
this on a snapshot COPY of the production DB (and, where the job reads files,
a copy of the data directory)::

    uv run python -m oncofiles.job_dryrun holding_move --db /tmp/x/oncofiles.db \\
        [--data-dir /tmp/x/data] [--now 2027-01-02T03:04:05Z]

One subcommand per job (``memory_check`` touches no DB and has none).
``oauth_token_cleanup`` (the MCP OAuth token cleanup loop) needs no data
directory and no option, nor does ``chat_history_retention`` (the #1022
chat history loop: rows it would move to the trash and purge with
``CHAT_RETENTION_MODE=on``). ``dashboard_access_log_retention`` (the #868
access log retention loop) takes ``--retention-days`` (stands for
``DASHBOARD_ACCESS_LOG_RETENTION_DAYS``, default 90, clamped to 30 like the
job) and needs no data directory. ``inbound_maintenance``
(the ``inbound_queue`` loop: claimed rows, the queue, quarantine expiry /
digests / purge) also takes ``--purge-approved`` (default off, like
``QUARANTINE_PURGE_APPROVED``) and ``--purge-approved-after-days``; its
quarantine files live under ``<data-dir>/quarantine``. Guarantees, like ``oncofiles.index_diff``:

* **Read-only.** The DB is opened through a ``file:…?mode=ro&immutable=1``
  URI plus ``PRAGMA query_only`` (:func:`open_snapshot`: unlike plain
  ``mode=ro`` it creates no ``-shm`` / ``-wal`` file next to the copy; a copy
  with a ``-wal`` file is refused, make it with ``sqlite3 <db> ".backup
  <copy>"``); every statement is a SELECT. Files are only checked with
  ``Path.is_file`` / ``is_dir``: a missing
  holding directory is reported as missing and never created, nor is anything
  else written anywhere.
* **Offline, no environment.** The socket layer is disabled
  (``index_diff.block_network``) before any job module is imported. Only
  import-free modules are loaded (no ``oncofiles.config``, so no data
  directory is created and no env var or secret is read); no Drive, model or
  SMTP call is possible.
* **The production predicates.** The SQL is ``oncofiles.holding_sql`` /
  ``oncofiles.inbound_sql`` / ``oncofiles.access_log_sql`` /
  ``oncofiles.oauth_cleanup_sql`` / ``oncofiles.chat_retention_sql``, the very
  statements the ``database`` helpers run for the jobs (and the purge's own
  path checks, ``purge_plan``), and the limits are the jobs' own constants,
  so the counts cannot drift.
* **Counts only.** The JSON output holds counts, booleans and constants, never
  a patient id, name, address, file name or document text.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

TOOL_VERSION = 1
_TS = "%Y-%m-%dT%H:%M:%SZ"  # the timestamp text the holding jobs write and compare


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_TS)


def _parse_now(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _held_file(holding_dir: Path, file_id: str) -> Path | None:
    """The held file of a ``holding:<uuid>`` id, or None for a malformed id."""
    from oncofiles import holding_store

    try:
        uid = holding_store._uuid_of(file_id)
    except holding_store.HoldingStoreError:
        return None
    return holding_dir / f"{uid}.bin"


# ── holding_move (+ the holding_extract backlog it shares a store with) ──


def dry_holding_move(
    conn: sqlite3.Connection, *, now: datetime, data_dir: Path | None, **_opts
) -> dict:
    """What one ``holding_loops`` mover pass (``holding_move.run_pending``)
    would do now, plus the reader's backlog. Not modelled offline: the curated
    inbox listing (a held file already in the inbox is counted as a copy here;
    the real run records it as ``present`` instead, so ``would_copy`` is an
    upper bound), a copy failing midway (the real run stops that patient), and
    a "ready" mail that becomes due only because this pass copied a trial's
    last unread row (``ready_mail`` is evaluated on the DB as it is now)."""
    from oncofiles import holding_extract, holding_move, holding_store, trial_funnel
    from oncofiles import holding_sql as q

    holding_dir = data_dir / "holding" if data_dir is not None else None
    dir_exists = holding_dir.is_dir() if holding_dir is not None else None
    stale_claim = _ts(now - timedelta(minutes=holding_move.STALE_CLAIM_MIN))

    trials = [r[0] for r in conn.execute(q.TRIALS_TO_CONVERT_SQL)]
    move = {
        "trials_eligible": len(trials),  # Drive connected, not converted
        "trials_claimed_by_live_run": 0,  # the real pass gets claim_refused
        "trials_would_convert": 0,  # every held row copied or present after this pass
        "trials_deferred": 0,  # over the per-run file budget
        "held_rows_to_move": 0,
        "held_rows_present_in_filed_tree": 0,  # recorded via=present, no Drive write
        "held_rows_would_copy": 0,  # upper bound (inbox listing not checked offline)
        "held_rows_would_copy_this_run": 0,  # within MAX_FILES_PER_RUN
        "held_rows_already_moved": 0,
        "held_files_missing": None if holding_dir is None else 0,
        "max_files_per_run": holding_move.MAX_FILES_PER_RUN,
    }
    budget = holding_move.MAX_FILES_PER_RUN
    present_this_run = 0  # recorded as present -> their mirror row supersedes them
    for pid in trials:
        move["held_rows_already_moved"] += int(
            conn.execute(q.HOLDING_MOVED_COUNT_SQL, (pid,)).fetchone()[0]
        )
        rows = [dict(r) for r in conn.execute(q.HOLDING_ROWS_TO_MOVE_SQL, (pid,))]
        move["held_rows_to_move"] += len(rows)
        if holding_dir is not None:
            for r in rows:
                path = _held_file(holding_dir, r["file_id"])
                if path is None or not path.is_file():
                    move["held_files_missing"] += 1
        state = {}
        for m in conn.execute(q.CURATED_MIRROR_ROWS_SQL, (pid,)):
            try:
                meta = json.loads(m["structured_metadata"] or "{}")
            except ValueError:
                meta = {}
            meta = meta if isinstance(meta, dict) else {}
            state[m["gdrive_id"]] = {
                "source_md5": (meta.get("curated") or {}).get("source_md5", ""),
                "deleted": m["deleted_at"] is not None,
                "foreign": q.is_foreign_mirror(meta),
            }
        filed = set(q.filed_md5s(state))
        md5s = [
            str(holding_store.holding_meta(r["structured_metadata"]).get("md5") or "") for r in rows
        ]
        move["held_rows_present_in_filed_tree"] += sum(1 for m in md5s if m and m in filed)
        move["held_rows_would_copy"] += sum(1 for m in md5s if not (m and m in filed))
        claimed = conn.execute(
            "SELECT move_claimed_at FROM trial_accounts WHERE patient_id = ?", (pid,)
        ).fetchone()[0]
        if claimed and claimed >= stale_claim:
            move["trials_claimed_by_live_run"] += 1
            continue
        # The pass's file budget is shared by every trial, in this order; the
        # same row walk as holding_move._move_claimed: a present md5 is only
        # recorded, a copy counts against the budget and makes its md5 present.
        if budget <= 0:
            move["trials_deferred"] += 1
            continue
        written, complete = 0, True
        for md5 in md5s:
            if md5 and md5 in filed:
                present_this_run += 1
                continue
            if written >= budget:
                complete = False
                break
            written += 1
            if md5:
                filed.add(md5)
        move["held_rows_would_copy_this_run"] += written
        budget -= written
        if complete:  # every row copied or present: the trial converts
            move["trials_would_convert"] += 1

    # moved rows whose mirror exists now, plus the rows this pass records as
    # present in the filed tree (their mirror row is that very md5's row)
    superseded = len(conn.execute(q.SUPERSEDED_CANDIDATES_SQL).fetchall()) + present_this_run

    due = conn.execute(q.TRIALS_AWAITING_READY_MAIL_SQL).fetchall()
    with_recipient = 0
    for pid, *_counts in due:
        row = conn.execute(q.PATIENT_CAREGIVER_SQL, (pid,)).fetchone()
        if row is not None and q.first_recipient(row[0]):
            with_recipient += 1
    ready = {
        "trials_due": len(due),
        "trials_due_without_recipient": len(due) - with_recipient,
        "would_send_now": min(with_recipient, trial_funnel.READY_MAILS_PER_HOUR),
        "max_per_hour": trial_funnel.READY_MAILS_PER_HOUR,
    }

    stale_reading = _ts(now - timedelta(minutes=holding_extract.STALE_READING_MIN))
    pending = int(
        conn.execute(
            "SELECT COUNT(*) " + q.HOLDING_DUE_FOR_READING_FROM, (_ts(now), stale_reading)
        ).fetchone()[0]
    )
    return {
        "holding_dir": {"given": holding_dir is not None, "exists": dir_exists},
        "move": move,
        "superseded_would_flag": superseded,
        "ready_mail": ready,
        "extract": {
            "held_docs_pending": pending,
            "max_docs_per_run": holding_extract.MAX_DOCS_PER_RUN,
        },
        "not_checked_offline": ["curated_inbox_listing", "holding_store_secret", "local_model"],
    }


# ── inbound_queue sweep: claimed rows, the queue, quarantine maintenance ──


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def _dry_claimed(conn: sqlite3.Connection, *, now: datetime) -> dict:
    """The claimed bucket's first step of a pass (``_verify_one``): deferred
    rows expired / promoted (``promote_deferred_claimed``), then what is
    claimable. Finished rows may free more slots during the pass, so
    ``deferred_would_promote`` is the first promotion only."""
    from oncofiles import inbound_queue as iq
    from oncofiles import inbound_sql as q

    expired_before = _ts(now - timedelta(seconds=iq.BREVO_ATTACHMENT_RETENTION_S))
    stale_before = _ts(now - timedelta(minutes=iq.STALE_PROCESSING_MIN))
    retention_before = expired_before
    expired = _count(
        conn,
        "SELECT COUNT(*) FROM inbound_claimed_jobs WHERE " + q.CLAIMED_DEFERRED_EXPIRED_WHERE,
        (expired_before,),
    )
    caps = iq._claimed_caps()
    added: dict[tuple, int] = {}
    promoted = 0
    over: dict[str, int] = {}
    for row in conn.execute(q.CLAIMED_DEFERRED_SQL).fetchall():
        row = dict(row)
        if row["created_at"] < expired_before:  # expired by the same UPDATE first
            continue
        cap_hit = None
        for cap, key, where, params in q.claimed_checks(row, deferred=False):
            n = _count(conn, q.claimed_count_sql(q.CLAIMED_OPEN_STATES, where), params)
            if n + added.get(key, 0) >= caps[cap]:
                cap_hit = cap
                break
        if cap_hit is not None:
            over[cap_hit] = over.get(cap_hit, 0) + 1
            continue
        for _cap, key, _w, _p in q.claimed_checks(row, deferred=False):
            added[key] = added.get(key, 0) + 1
        promoted += 1
    rows = conn.execute(
        "SELECT state, attempts, created_at FROM inbound_claimed_jobs WHERE "
        + q.CLAIMED_CLAIMABLE_WHERE,
        (_ts(now), stale_before),
    ).fetchall()
    max_attempts = sum(1 for r in rows if r["attempts"] >= iq.MAX_ATTEMPTS)
    too_old = sum(
        1 for r in rows if r["attempts"] < iq.MAX_ATTEMPTS and r["created_at"] < retention_before
    )
    claimable = len(rows) + promoted  # a promoted row is due at once
    return {
        "deferred": _count(conn, q.claimed_count_sql(q.CLAIMED_DEFERRED_STATES)),
        "deferred_would_expire": expired,  # failed / download_expired
        "deferred_would_promote": promoted,
        "deferred_held_by_cap": dict(sorted(over.items())),
        "claimable": claimable,
        "claimable_this_run": min(claimable, iq.MAX_JOBS_PER_RUN),
        "would_fail_max_attempts": max_attempts,
        # failed / download_expired once decrypted (an undecryptable payload
        # fails as undecryptable first)
        "would_fail_download_expired": too_old,
        # EML download + DKIM / DNS: not run offline
        "would_verify": claimable - max_attempts - too_old,
        "by_state": _group(rows, "state"),
    }


def _group(rows, key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        value = str(r[key] or "new")
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def _dry_jobs(conn: sqlite3.Connection, *, now: datetime) -> dict:
    """``inbound_jobs`` claimable now (``next_inbound_job``), by state and
    reason code. Rows the claimed step promotes in the same pass are not
    included (their verdict needs the EML and DNS)."""
    from oncofiles import inbound_queue as iq
    from oncofiles import inbound_sql as q

    stale_before = _ts(now - timedelta(minutes=iq.STALE_PROCESSING_MIN))
    retention_before = _ts(now - timedelta(seconds=iq.BREVO_ATTACHMENT_RETENTION_S))
    rows = conn.execute(
        "SELECT state, reason, attempts, created_at FROM inbound_jobs WHERE "
        + q.JOB_CLAIMABLE_WHERE,
        (_ts(now), stale_before),
    ).fetchall()
    max_attempts = sum(1 for r in rows if r["attempts"] >= iq.MAX_ATTEMPTS)
    too_old = sum(
        1 for r in rows if r["attempts"] < iq.MAX_ATTEMPTS and r["created_at"] < retention_before
    )
    return {
        "claimable": len(rows),
        "claimable_this_run": min(len(rows), iq.MAX_JOBS_PER_RUN),
        "by_state": _group(rows, "state"),
        "by_reason": _group(rows, "reason"),
        "would_fail_max_attempts": max_attempts,
        # failed / download_expired when the payload lists attachments (the
        # payload is encrypted: an upper bound offline)
        "older_than_attachment_retention": too_old,
        "max_jobs_per_run": iq.MAX_JOBS_PER_RUN,
    }


def _dry_notices(conn: sqlite3.Connection, *, now: datetime) -> dict:
    """The digests ``run_maintenance`` would send now (``notify_due`` per
    patient: daily cap, per-sender hour, recipient, ``MAX_ITEMS_PER_NOTICE``).
    Not modelled: a send refused within ``NOTICE_RETRY_S`` (in memory)."""
    from oncofiles import holding_sql
    from oncofiles import inbound_quarantine as iqr
    from oncofiles import inbound_sql as q

    day_ago = _ts(now - timedelta(days=1))
    hour_ago = _ts(now - timedelta(seconds=iqr.NOTIFY_SENDER_INTERVAL_S))
    out = {
        "patients_due": 0,
        "would_send": 0,
        "patients_daily_cap": 0,
        "patients_throttled": 0,
        "patients_without_recipient": 0,
        "items_announced": 0,
        "items_with_buttons": 0,
        "items_dashboard_only": 0,  # announced beyond MAX_ITEMS_PER_NOTICE
        "items_waiting_for_digest": 0,  # sender announced within the hour
        "max_notices_per_day": iqr.MAX_NOTICES_PER_DAY,
        "max_items_per_notice": iqr.MAX_ITEMS_PER_NOTICE,
    }
    for (pid,) in conn.execute(q.QUARANTINE_PATIENTS_DUE_SQL, (day_ago,)).fetchall():
        out["patients_due"] += 1
        items = [
            dict(r)
            for r in conn.execute(
                "SELECT id, sender_hash " + q.QUARANTINE_UNNOTIFIED_FROM, (pid, day_ago)
            )
        ]
        if not items:
            continue
        if _count(conn, q.QUARANTINE_NOTICES_SINCE_SQL, (pid, day_ago)) >= iqr.MAX_NOTICES_PER_DAY:
            out["patients_daily_cap"] += 1
            continue
        last = {}
        for it in items:
            sh = it["sender_hash"]
            if sh not in last:
                row = conn.execute(q.QUARANTINE_LAST_NOTICE_SQL, (pid, sh)).fetchone()
                last[sh] = row[0] if row and row[0] else None
        due = q.due_for_notice(items, last, hour_ago)
        out["items_waiting_for_digest"] += len(items) - len(due)
        if not due:
            out["patients_throttled"] += 1
            continue
        row = conn.execute(holding_sql.PATIENT_CAREGIVER_SQL, (pid,)).fetchone()
        if row is None or not iqr._recipient(SimpleNamespace(caregiver_email=row[0])):
            out["patients_without_recipient"] += 1
            continue
        out["would_send"] += 1
        out["items_announced"] += len(due)
        out["items_with_buttons"] += min(len(due), iqr.MAX_ITEMS_PER_NOTICE)
        out["items_dashboard_only"] += max(len(due) - iqr.MAX_ITEMS_PER_NOTICE, 0)
    return out


def _dry_purge(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    data_dir: Path | None,
    purge_approved: bool,
    purge_approved_after_days: int | None,
) -> dict:
    """What the quarantine purge would remove now: the encrypted files of rejected /
    expired items past ``TRASH_DAYS``, approved ones only with
    ``purge_approved`` (mirrors ``QUARANTINE_PURGE_APPROVED``, default off).
    The path checks are ``inbound_quarantine.purge_plan`` itself."""
    from oncofiles import inbound_quarantine as iqr
    from oncofiles import inbound_sql as q

    days = max(1, int(purge_approved_after_days or iqr.PURGE_APPROVED_AFTER_DAYS))
    trash_before = _ts(now - timedelta(days=iqr.TRASH_DAYS))
    approved_before = _ts(now - timedelta(days=days)) if purge_approved else None
    rows = conn.execute(
        q.QUARANTINE_PURGE_CANDIDATES_SQL,
        (trash_before, approved_before, approved_before, iqr.PURGE_MAX_ITEMS),
    ).fetchall()
    root = data_dir / "quarantine" if data_dir is not None else None
    out = {
        "items": 0,  # rejected / expired -> state purged
        "approved_items": 0,  # files only, state stays approved
        "files": 0,
        "bytes": None if root is None else 0,  # on disk (needs --data-dir)
        "manifest_bytes": 0,  # plaintext sizes the rows record
        "files_missing": None if root is None else 0,
        "refused": None if root is None else 0,  # path checks of the purge
        "approved_switch": bool(purge_approved),
        "approved_after_days": days,
        "trash_days": iqr.TRASH_DAYS,
        "max_items_per_run": iqr.PURGE_MAX_ITEMS,
    }
    for r in rows:
        state = r["state"]
        if state == "approved" and not purge_approved:
            continue
        if state not in ("approved", "rejected", "expired"):
            continue
        manifest = conn.execute(
            "SELECT total_bytes FROM inbound_quarantine WHERE id = ?", (r["id"],)
        ).fetchone()[0]
        if root is not None:
            plan = iqr.purge_plan(root, r["item_uuid"], int(r["file_count"]))
            if plan.refused:
                out["refused"] += 1
                continue
            out["files"] += len(plan.targets)
            out["bytes"] += plan.bytes
            out["files_missing"] += plan.missing
        else:
            out["files"] += int(r["file_count"])
        out["manifest_bytes"] += int(manifest or 0)
        out["approved_items" if state == "approved" else "items"] += 1
    return out


def dry_inbound_maintenance(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    data_dir: Path | None,
    purge_approved: bool = False,
    purge_approved_after_days: int | None = None,
    **_opts,
) -> dict:
    """What one ``inbound_loop`` pass (``inbound_queue.run_cycle``) would do
    now: the claimed bucket, the queue, then ``run_maintenance`` (expiry,
    purge, digests). Not modelled offline: payload contents (encrypted),
    the EML download and DKIM / DNS verdicts, the SMTP relay, and the two
    secrets without which a pass touches nothing (``INBOUND_JOBS_SECRET``
    for the whole pass, ``HOLDING_STORE_SECRET`` for the quarantine)."""
    from oncofiles import inbound_quarantine as iqr
    from oncofiles import inbound_sql as q

    root = data_dir / "quarantine" if data_dir is not None else None
    expire_before = _ts(now - timedelta(days=iqr.EXPIRE_AFTER_DAYS))
    stale_claim = _ts(now - timedelta(minutes=iqr.CLAIM_STALE_MIN))
    return {
        "quarantine_dir": {
            "given": root is not None,
            "exists": root.is_dir() if root is not None else None,
        },
        "claimed": _dry_claimed(conn, now=now),
        "jobs": _dry_jobs(conn, now=now),
        "quarantine": {
            "would_expire": _count(
                conn,
                "SELECT COUNT(*) FROM inbound_quarantine WHERE " + q.QUARANTINE_EXPIRE_WHERE,
                (expire_before, stale_claim),
            ),
            "expire_after_days": iqr.EXPIRE_AFTER_DAYS,
            "notices": _dry_notices(conn, now=now),
            "purge": _dry_purge(
                conn,
                now=now,
                data_dir=data_dir,
                purge_approved=purge_approved,
                purge_approved_after_days=purge_approved_after_days,
            ),
        },
        "not_checked_offline": [
            "eml_dkim_dns",
            "holding_store_secret",
            "inbound_jobs_secret",
            "payload_contents",
            "smtp_relay",
        ],
    }


# ── dashboard_access_log_retention (#868) ───────────────────────────────

# Age buckets of the rows that would go, in days past the cutoff (upper bounds;
# the last bucket is open). Counts only.
_AGE_BUCKETS_DAYS = (7, 30, 90, 365)


def dry_access_log_retention(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    retention_days: int | None = None,
    **_opts,
) -> dict:
    """What one ``dashboard_access_log_retention`` run would delete now: the
    rows matched by the production predicate (``oncofiles.access_log_sql``)
    with the production clamp, plus how far past the cutoff the oldest is.
    ``retention_days`` stands for ``DASHBOARD_ACCESS_LOG_RETENTION_DAYS``
    (default 90, below 30 clamped to 30, like the job)."""
    from oncofiles import access_log_sql as q
    from oncofiles.dashboard_audit import PURGE_CHUNK_ROWS

    requested = q.DEFAULT_RETENTION_DAYS if retention_days is None else int(retention_days)
    days = q.effective_days(requested)
    cutoff = q.cutoff(now, days)
    where = q.EXPIRED_WHERE
    would_delete = _count(
        conn, f"SELECT COUNT(*) FROM dashboard_access_log WHERE {where}", (cutoff,)
    )
    oldest = conn.execute(
        f"SELECT MIN(created_at) FROM dashboard_access_log WHERE {where}", (cutoff,)
    ).fetchone()[0]
    return {
        "retention_days": days,
        "retention_clamped": days != requested,
        "rows_total": _count(conn, "SELECT COUNT(*) FROM dashboard_access_log"),
        "would_delete": would_delete,
        "delete_chunks": -(-would_delete // PURGE_CHUNK_ROWS),
        "oldest_expired_bucket": _age_bucket(oldest, cutoff, q.CUTOFF_FORMAT),
    }


def _age_bucket(oldest: str | None, cutoff: str, fmt: str) -> str | None:
    """How far before the cutoff the oldest expired row is, as a bucket code."""
    if not oldest:
        return None
    try:
        old_dt = datetime.strptime(oldest, fmt)
    except ValueError:
        return "unparsed"
    past = (datetime.strptime(cutoff, fmt) - old_dt).days
    upper = next((b for b in _AGE_BUCKETS_DAYS if past < b), None)
    if upper is None:
        return f"over_{_AGE_BUCKETS_DAYS[-1]}_days_past_cutoff"
    return f"under_{upper}_days_past_cutoff"


# ── oauth_token_cleanup (#999 job 4) ────────────────────────────────────


def dry_oauth_token_cleanup(conn: sqlite3.Connection, *, now: datetime, **_opts) -> dict:
    """What one ``oauth_token_cleanup`` run would delete now: the
    ``mcp_oauth_tokens`` rows matched by the production predicate
    (``oncofiles.oauth_cleanup_sql``), by token type, plus the rows kept
    (no expiry, expired within the grace, still valid). Counts only: never a
    token digest, client id or account."""
    from oncofiles import oauth_cleanup_sql as q

    cut = q.cutoff(now)
    where = q.EXPIRED_WHERE
    epoch = int(now.astimezone(UTC).timestamp())
    by_type = {
        str(r["token_type"]): int(r["n"])
        for r in conn.execute(
            f"SELECT token_type, COUNT(*) AS n FROM mcp_oauth_tokens WHERE {where} "
            "GROUP BY token_type",
            (cut,),
        )
    }
    return {
        "grace_days": q.EXPIRED_TOKEN_GRACE_S // 86400,
        "rows_total": _count(conn, "SELECT COUNT(*) FROM mcp_oauth_tokens"),
        "would_delete": _count(
            conn, f"SELECT COUNT(*) FROM mcp_oauth_tokens WHERE {where}", (cut,)
        ),
        "would_delete_by_type": dict(sorted(by_type.items())),
        "kept_no_expiry": _count(
            conn, "SELECT COUNT(*) FROM mcp_oauth_tokens WHERE expires_at IS NULL"
        ),
        "kept_expired_within_grace": _count(
            conn,
            "SELECT COUNT(*) FROM mcp_oauth_tokens WHERE expires_at IS NOT NULL "
            "AND expires_at >= ? AND expires_at < ?",
            (cut, epoch),
        ),
        "kept_valid": _count(
            conn,
            "SELECT COUNT(*) FROM mcp_oauth_tokens WHERE expires_at IS NOT NULL "
            "AND expires_at >= ?",
            (epoch,),
        ),
    }


# ── chat_history_retention (#1022) ──────────────────────────────────────


def dry_chat_history_retention(conn: sqlite3.Connection, *, now: datetime, **_opts) -> dict:
    """What one ``chat_history_retention`` run with ``CHAT_RETENTION_MODE=on``
    would do now: rows moved to the trash (90 days) and rows whose content is
    purged (30 days in the trash), with the production predicates
    (``oncofiles.chat_retention_sql``). Counts only: never a patient id,
    conversation id or text (the text is encrypted anyway)."""
    from oncofiles import chat_retention_sql as q

    def n(where: str = "1", params: tuple = ()) -> int:
        return _count(conn, f"SELECT COUNT(*) FROM chat_messages WHERE {where}", params)

    would_trash = n(q.TRASH_WHERE, (q.trash_cutoff(now),))
    would_purge = n(q.PURGE_WHERE, (q.purge_cutoff(now),))
    return {
        "trash_after_days": q.TRASH_AFTER_DAYS,
        "purge_after_trash_days": q.PURGE_AFTER_TRASH_DAYS,
        "rows_total": n(),
        "rows_live": n("deleted_at IS NULL AND purged_at IS NULL"),
        "rows_in_trash": n("deleted_at IS NOT NULL AND purged_at IS NULL"),
        "rows_purged": n("purged_at IS NOT NULL"),
        "patients_with_rows": _count(conn, "SELECT COUNT(DISTINCT patient_id) FROM chat_messages"),
        "would_trash": would_trash,
        "would_purge": would_purge,
        "trash_chunks": -(-would_trash // q.CHUNK_ROWS),
        "purge_chunks": -(-would_purge // q.CHUNK_ROWS),
    }


# job name -> counting function (conn, now=, data_dir=, **options) -> dict
JOBS: dict[str, Callable[..., dict]] = {
    "chat_history_retention": dry_chat_history_retention,
    "holding_move": dry_holding_move,
    "inbound_maintenance": dry_inbound_maintenance,
    "dashboard_access_log_retention": dry_access_log_retention,
    "oauth_token_cleanup": dry_oauth_token_cleanup,
}


class UnsafeCopyError(RuntimeError):
    """The copy has a WAL file next to it: reading it would need a -shm file."""


def open_snapshot(path: str | Path) -> sqlite3.Connection:
    """Open a DB copy strictly read-only, creating no file at all.

    ``mode=ro`` alone still lets SQLite create ``-shm`` / ``-wal`` files next
    to a WAL-mode database. A snapshot made with ``sqlite3 <db> ".backup
    <copy>"`` has all its pages in the main file, so it is opened
    ``immutable=1`` (no locking, no side files). A copy with a ``-wal`` file
    next to it is refused: make the copy with ``.backup`` instead.
    """
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"no database file at {p}")
    wal = p.with_name(p.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise UnsafeCopyError(
            f"{wal.name} exists: make the copy with sqlite3 <db> '.backup <copy>' first"
        )
    conn = sqlite3.connect(f"file:{quote(str(p))}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def run(
    job: str, db: str | Path, *, data_dir: str | Path | None = None, now=None, **options
) -> dict:
    conn = open_snapshot(db)
    try:
        body = JOBS[job](
            conn,
            now=now if isinstance(now, datetime) else _parse_now(now),
            data_dir=Path(data_dir).resolve() if data_dir else None,
            **options,
        )
    finally:
        conn.close()
    return {"tool": "oncofiles.job_dryrun", "tool_version": TOOL_VERSION, "job": job, **body}


def main(argv: list[str] | None = None) -> int:
    from oncofiles.index_diff import block_network

    block_network()  # before anything below imports a job module
    ap = argparse.ArgumentParser(
        prog="python -m oncofiles.job_dryrun", description=__doc__.split("\n")[0]
    )
    ap.add_argument("job", choices=sorted(JOBS), help="the job to count")
    ap.add_argument("--db", required=True, help="path to a COPY of the oncofiles SQLite DB")
    ap.add_argument(
        "--data-dir",
        help="a copy of DATA_DIR (holding store under <dir>/holding, quarantine under "
        "<dir>/quarantine); only checked, never created",
    )
    ap.add_argument("--now", help="ISO time to evaluate at (default: now, UTC)")
    ap.add_argument(
        "--purge-approved",
        action="store_true",
        help="inbound_maintenance: count approved quarantine copies as the purge would with "
        "QUARANTINE_PURGE_APPROVED=true (default off, like the env)",
    )
    ap.add_argument(
        "--purge-approved-after-days",
        type=int,
        help="inbound_maintenance: QUARANTINE_PURGE_APPROVED_AFTER_DAYS (default 7)",
    )
    ap.add_argument(
        "--retention-days",
        type=int,
        help="dashboard_access_log_retention: DASHBOARD_ACCESS_LOG_RETENTION_DAYS "
        "(default 90, below 30 clamped to 30, like the job)",
    )
    args = ap.parse_args(argv)
    options = {}
    if args.job == "inbound_maintenance":
        options = {
            "purge_approved": args.purge_approved,
            "purge_approved_after_days": args.purge_approved_after_days,
        }
    elif args.job == "dashboard_access_log_retention":
        options = {"retention_days": args.retention_days}
    report = run(args.job, args.db, data_dir=args.data_dir, now=args.now, **options)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
