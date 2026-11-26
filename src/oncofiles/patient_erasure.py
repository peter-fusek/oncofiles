"""Erasure of ONE patient on request (#856, /privacy, maintainer decision 2026-11-19).

The only code that hard-deletes a patient's rows. User-invoked, never scheduled
(``tests/test_no_automatic_deletion.py``: its delete call site is allowlisted
with the reason below and no scheduler job may reach it). Reached through the
operator MCP tool ``erase_patient`` (``tools/patient.py``, admin only, served
on ``/ops/mcp``).

Dry run (the default): counts per registry table, quarantine items and held
files; writes nothing. A real run, in this order, refusing before anything is
removed:

1. ``confirm_slug`` must equal the patient's slug and ``reason`` must be given
   (≤ 200 chars; never stored nor logged: it may name the requester);
2. no curated work of the patient may run (``patient_work_reason``), and the
   patient lock, the inbound processing lock and the inbound patient lock are
   held for the whole erasure;
3. a verified pre-store copy of the database (``pre_store.make_copy(db,
   "erase")``); no copy, no erasure;
4. the file list and the Google grants are read; then every row of every
   ``data_rights.REGISTRY`` table is deleted in ONE transaction run as one job
   on the database thread (no other coroutine's statement can interleave),
   FK checks deferred to the commit, ``secure_delete`` on; ``patients`` last;
   ``patient_consents`` with it (decision 2026-11-19). A refusal
   (``db_busy``) or rollback ends here with nothing touched (a deferred FK
   failure also returns ``fk_violations`` = {table: rows});
   after the commit, best effort (never failing the erasure): FTS5
   ``optimize`` of ``documents_fts`` / ``conversation_entries_fts`` and
   ``PRAGMA wal_checkpoint(TRUNCATE)`` with ``secure_delete`` on, so no
   erased text lingers in old index segments or the WAL; then the registry is
   re-counted and anything left is returned as ``residual_rows``;
5. only after the commit the encrypted files go: the patient's quarantine
   items (through the quarantine purge helper's own path checks) and a
   trial's held originals; missing files are tolerated, a path that fails its
   check is left and named in ``leftover_files`` for the operator;
6. then the Google grant is revoked, by the Drive disconnect's fail-safe
   rule: only a grant proven unused by any other patient; a failed revoke is
   reported, the stored token is gone anyway;
7. one ``activity_log`` row under ``SYSTEM_NO_PATIENT`` with counts, the
   copy's name and the caller's keyed hash: no slug, no patient id, no name.

Never touches Google Drive files: they are the person's own.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import stat
from contextlib import AsyncExitStack

from oncofiles import data_rights

logger = logging.getLogger(__name__)

ERASURE_REASON = "user-invoked erasure on request, /privacy §5, maintainer decision 2026-11-19"
REASON_MAX = 200


class ErasureRefusedError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ErasureRolledBackError(RuntimeError):
    """The transaction was rolled back. ``fk_violations`` = {table: rows} that
    ``PRAGMA foreign_key_check`` reported when a deferred FK check failed the
    commit (table names and counts only, never row ids or values)."""

    def __init__(self, cause: BaseException, fk_violations: dict[str, int]) -> None:
        super().__init__(type(cause).__name__)
        self.cause_name = type(cause).__name__
        self.fk_violations = fk_violations


def _fk_violations(conn) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for row in conn.execute("PRAGMA foreign_key_check").fetchall():
            out[row[0]] = out.get(row[0], 0) + 1
    except Exception:  # noqa: BLE001 - a diagnostic only
        logger.warning("erasure: foreign_key_check failed", exc_info=True)
    return out


FTS_TABLES = ("documents_fts", "conversation_entries_fts")


def _compact_sync(conn) -> list[str]:
    """Best effort after the erase commit: merge the FTS5 segments (the deleted
    rows' tokens otherwise linger in old segments until a merge) and truncate
    the WAL (which still holds page images with the erased rows), with
    ``secure_delete`` on so freed pages are zeroed. Never raises; returns the
    steps that failed or were skipped (``db_busy``: another coroutine left a
    transaction open, committing would commit its writes)."""
    failed: list[str] = []
    try:
        if conn.in_transaction:
            return ["db_busy"]
        secure = conn.execute("PRAGMA secure_delete").fetchone()[0]
        conn.execute("PRAGMA secure_delete = ON")
        try:
            for fts in FTS_TABLES:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (fts,)
                ).fetchone()
                if not exists:
                    continue
                try:
                    table = data_rights._quote(fts)
                    conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
                    conn.commit()
                except Exception:  # noqa: BLE001 - best effort
                    if conn.in_transaction:
                        conn.rollback()
                    failed.append(fts)
            try:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if row and row[0]:  # busy: not every frame could be checkpointed
                    failed.append("wal_checkpoint_busy")
            except Exception:  # noqa: BLE001 - best effort
                failed.append("wal_checkpoint")
        finally:
            conn.execute(f"PRAGMA secure_delete = {int(secure)}")
    except Exception:  # noqa: BLE001 - never fails the erasure
        failed.append("compact")
    return failed


def _delete_rows_sync(conn, patient_id: str) -> dict[str, int]:
    """Delete the patient's rows of every registry table in ONE transaction.

    Runs on aiosqlite's worker thread as one job (``Connection._execute``), so
    nothing else uses the connection in between. Refuses when another
    coroutine left a transaction open (``db_busy``: committing would commit
    its half-done writes). Rolls back on any error, a deferred FK violation at
    the commit included.
    """
    if conn.in_transaction:
        raise ErasureRefusedError("db_busy")
    secure = conn.execute("PRAGMA secure_delete").fetchone()[0]
    conn.execute("PRAGMA secure_delete = ON")
    counts: dict[str, int] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            for rule in data_rights.REGISTRY:
                table = data_rights._quote(rule.table)
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE {rule.erase_scope}", {"pid": patient_id}
                )
                counts[rule.table] = int(cursor.rowcount or 0)
            conn.commit()
        except BaseException as exc:
            fk: dict[str, int] = {}
            if (
                isinstance(exc, sqlite3.IntegrityError)
                and "FOREIGN KEY" in str(exc).upper()
                and conn.in_transaction
            ):
                fk = _fk_violations(conn)  # before the rollback undoes them
            conn.rollback()
            if isinstance(exc, Exception):
                raise ErasureRolledBackError(exc, fk) from exc
            raise
    finally:
        conn.execute(f"PRAGMA secure_delete = {int(secure)}")
    return counts


def erase_held_file(file_id: str) -> str:
    """Remove ONE held original of the erased trial patient. Blocking.

    Lives here, not in ``holding_store`` (which never deletes: the trial
    lifecycle purge is still a maintainer TODO). Only
    ``<holding dir>/<uuid>.bin`` of a canonical ``holding:<uuid>`` id, and
    only a regular file directly inside the holding directory (no symlink,
    no glob). Returns ``removed``, ``missing`` or ``refused``.
    """
    from oncofiles import holding_store

    try:
        uid = holding_store._uuid_of(file_id)
    except holding_store.HoldingStoreError:
        return "refused"
    root = holding_store.holding_dir().resolve()
    path = root / f"{uid}.bin"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    if not stat.S_ISREG(info.st_mode) or path.parent != root:
        return "refused"
    try:
        os.remove(path)  # one named, checked file
    except FileNotFoundError:
        return "missing"
    return "removed"


async def _files_plan(db, patient_id: str) -> tuple[list[tuple[str, int]], list[str]]:
    async with db.db.execute(
        "SELECT item_uuid, file_count FROM inbound_quarantine WHERE patient_id = ?",
        (patient_id,),
    ) as cursor:
        items = [(r[0], int(r[1] or 0)) for r in await cursor.fetchall()]
    async with db.db.execute(
        "SELECT file_id FROM documents WHERE patient_id = ? AND file_id LIKE 'holding:%'",
        (patient_id,),
    ) as cursor:
        held = [r[0] for r in await cursor.fetchall()]
    return items, held


def _forget_runtime(patient_id: str) -> None:
    """In-memory state of the erased patient (caches, curated registries)."""
    from oncofiles import patient_context
    from oncofiles.tools import curated

    patient_context._contexts.pop(patient_id, None)
    curated.forget_patient(patient_id)
    curated._patient_locks.pop(patient_id, None)
    try:
        from oncofiles import server

        server._forget_patient_runtime(patient_id)
    except Exception:  # noqa: BLE001 - no server module loaded (CLI, tests)
        pass


async def erase_patient(
    db,
    *,
    patient_id: str,
    confirm_slug: str | None,
    dry_run: bool = True,
    reason: str | None = None,
    caller_email: str | None = None,
) -> dict:
    """Erase ONE patient (see the module docstring). Returns codes and counts."""
    from oncofiles import inbound_email, inbound_quarantine, pre_store
    from oncofiles.constants import SYSTEM_NO_PATIENT
    from oncofiles.tools import curated

    patient = await db.get_patient(patient_id)
    if patient is None:
        return {"status": "not_found"}
    counts = await data_rights.count_rows(db, patient_id)
    items, held = await _files_plan(db, patient_id)
    plan = {
        "rows": counts,
        "rows_total": sum(counts.values()),
        "quarantine_items": len(items),
        "quarantine_files": sum(n for _, n in items),
        "held_files": len(held),
        "drive_files": "untouched",
    }
    if dry_run:
        return {"status": "dry_run", **plan}

    def refused(code: str) -> dict:
        return {"status": "refused", "code": code}

    if not confirm_slug or confirm_slug.strip() != (patient.slug or ""):
        return refused("confirm_slug_mismatch")
    reason = (reason or "").strip()
    if not reason:
        return refused("reason_required")
    if len(reason) > REASON_MAX:
        return refused("reason_too_long")
    busy = curated.patient_work_reason(patient_id)
    if busy:
        return refused(busy)

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(curated.patient_lock(patient_id))
        await stack.enter_async_context(inbound_email.PROCESS_LOCK)
        await stack.enter_async_context(inbound_email._state.lock(patient_id))
        try:
            copy_name = await asyncio.to_thread(pre_store.make_copy, db, "erase")
        except pre_store.PreStoreError as exc:
            logger.warning("erasure: pre-store copy failed (%s)", type(exc).__name__)
            return refused("pre_store_failed")
        busy = curated.patient_work_reason(patient_id, lock_held_by_caller=True)
        if busy:  # a job may have started while we waited for the lock
            return refused(busy)
        # Everything the rows point at is read BEFORE the transaction; files and
        # the Google grant are touched only AFTER a successful commit, so a
        # refused or rolled-back erasure leaves the patient exactly as it was.
        grants = await db.oauth_grants_for_disconnect(patient_id=patient_id)
        items, held = await _files_plan(db, patient_id)  # again, under the locks
        try:
            removed = await db.db._execute(_delete_rows_sync, db.db._conn, patient_id)
        except ErasureRefusedError as exc:
            return refused(exc.code)
        except ErasureRolledBackError as exc:
            logger.warning(
                "erasure: rolled back (%s), fk violations in %d table(s)",
                exc.cause_name,
                len(exc.fk_violations),
            )
            out = refused("rolled_back")
            if exc.fk_violations:
                out["fk_violations"] = exc.fk_violations
            return out
        except Exception as exc:  # noqa: BLE001 - rolled back; say why by class only
            logger.warning("erasure: rolled back (%s)", type(exc).__name__)
            return refused("rolled_back")
        compact_failed = await db.db._execute(_compact_sync, db.db._conn)
        if compact_failed:
            logger.warning("erasure: compaction steps not done: %s", ",".join(compact_failed))
        residual = {t: n for t, n in (await data_rights.count_rows(db, patient_id)).items() if n}
        if residual:
            logger.warning("erasure: %d row(s) left after the commit", sum(residual.values()))
        q = await asyncio.to_thread(inbound_quarantine.erase_items_files, items)
        held_codes: dict[str, int] = {}
        held_left: list[str] = []
        for file_id in held:
            code = await asyncio.to_thread(erase_held_file, file_id)
            held_codes[code] = held_codes.get(code, 0) + 1
            if code == "refused":
                held_left.append(file_id)
        google = await data_rights.revoke_grants(grants)
    _forget_runtime(patient_id)
    leftover = {"quarantine_items": q.pop("refused_items", []), "held_files": held_left}
    if leftover["quarantine_items"] or leftover["held_files"]:
        # rows are gone; a path that failed its check needs an operator (ids only)
        logger.warning(
            "erasure: %d quarantine item dir(s) and %d held file(s) refused by the path check",
            len(leftover["quarantine_items"]),
            len(leftover["held_files"]),
        )
    out = {
        "status": "erased",
        "rows_removed": removed,
        "rows_total": sum(removed.values()),
        # re-counted after the commit: {table: rows} still matching, normally {}
        "residual_rows": residual,
        "compaction": {"failed": compact_failed} if compact_failed else "ok",
        "quarantine": q,
        "held_files": held_codes,
        "leftover_files": leftover,
        "google_revoke": google,
        "pre_store_copy": copy_name,
        "drive_files": "untouched",
    }
    logger.info(
        "erasure: patient erased, %d rows, quarantine files %d, held files %d, google=%s",
        out["rows_total"],
        int(q.get("files_removed", 0)),
        int(held_codes.get("removed", 0)),
        google,
    )
    await data_rights.audit(
        db,
        patient_id=SYSTEM_NO_PATIENT,
        tool="erase_patient",
        caller_email=caller_email,
        data={k: v for k, v in out.items()},
    )
    return out
