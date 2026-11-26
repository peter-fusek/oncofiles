"""Pre-store safety copy of the database (#573, infra review of #701).

Before any REAL re-read, re-OCR, clinical-extraction (#726), LLM lab write (#714)
or sidecar-restore (#806) job writes, the server takes a consistent copy of the
local DB file with SQLite's online backup API and checks it with
``PRAGMA quick_check``. No copy, no job: the caller refuses (409 on the API)
and writes nothing. Retention is in TOTAL
across job kinds.

* Location: ``<DATABASE_PATH parent>/pre-store/oncofiles-<UTC ts>-<job>.db``
  (derived from ``config.DATABASE_PATH`` at call time, never hardcoded).
* Mode 0600, directory 0700.
* Retention: the newest ``KEEP`` copies stay; older ones are removed one by
  one, and only files whose name fully matches ``NAME_RE`` — never a glob,
  never anything else in the directory. Erase copies (#856) are also removed
  once older than ``ERASE_MAX_AGE_DAYS`` (30), on every copy and by the daily
  ``pre_store_erase_retention`` job (maintainer decision 2026-11-26, RUNBOOK).

Blocking (file I/O): call through ``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

KEEP = 5
# clinical: #726; restore: #806 sidecar restore; lab: #714 LLM lab facts;
# erase: #856 erasure of a patient on request (the copy is made before any row goes)
JOBS = ("reread", "reocr", "clinical", "restore", "lab", "erase")
NAME_RE = re.compile(r"oncofiles-\d{8}T\d{12}Z-(?:reread|reocr|clinical|restore|lab|erase)\.db")
# #856: an erase copy holds the erased patient's rows; it is removed once older
# than this (maintainer decision 2026-11-26), so /privacy's "within 30 days" holds.
ERASE_SUFFIX = "-erase.db"
ERASE_MAX_AGE_DAYS = 30


class PreStoreError(RuntimeError):
    """The safety copy could not be made or did not verify."""


def pre_store_dir() -> Path:
    from oncofiles import config

    return Path(config.DATABASE_PATH).parent / "pre-store"


def _source_path(db) -> Path:
    """The local DB file behind ``db``. Anything else (in-memory, Turso) has
    no file to copy, so a real store is refused (fail closed)."""
    raw = getattr(db, "path", "") or ""
    if getattr(db, "_use_turso", False) or not raw or raw == ":memory:":
        raise PreStoreError("no local database file to copy")
    path = Path(raw)
    if not path.is_file():
        raise PreStoreError("database file not found")
    return path


def _quick_check(path: Path) -> list:
    """``PRAGMA quick_check`` on the finished copy, through a fresh connection."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA quick_check").fetchall()
    finally:
        conn.close()


def _copy_time(name: str) -> datetime | None:
    """UTC time from a copy's own name (``oncofiles-<YYYYmmddTHHMMSS…>Z-…``)."""
    try:
        return datetime.strptime(name[10:25], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _expired_erase(directory: Path, name: str, now: datetime) -> bool:
    """An erase copy older than ``ERASE_MAX_AGE_DAYS`` (time from its name, else mtime)."""
    if not name.endswith(ERASE_SUFFIX) or not (directory / name).is_file():
        return False
    made = _copy_time(name)
    if made is None:
        try:
            made = datetime.fromtimestamp((directory / name).stat().st_mtime, UTC)
        except OSError:
            return False
    return now - made > timedelta(days=ERASE_MAX_AGE_DAYS)


def _prune(directory: Path, keep: int, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    names = sorted(n for n in os.listdir(directory) if NAME_RE.fullmatch(n))
    over_count = set(names[: max(0, len(names) - keep)])
    removed = []
    for name in names:
        if name not in over_count and not _expired_erase(directory, name, now):
            continue
        try:
            os.remove(directory / name)  # exact filename, pattern-matched above
            removed.append(name)
        except OSError:
            logger.warning("pre-store: could not remove old copy %s", name)
    return removed


def prune_expired_erase_copies(*, now: datetime | None = None) -> int:
    """Daily lifecycle (maintainer decision 2026-11-26, RUNBOOK): remove erase
    copies older than ``ERASE_MAX_AGE_DAYS`` (and anything over ``KEEP``), so a
    stale copy never outlives the 30-day erasure promise. Returns the count."""
    directory = pre_store_dir()
    if not directory.is_dir():
        return 0
    return len(_prune(directory, KEEP, now))


def make_copy(db, job: str, *, now: datetime | None = None, keep: int = KEEP) -> str:
    """Copy the DB, verify it, prune old copies. Returns the copy's filename.

    Raises ``PreStoreError`` on any failure; a partial copy is removed.
    """
    if job not in JOBS:
        raise PreStoreError(f"unknown job {job!r}")
    src_path = _source_path(db)
    directory = pre_store_dir()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise PreStoreError(f"cannot create the pre-store directory: {type(exc).__name__}") from exc
    now = now or datetime.now(UTC)
    name = f"oncofiles-{now:%Y%m%dT%H%M%S%f}Z-{job}.db"
    dst_path = directory / name
    created = False
    try:
        fd = os.open(dst_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        created = True
        # Read-only source connection: the app's own connection keeps running
        # (WAL lets readers proceed while the backup reads a consistent snapshot).
        src = sqlite3.connect(f"{src_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(dst_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        if _quick_check(dst_path) != [("ok",)]:
            raise PreStoreError("quick_check failed on the copy")
        os.chmod(dst_path, 0o600)
    except Exception as exc:
        if created:
            for suffix in ("", "-journal"):  # only the files this call created
                with contextlib.suppress(OSError):
                    os.remove(f"{dst_path}{suffix}")
        if isinstance(exc, PreStoreError):
            raise
        raise PreStoreError(f"backup failed: {type(exc).__name__}") from exc
    removed = _prune(directory, keep, now)
    logger.info("pre-store copy %s made (%d old copies removed)", name, len(removed))
    return name
