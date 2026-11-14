"""Pre-store safety copy of the database (#573, infra review of #701).

Before any REAL re-read, re-OCR, clinical-extraction (#726) or sidecar-restore
(#806) job writes, the server takes a consistent copy of the local DB file with SQLite's online
backup API and checks it with ``PRAGMA quick_check``. No copy, no job: the
caller refuses (409 on the API) and writes nothing. Retention is in TOTAL
across job kinds.

* Location: ``<DATABASE_PATH parent>/pre-store/oncofiles-<UTC ts>-<job>.db``
  (derived from ``config.DATABASE_PATH`` at call time, never hardcoded).
* Mode 0600, directory 0700.
* Retention: the newest ``KEEP`` copies stay; older ones are removed one by
  one, and only files whose name fully matches ``NAME_RE`` — never a glob,
  never anything else in the directory.

Blocking (file I/O): call through ``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

KEEP = 5
JOBS = ("reread", "reocr", "clinical", "restore")  # clinical: #726; restore: #806 sidecar restore
NAME_RE = re.compile(r"oncofiles-\d{8}T\d{12}Z-(?:reread|reocr|clinical|restore)\.db")


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


def _prune(directory: Path, keep: int) -> list[str]:
    names = sorted(n for n in os.listdir(directory) if NAME_RE.fullmatch(n))
    removed = []
    for name in names[: max(0, len(names) - keep)]:
        try:
            os.remove(directory / name)  # exact filename, pattern-matched above
            removed.append(name)
        except OSError:
            logger.warning("pre-store: could not remove old copy %s", name)
    return removed


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
    removed = _prune(directory, keep)
    logger.info("pre-store copy %s made (%d old copies removed)", name, len(removed))
    return name
