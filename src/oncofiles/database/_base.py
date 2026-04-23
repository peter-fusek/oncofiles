"""Database connection, lifecycle, and migration logic."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
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

    Exposes ``stats()`` for ``/readiness`` so operators can see live breaker
    state + last-trip metadata without SSH'ing into the process (#469 Phase 3).
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    _CAUSE_MAX_LEN = 200

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
        # Telemetry (no functional role — exposed via stats() on /readiness)
        self._last_trip_at: str | None = None
        self._last_trip_cause: str | None = None
        self._trip_count_total = 0

    @property
    def state(self) -> str:
        if self._state == self.OPEN and time.monotonic() - self._opened_at >= self.cooldown:
            self._state = self.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failure_times.clear()
        self._state = self.CLOSED

    def record_failure(self, cause: str | None = None) -> None:
        now = time.monotonic()
        self._failure_times = [t for t in self._failure_times if now - t < self.window]
        self._failure_times.append(now)
        if len(self._failure_times) >= self.max_failures and self._state != self.OPEN:
            self._state = self.OPEN
            self._opened_at = now
            self._trip_count_total += 1
            self._last_trip_at = datetime.now(UTC).isoformat()
            if cause:
                self._last_trip_cause = cause[: self._CAUSE_MAX_LEN]
            logger.error(
                "Circuit breaker OPEN (trip #%d): %d failures in %.0fs, "
                "cooling down %.0fs — cause: %s",
                self._trip_count_total,
                len(self._failure_times),
                self.window,
                self.cooldown,
                self._last_trip_cause or "unknown",
            )

    def check(self) -> None:
        """Raise if circuit is open."""
        state = self.state
        if state == self.OPEN:
            wait = self.cooldown - (time.monotonic() - self._opened_at)
            raise RuntimeError(f"Circuit breaker open — DB unavailable, retry in {wait:.0f}s")

    def stats(self) -> dict[str, Any]:
        """Live breaker state + telemetry for /readiness (#469 Phase 3).

        Returned dict is JSON-serializable. ``state`` reads through the property
        so a HALF_OPEN transition after cooldown is reflected correctly.
        """
        state = self.state  # triggers OPEN→HALF_OPEN transition if due
        now = time.monotonic()
        failures_in_window = sum(1 for t in self._failure_times if now - t < self.window)
        cooldown_remaining_s = 0.0
        if state == self.OPEN:
            cooldown_remaining_s = max(0.0, self.cooldown - (now - self._opened_at))
        return {
            "state": state,
            "failures_in_window": failures_in_window,
            "window_seconds": self.window,
            "max_failures": self.max_failures,
            "cooldown_seconds": self.cooldown,
            "cooldown_remaining_s": round(cooldown_remaining_s, 1),
            "last_trip_at": self._last_trip_at,
            "last_trip_cause": self._last_trip_cause,
            "trip_count_total": self._trip_count_total,
        }


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


def _is_transient_db_error(exc: Exception) -> bool:
    """Check if an exception is a transient DB error worth retrying (#378).

    Covers stale streams, Hrana conflicts, and embedded replica corruption
    (common after Railway container restart or Volume hiccup).
    """
    msg = str(exc)
    return _is_stale_stream_error(exc) or any(e in msg for e in _HRANA_TRANSIENT_ERRORS)


class _TursoConnection:
    """Async wrapper around sync libsql connection matching aiosqlite interface.

    Auto-reconnects on stale Hrana stream errors (e.g. after Railway cold start).

    When ``replica_path`` is set, uses an embedded replica: a local SQLite file
    that syncs from the Turso primary.  Reads are served locally (~0.3 ms vs
    ~100 ms remote); writes go through to the primary.
    """

    def __init__(self, url: str, auth_token: str, *, replica_path: str = "") -> None:
        self._url = url
        self._auth_token = auth_token
        self._replica_path = replica_path
        self._conn: Any = None
        self._breaker = _CircuitBreaker()

    _CONNECT_TIMEOUT = 15.0  # seconds — prevents indefinite hangs on stale Turso

    @property
    def is_replica(self) -> bool:
        return bool(self._replica_path)

    async def connect(self) -> None:
        import libsql

        def _sanity_check(conn) -> None:
            """Execute SELECT 1 to verify the DB file isn't corrupt.

            libsql.connect() + sync() don't actually read the main DB file —
            they just initialize. The 'file is not a database' error only
            surfaces on the first fetchone(). This probe forces that detection
            at connect time so we can trigger the wipe-and-resync path.
            """
            cur = conn.execute("SELECT 1")
            cur.fetchone()

        def _wipe_replica_files() -> None:
            import os

            for suffix in ("", "-wal", "-shm", "-journal"):
                path = f"{self._replica_path}{suffix}"
                try:
                    os.remove(path)
                    logger.info("Wiped corrupt replica file: %s", path)
                except FileNotFoundError:
                    pass
                except Exception as rm_exc:
                    logger.warning("Failed to remove %s: %s", path, rm_exc)

        async def _connect_and_sync():
            sync_url = self._url.replace("libsql://", "https://")
            self._conn = await asyncio.wait_for(
                asyncio.to_thread(
                    libsql.connect,
                    self._replica_path,
                    sync_url=sync_url,
                    auth_token=self._auth_token,
                ),
                timeout=self._CONNECT_TIMEOUT,
            )
            await asyncio.wait_for(
                asyncio.to_thread(self._conn.sync),
                timeout=self._CONNECT_TIMEOUT,
            )
            # Probe: validate the DB file is actually readable. Corruption
            # only surfaces on first query, not connect/sync (#476).
            await asyncio.wait_for(
                asyncio.to_thread(_sanity_check, self._conn),
                timeout=self._CONNECT_TIMEOUT,
            )

        if self._replica_path:
            try:
                await _connect_and_sync()
                logger.info("Embedded replica connected: %s", self._replica_path)
            except Exception as exc:
                msg = str(exc)
                # Auto-recover from corrupt replica file (#476 incident —
                # migration 062's 19,707 UPDATE batch corrupted Turso WAL,
                # leaving the replica in "file is not a database" state).
                # Wipe the local file and retry — Turso will sync a fresh
                # copy from cloud primary.
                if "file is not a database" in msg or "disk image is malformed" in msg:
                    logger.error(
                        "Embedded replica corrupt (%s: %s) — wiping %s and resyncing from cloud",
                        type(exc).__name__,
                        msg,
                        self._replica_path,
                    )
                    _wipe_replica_files()
                    # Retry connect with a fresh file — libsql will create it
                    # from the cloud primary on first sync.
                    await _connect_and_sync()
                    logger.info(
                        "Embedded replica RESYNCED after corruption: %s", self._replica_path
                    )
                else:
                    raise
        else:
            self._conn = await asyncio.wait_for(
                asyncio.to_thread(libsql.connect, self._url, auth_token=self._auth_token),
                timeout=self._CONNECT_TIMEOUT,
            )

    async def sync(self) -> None:
        """Sync embedded replica from Turso primary. No-op for direct connections."""
        if self._replica_path and self._conn:
            await asyncio.to_thread(self._conn.sync)

    async def reconnect(self) -> None:
        """Close stale connection and create a fresh one."""
        logger.warning("Reconnecting to Turso (stale stream detected)")
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            logger.warning("Turso close() failed during reconnect", exc_info=True)
        self._conn = None
        try:
            await self.connect()
        except TimeoutError:
            logger.error("Turso reconnect timed out after %.0fs", self._CONNECT_TIMEOUT)
            self._breaker.record_failure(f"reconnect TimeoutError after {self._CONNECT_TIMEOUT}s")
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
        # If a prior reconnect attempt failed (self._conn left None), the next
        # query would crash with 'NoneType' object has no attribute 'execute'
        # and the except branch below would not catch it as transient. Recover
        # proactively here so the user-facing call can still succeed. (#405)
        if self._conn is None:
            logger.warning("DB connection is None on entry; attempting reconnect")
            await self.reconnect()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._conn.execute, sql, params),
                timeout=self._QUERY_TIMEOUT,
            )
            self._breaker.record_success()
            return result
        except TimeoutError:
            logger.error("Query timed out after %.0fs: %s", self._QUERY_TIMEOUT, sql[:200])
            self._breaker.record_failure(f"query TimeoutError after {self._QUERY_TIMEOUT}s")
            raise
        except BaseException as exc:
            # Treat AttributeError from a None self._conn as transient: the
            # prior reconnect likely lost the connection mid-call; retry with
            # a fresh one. Common symptom of Turso Hrana stream expiry under
            # load (#405).
            none_conn = isinstance(exc, AttributeError) and self._conn is None
            if _is_transient_db_error(exc) or "PanicException" in type(exc).__name__ or none_conn:
                self._breaker.record_failure(f"{type(exc).__name__}: {str(exc)[:150]}")
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
                    self._breaker.record_failure(
                        f"retry {type(retry_exc).__name__}: {str(retry_exc)[:150]}"
                    )
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


# ── Hrana retry helper ─────────────────────────────────────────────────────────

_HRANA_TRANSIENT_ERRORS = (
    "Stream already in use",
    "stream error",
    "Failed to checkpoint WAL",
    "database table is locked",
    # Embedded replica corruption — transient after container restart (#378)
    "file is not a database",
    "database disk image is malformed",
)


async def retry_on_hrana_conflict(coro_fn, *, max_retries: int = 3, label: str = ""):
    """Retry an async DB operation on transient Hrana/libsql errors.

    Applies exponential backoff (0.1s, 0.3s, 0.9s) for stream conflicts
    that occur under concurrent access to the single Turso connection (#297).
    """
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            msg = str(exc)
            if attempt < max_retries and any(e in msg for e in _HRANA_TRANSIENT_ERRORS):
                delay = 0.1 * (3**attempt)
                logger.warning(
                    "Hrana transient error (attempt %d/%d, retry in %.1fs) [%s]: %s",
                    attempt + 1,
                    max_retries,
                    delay,
                    label,
                    msg[:100],
                )
                await asyncio.sleep(delay)
            else:
                raise


# ── DatabaseBase ──────────────────────────────────────────────────────────────


class DatabaseBase:
    """Connection, lifecycle, and migration management."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        turso_url: str = "",
        turso_token: str = "",
        turso_replica_path: str = "",
    ) -> None:
        self.path = str(path)
        self._turso_url = turso_url
        self._turso_token = turso_token
        self._turso_replica_path = turso_replica_path
        self._use_turso = bool(turso_url)
        self._db: aiosqlite.Connection | _TursoConnection | None = None

    async def connect(self) -> None:
        if self._use_turso:
            conn = _TursoConnection(
                self._turso_url, self._turso_token, replica_path=self._turso_replica_path
            )
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

    @property
    def is_replica(self) -> bool:
        """True if using an embedded replica (local SQLite syncing from Turso)."""
        return isinstance(self._db, _TursoConnection) and self._db.is_replica

    async def sync_replica(self) -> None:
        """Pull latest changes from Turso primary into local replica. No-op if not a replica."""
        if isinstance(self._db, _TursoConnection):
            await self._db.sync()

    def circuit_breaker_stats(self) -> dict[str, Any] | None:
        """Live circuit-breaker telemetry for /readiness (#469 Phase 3).

        Returns None when running on local aiosqlite (no breaker) — tests and
        dev. Returns a stats dict when backed by Turso.
        """
        if isinstance(self._db, _TursoConnection):
            return self._db._breaker.stats()
        return None

    async def reconnect_if_stale(self, timeout: float = 10.0) -> bool:
        """Reconnect Turso connection if stale. Returns True if reconnected.

        Overall *timeout* (default 10s) caps the total time spent probing +
        reconnecting so that callers never block for 30-105s.
        """
        if self._use_turso and isinstance(self._db, _TursoConnection):
            try:
                return await asyncio.wait_for(self._reconnect_if_stale_inner(), timeout=timeout)
            except TimeoutError:
                logger.warning("reconnect_if_stale timed out after %.1fs", timeout)
                raise
        return False

    async def _reconnect_if_stale_inner(self) -> bool:
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
