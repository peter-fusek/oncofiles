"""Tests for direct DB query tool."""

import json
from unittest.mock import MagicMock

from oncofiles.database import Database
from oncofiles.tools.db_query import query_db
from tests.helpers import make_doc


def _mock_ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    return ctx


async def test_select_query(db: Database):
    await db.insert_document(make_doc(file_id="f1"))
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "SELECT COUNT(*) as cnt FROM documents"))
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
        await db.insert_document(make_doc(file_id=f"f{i}"))
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "SELECT * FROM documents", limit=3))
    assert result["row_count"] == 3


async def test_invalid_sql(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await query_db(ctx, "SELECT * FROM nonexistent_table"))
    assert "error" in result


async def test_with_clause(db: Database):
    """WITH (CTE) queries should work."""
    await db.insert_document(make_doc(file_id="f1"))
    ctx = _mock_ctx(db)
    result = json.loads(
        await query_db(ctx, "WITH d AS (SELECT * FROM documents) SELECT COUNT(*) as c FROM d")
    )
    assert result["rows"][0]["c"] == 1
