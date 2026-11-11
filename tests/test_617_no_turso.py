"""#617: the Turso/libsql path is gone; a leftover TURSO_* variable stops startup.

The master store is the local SQLite file. One environment variable must never
be able to route PHI to a cloud database, so any non-empty ``TURSO_*`` value
is a hard error at startup, before the database is opened.
"""

from __future__ import annotations

import importlib.util
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from oncofiles import config, server
from oncofiles.database import Database

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "oncofiles"


@pytest.mark.parametrize(
    "env",
    [
        {"TURSO_DATABASE_URL": "libsql://example-db.invalid"},
        {"TURSO_AUTH_TOKEN": "not-a-real-token"},
        {"TURSO_REPLICA_PATH": "/tmp/replica.db"},
    ],
)
def test_any_turso_var_refuses(env):
    name = next(iter(env))
    with pytest.raises(RuntimeError, match="local SQLite") as exc:
        server._assert_local_database(env)
    assert name in str(exc.value)
    # Never echo the value itself (a token or URL) into logs.
    assert env[name] not in str(exc.value)


def test_empty_or_unrelated_vars_are_fine():
    server._assert_local_database({})
    server._assert_local_database({"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": "  "})
    server._assert_local_database({"DATABASE_PATH": "/data/oncofiles.db"})


def test_reads_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example-db.invalid")
    with pytest.raises(RuntimeError, match="TURSO_DATABASE_URL"):
        server._assert_local_database()


@pytest.mark.parametrize("var", ["TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"])
async def test_server_startup_fails_before_opening_any_database(monkeypatch, var):
    monkeypatch.setenv(var, "set-by-mistake")
    fake_db_cls = MagicMock()
    with (
        patch.object(server, "_setup_logging"),
        patch.object(server, "Database", fake_db_cls),
        pytest.raises(RuntimeError, match=var),
    ):
        async with server.lifespan(MagicMock()):
            pass  # pragma: no cover - startup must not get here
    fake_db_cls.assert_not_called()


def test_turso_code_is_gone():
    for name in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "TURSO_REPLICA_PATH"):
        assert not hasattr(config, name)
    assert importlib.util.find_spec("oncofiles.migrate_to_turso") is None
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        assert "import libsql" not in text, path
        assert "turso_url" not in text, path


def test_database_takes_only_a_path():
    with pytest.raises(TypeError):
        Database(turso_url="libsql://example-db.invalid")  # type: ignore[call-arg]


async def test_ping_runs_a_real_query(db):
    await db.ping(timeout=1.0)


async def test_ping_fails_when_not_connected():
    with pytest.raises(RuntimeError, match="not connected"):
        await Database(":memory:").ping(timeout=1.0)


# ── /readiness ping: short timeout, single attempt (infra review of #750) ──


def test_readiness_pings_with_at_most_two_seconds():
    import inspect
    import re

    source = inspect.getsource(server.readiness)
    timeouts = [float(t) for t in re.findall(r"db\.ping\(timeout=([0-9.]+)\)", source)]
    assert timeouts == [2.0]
    default = inspect.signature(Database.ping).parameters["timeout"].default
    assert default <= 2.0


async def test_ping_is_one_attempt_with_no_retry(db):
    import asyncio

    calls = []
    real_wait_for = asyncio.wait_for

    async def spy(aw, timeout):
        calls.append(timeout)
        return await real_wait_for(aw, timeout)

    with patch("oncofiles.database._base.asyncio.wait_for", spy):
        await db.ping()
    assert calls == [2.0]


async def test_ping_times_out_once_and_raises(db):
    import asyncio

    attempts = []

    class _SlowCursor:
        async def __aenter__(self):
            attempts.append(1)
            await asyncio.sleep(1.0)
            return self

        async def __aexit__(self, *a):
            return None

    fake_conn = MagicMock()
    fake_conn.execute = MagicMock(return_value=_SlowCursor())
    real = db._db
    db._db = fake_conn
    try:
        with pytest.raises(TimeoutError):
            await db.ping(timeout=0.05)
    finally:
        db._db = real
    assert attempts == [1]  # no retry
