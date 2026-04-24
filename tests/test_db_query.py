"""Tests for direct DB query tool — integration-level (real DB round-trip).

The unit-level validator coverage lives in test_query_db_hardened.py.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.database import Database
from oncofiles.tools.db_query import query_db
from tests.helpers import ERIKA_UUID, make_doc


def _mock_ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    return ctx


@pytest.fixture
def resolve_to_erika():
    """Resolve every query_db call in this file to ERIKA_UUID by default."""
    with patch(
        "oncofiles.tools.db_query._resolve_patient_id",
        new=AsyncMock(return_value=ERIKA_UUID),
    ) as mocked:
        yield mocked


async def test_select_query(db: Database, resolve_to_erika):
    await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    ctx = _mock_ctx(db)
    result = json.loads(
        await query_db(
            ctx, f"SELECT COUNT(*) as cnt FROM documents WHERE patient_id = '{ERIKA_UUID}'"
        )
    )
    assert "error" not in result, result
    assert result["rows"][0]["cnt"] == 1


async def test_blocks_insert(db: Database, resolve_to_erika):
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "INSERT INTO documents (id) VALUES (999)"))
    assert "error" in result


async def test_blocks_delete(db: Database, resolve_to_erika):
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "DELETE FROM documents WHERE id = 1"))
    assert "error" in result


async def test_blocks_drop(db: Database, resolve_to_erika):
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "DROP TABLE documents"))
    assert "error" in result


async def test_blocks_update(db: Database, resolve_to_erika):
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "UPDATE documents SET filename = 'hack' WHERE id = 1"))
    assert "error" in result


async def test_limit_enforced(db: Database, resolve_to_erika):
    for i in range(5):
        await db.insert_document(make_doc(file_id=f"f{i}"), patient_id=ERIKA_UUID)
    ctx = _mock_ctx(db)
    result = json.loads(
        await query_db(ctx, f"SELECT * FROM documents WHERE patient_id = '{ERIKA_UUID}'", limit=3)
    )
    assert result["row_count"] == 3


async def test_unknown_table_rejected_by_allowlist(db: Database, resolve_to_erika):
    """Nonexistent table now trips the allow-list before reaching the DB."""
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "SELECT * FROM nonexistent_table"))
    assert "error" in result
    assert "allow-list" in result["error"] or "not allowed" in result["error"]


async def test_cte_rejected_post_486(db: Database, resolve_to_erika):
    """CTEs are rejected by the hardened validator — document this explicitly."""
    ctx = _mock_ctx(db)
    result = json.loads(
        await query_db(
            ctx,
            "WITH d AS (SELECT * FROM documents"
            f" WHERE patient_id = '{ERIKA_UUID}')"
            " SELECT COUNT(*) as c FROM d",
        )
    )
    assert "error" in result
    assert "CTE" in result["error"]


async def test_blocks_cross_patient_query(db: Database, resolve_to_erika):
    """Patient-scoped table without the caller's pid literal is rejected."""
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "SELECT * FROM documents"))
    assert "error" in result
    assert "patient_id" in result["error"]


async def test_allows_admin_only_table(db: Database, resolve_to_erika):
    """schema_migrations has no patient_id; allow-listed as admin-only."""
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "SELECT * FROM schema_migrations"))
    assert "error" not in result
