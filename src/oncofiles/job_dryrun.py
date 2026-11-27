"""Read-only "check before" counts of a background job (#999).

Before a job moves out of APScheduler into its own lifespan loop, the
maintainer wants to know what its first run in production would do. Infra runs
this on a snapshot COPY of the production DB (and, where the job reads files,
a copy of the data directory)::

    uv run python -m oncofiles.job_dryrun holding_move --db /tmp/x/oncofiles.db \\
        [--data-dir /tmp/x/data] [--now 2027-01-02T03:04:05Z]

One subcommand per job; the next #999 PRs add theirs (``inbound_maintenance``,
``access_log_retention``, ``oauth_token_cleanup``, ``memory_check``) to
``JOBS``. Guarantees, like ``oncofiles.index_diff``:

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
* **The production predicates.** The SQL is ``oncofiles.holding_sql``, the
  very statements the ``database._trials`` helpers run for the jobs, and the
  limits are the jobs' own constants, so the counts cannot drift.
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


def dry_holding_move(conn: sqlite3.Connection, *, now: datetime, data_dir: Path | None) -> dict:
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


# job name -> counting function (conn, now=, data_dir=) -> dict
JOBS: dict[str, Callable[..., dict]] = {
    "holding_move": dry_holding_move,
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


def run(job: str, db: str | Path, *, data_dir: str | Path | None = None, now=None) -> dict:
    conn = open_snapshot(db)
    try:
        body = JOBS[job](
            conn,
            now=now if isinstance(now, datetime) else _parse_now(now),
            data_dir=Path(data_dir).resolve() if data_dir else None,
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
        help="a copy of DATA_DIR (holding store under <dir>/holding); only checked, never created",
    )
    ap.add_argument("--now", help="ISO time to evaluate at (default: now, UTC)")
    args = ap.parse_args(argv)
    report = run(args.job, args.db, data_dir=args.data_dir, now=args.now)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
