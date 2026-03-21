"""Database connection, lifecycle, and migration logic."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


# ── Circuit breaker ──────────────────────────────────────────────────────────


class _CircuitBreaker:
    """Prevents tight reconnect loops when Turso driver panics repeatedly.

    States: CLOSED (normal) → OPEN (failing, reject fast) → HALF_OPEN (probe).
    After ``max_failures`` consecutive failures within ``window`` seconds,
    opens the circuit for ``cooldown`` seconds.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        max_failures: int = 3,
        window: float = 60.0,
        cooldown: float = 30.0,
    ) -> None:
        self.max_failures = max_failures
        self.window = window
        self.cooldown = cooldown
        self._state = self.CLOSED
        self._failure_times: list[float] = []
        self._opened_at: float = 0.0

    @property
    def state(self) -> str:
        if self._state == self.OPEN and time.monotonic() - self._opened_at >= self.cooldown:
            self._state = self.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failure_times.clear()
        self._state = self.CLOSED

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failure_times = [t for t in self._failure_times if now - t < self.window]
        self._failure_times.append(now)
        if len(self._failure_times) >= self.max_failures:
            self._state = self.OPEN
            self._opened_at = now
            logger.error(
                "Circuit breaker OPEN: %d failures in %.0fs, cooling down %.0fs",
                len(self._failure_times),
                self.window,
                self.cooldown,
            )

    def check(self) -> None:
        """Raise if circuit is open."""
        state = self.state
        if state == self.OPEN:
            wait = self.cooldown - (time.monotonic() - self._opened_at)
            raise RuntimeError(f"Circuit breaker open — DB unavailable, retry in {wait:.0f}s")


MIGRATIONS_DIR = Path(__file__).parent.parent.parent.parent / "migrations"


# ── Turso async wrappers ──────────────────────────────────────────────────────


class _TursoCursor:
    """Async-compatible wrapper around sync libsql cursor.

    Converts tuple rows to dicts using cursor.description so that
    row["column_name"] access works identically to aiosqlite.Row.
    """

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def _to_dict(self, row: tuple | None) -> dict | None:
        if row is None:
            return None
        cols = [d[0] for d in self._cursor.description]
        return dict(zip(cols, row, strict=True))

    async def fetchone(self) -> dict | None:
        return self._to_dict(self._cursor.fetchone())

    async def fetchall(self) -> list[dict]:
        return [self._to_dict(r) for r in self._cursor.fetchall()]

    async def __aenter__(self) -> _TursoCursor:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class _TursoExecProxy:
    """Awaitable + async-context-manager proxy matching aiosqlite.execute()."""

    def __init__(self, turso_conn: _TursoConnection, sql: str, params: tuple) -> None:
        self._turso_conn = turso_conn
        self._sql = sql
        self._params = params

    async def _run(self) -> _TursoCursor:
        cursor = await self._turso_conn._execute_raw(self._sql, self._params)
        return _TursoCursor(cursor)

    def __await__(self):  # noqa: ANN204
        return self._run().__await__()

    async def __aenter__(self) -> _TursoCursor:
        return await self._run()

    async def __aexit__(self, *args: Any) -> None:
        pass


def _is_stale_stream_error(exc: Exception) -> bool:
    """Check if an exception is a Turso/Hrana stale stream error."""
    msg = str(exc).lower()
    return "stream not found" in msg or "stream expired" in msg


class _TursoConnection:
    """Async wrapper around sync libsql connection matching aiosqlite interface.

    Auto-reconnects on stale Hrana stream errors (e.g. after Railway cold start).
    """

    def __init__(self, url: str, auth_token: str) -> None:
        self._url = url
        self._auth_token = auth_token
        self._conn: Any = None
        self._breaker = _CircuitBreaker()

    _CONNECT_TIMEOUT = 15.0  # seconds — prevents indefinite hangs on stale Turso

    async def connect(self) -> None:
        import libsql

        self._conn = await asyncio.wait_for(
            asyncio.to_thread(libsql.connect, self._url, auth_token=self._auth_token),
            timeout=self._CONNECT_TIMEOUT,
        )

    async def reconnect(self) -> None:
        """Close stale connection and create a fresh one."""
        logger.warning("Reconnecting to Turso (stale stream detected)")
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        try:
            await self.connect()
        except TimeoutError:
            logger.error("Turso reconnect timed out after %.0fs", self._CONNECT_TIMEOUT)
            self._breaker.record_failure()
            raise RuntimeError("Turso reconnect timed out") from None

    def execute(self, sql: str, params: tuple | list = ()) -> _TursoExecProxy:
        return _TursoExecProxy(self, sql, tuple(params))

    _QUERY_TIMEOUT = 30.0  # seconds

    async def _execute_raw(self, sql: str, params: tuple) -> Any:
        """Execute SQL, auto-reconnecting on stale stream errors. 30s timeout.

        Uses a circuit breaker to prevent tight reconnect loops when the
        Turso driver panics repeatedly (e.g. overnight stale connections).
        """
        self._breaker.check()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._conn.execute, sql, params),
                timeout=self._QUERY_TIMEOUT,
            )
            self._breaker.record_success()
            return result
        except TimeoutError:
            logger.error("Query timed out after %.0fs: %s", self._QUERY_TIMEOUT, sql[:200])
            self._breaker.record_failure()
            raise
        except BaseException as exc:
            if _is_stale_stream_error(exc) or "PanicException" in type(exc).__name__:
                self._breaker.record_failure()
                self._breaker.check()  # bail early if breaker just opened
                logger.warning(
                    "DB driver error (%s), reconnecting: %s", type(exc).__name__, sql[:100]
                )
                await self.reconnect()
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(self._conn.execute, sql, params),
                        timeout=self._QUERY_TIMEOUT,
                    )
                    self._breaker.record_success()
                    return result
                except BaseException as retry_exc:
                    self._breaker.record_failure()
                    if "PanicException" in type(retry_exc).__name__:
                        raise RuntimeError(
                            f"DB driver panic after reconnect: {retry_exc}"
                        ) from retry_exc
                    raise
            raise

    async def executescript(self, sql: str) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._conn.executescript, sql),
                timeout=self._QUERY_TIMEOUT,
            )
        except Exception as exc:
            if _is_stale_stream_error(exc):
                await self.reconnect()
                await asyncio.wait_for(
                    asyncio.to_thread(self._conn.executescript, sql),
                    timeout=self._QUERY_TIMEOUT,
                )
            else:
                raise

    async def commit(self) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._conn.commit),
                timeout=self._QUERY_TIMEOUT,
            )
        except Exception as exc:
            if _is_stale_stream_error(exc):
                await self.reconnect()
                await asyncio.wait_for(
                    asyncio.to_thread(self._conn.commit),
                    timeout=self._QUERY_TIMEOUT,
                )
            else:
                raise

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# ── DatabaseBase ──────────────────────────────────────────────────────────────


class DatabaseBase:
    """Connection, lifecycle, and migration management."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        turso_url: str = "",
        turso_token: str = "",
    ) -> None:
        self.path = str(path)
        self._turso_url = turso_url
        self._turso_token = turso_token
        self._use_turso = bool(turso_url)
        self._db: aiosqlite.Connection | _TursoConnection | None = None

    async def connect(self) -> None:
        if self._use_turso:
            conn = _TursoConnection(self._turso_url, self._turso_token)
            await conn.connect()
            self._db = conn
        else:
            self._db = await aiosqlite.connect(self.path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def reconnect_if_stale(self) -> bool:
        """Reconnect Turso connection if stale. Returns True if reconnected."""
        if self._use_turso and isinstance(self._db, _TursoConnection):
            try:
                await self._db._execute_raw("SELECT 1", ())
            except Exception as exc:
                if _is_stale_stream_error(exc):
                    # reconnect() was already called by _execute_raw, verify it worked
                    await self._db._execute_raw("SELECT 1", ())
                    return True
                raise
        return False

    @property
    def db(self) -> aiosqlite.Connection | _TursoConnection:
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
                        except Exception:
                            pass
            await self.db.execute(
                "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
                (version,),
            )
            await self.db.commit()
