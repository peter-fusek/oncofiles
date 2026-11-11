"""Database connection, lifecycle, and migration logic."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


MIGRATIONS_DIR = Path(__file__).parent.parent.parent.parent / "migrations"


# ── DatabaseBase ──────────────────────────────────────────────────────────────


class DatabaseBase:
    """Connection, lifecycle, and migration management."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def ping(self, timeout: float = 2.0) -> None:
        """Bounded ``SELECT 1`` against the local file. Raises on failure or timeout.

        Used by ``/readiness``. Never call from ``/health`` (liveness, no DB).
        """

        async def _probe() -> None:
            async with self.db.execute("SELECT 1") as cursor:
                await cursor.fetchone()

        await asyncio.wait_for(_probe(), timeout=timeout)

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._db

    async def migrate(self) -> None:
        """Run numbered SQL migration files from migrations/ directory.

        Pattern: migrations/001_description.sql, 002_description.sql, etc.
        Uses a schema_migrations table to track applied migrations and skip
        already-applied ones. Safe to call on already-migrated databases.
        """
        # Ensure tracking table exists (must run before checking applied)
        await self.db.executescript(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version TEXT PRIMARY KEY,"
            "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ");"
        )

        # Get already-applied migrations
        async with self.db.execute("SELECT version FROM schema_migrations") as cursor:
            rows = await cursor.fetchall()
        applied = {row["version"] if isinstance(row, dict) else row[0] for row in rows}

        # Run pending migrations in order
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = sql_file.stem  # e.g. "001_initial_schema"
            if version in applied:
                continue
            sql = sql_file.read_text()
            if sql.strip():
                try:
                    await self.db.executescript(sql)
                except Exception:
                    # On partially-tracked DBs, ALTER TABLE may fail with
                    # "duplicate column". Run each statement best-effort.
                    for stmt in sql.split(";"):
                        stmt = stmt.strip()
                        if not stmt or stmt.startswith("--"):
                            continue
                        try:
                            await self.db.execute(stmt)
                            await self.db.commit()
                        except Exception as stmt_err:
                            logger.warning(
                                "Migration %s: statement failed (best-effort): %s — %s",
                                version,
                                stmt[:80],
                                stmt_err,
                            )
            await self.db.execute(
                "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
                (version,),
            )
            await self.db.commit()
