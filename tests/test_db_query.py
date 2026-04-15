"""Tests for direct DB query tool."""

import json
from unittest.mock import MagicMock

from oncofiles.database import Database
from oncofiles.tools.db_query import query_db
from tests.helpers import ERIKA_UUID, make_doc


def _mock_ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    return ctx


async def test_select_query(db: Database):
    await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    ctx = _mock_ctx(db)
    result = json.loads(
        await query_db(
            ctx, f"SELECT COUNT(*) as cnt FROM documents WHERE patient_id = '{ERIKA_UUID}'"
        )
    )
    assert result["rows"][0]["cnt"] == 1


async def test_blocks_insert(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "INSERT INTO documents (id) VALUES (999)"))
    assert "error" in result
    assert "select" in result["error"].lower() or "read-only" in result["error"].lower()


async def test_blocks_delete(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "DELETE FROM documents WHERE id = 1"))
    assert "error" in result


async def test_blocks_drop(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "DROP TABLE documents"))
    assert "error" in result


async def test_blocks_update(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "UPDATE documents SET filename = 'hack' WHERE id = 1"))
    assert "error" in result


async def test_limit_enforced(db: Database):
    for i in range(5):
        await db.insert_document(make_doc(file_id=f"f{i}"), patient_id=ERIKA_UUID)
    ctx = _mock_ctx(db)
    result = json.loads(
        await query_db(ctx, f"SELECT * FROM documents WHERE patient_id = '{ERIKA_UUID}'", limit=3)
    )
    assert result["row_count"] == 3


async def test_invalid_sql(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "SELECT * FROM nonexistent_table"))
    assert "error" in result


async def test_with_clause(db: Database):
    """WITH (CTE) queries should work."""
    await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    ctx = _mock_ctx(db)
    result = json.loads(
        await query_db(
            ctx,
            "WITH d AS (SELECT * FROM documents"
            f" WHERE patient_id = '{ERIKA_UUID}')"
            " SELECT COUNT(*) as c FROM d",
        )
    )
    assert result["rows"][0]["c"] == 1


async def test_blocks_cross_patient_query(db: Database):
    """Queries on patient-scoped tables without patient_id filter are rejected."""
    from unittest.mock import patch

    ctx = _mock_ctx(db)
    with patch("oncofiles.tools.db_query._get_patient_id", return_value=ERIKA_UUID):
        result = json.loads(await query_db(ctx, "SELECT * FROM documents"))
        assert "error" in result
        assert "patient_id" in result["error"]


async def test_allows_non_patient_table(db: Database):
    """Queries on non-patient-scoped tables (e.g. schema_migrations) don't require patient_id."""
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "SELECT * FROM schema_migrations"))
    assert "error" not in result or "patient_id" not in result.get("error", "")
