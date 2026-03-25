"""Tests for lab trend tracking (#59)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from oncofiles.database import Database
from oncofiles.models import LabTrendQuery
from tests.helpers import make_doc, make_lab_value


def _mock_ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock(), "gdrive": None}
    return ctx


# ── Database layer tests ──────────────────────────────────────────────────


async def test_insert_and_get_lab_trends(db: Database):
    """Insert lab values and retrieve by parameter."""
    doc = make_doc()
    doc = await db.insert_document(doc)

    values = [
        make_lab_value(document_id=doc.id, parameter="WBC", value=6.8),
        make_lab_value(document_id=doc.id, parameter="PLT", value=220.0, unit="10^9/L"),
    ]
    count = await db.insert_lab_values(values)
    assert count == 2

    # Query by parameter
    results = await db.get_lab_trends(LabTrendQuery(parameter="WBC"))
    assert len(results) == 1
    assert results[0].value == 6.8
    assert results[0].unit == "10^9/L"

    # Query all
    results = await db.get_lab_trends(LabTrendQuery())
    assert len(results) == 2


async def test_lab_values_idempotent(db: Database):
    """Inserting same document_id+parameter replaces, not duplicates."""
    doc = make_doc()
    doc = await db.insert_document(doc)

    v1 = [make_lab_value(document_id=doc.id, parameter="WBC", value=6.8)]
    await db.insert_lab_values(v1)

    # Re-insert with updated value
    v2 = [make_lab_value(document_id=doc.id, parameter="WBC", value=7.2)]
    await db.insert_lab_values(v2)

    results = await db.get_lab_trends(LabTrendQuery(parameter="WBC"))
    assert len(results) == 1
    assert results[0].value == 7.2


async def test_get_lab_snapshot(db: Database):
    """Get all lab values from a specific document."""
    doc = make_doc()
    doc = await db.insert_document(doc)

    values = [
        make_lab_value(document_id=doc.id, parameter="WBC", value=6.8),
        make_lab_value(document_id=doc.id, parameter="PLT", value=220.0),
        make_lab_value(document_id=doc.id, parameter="HGB", value=14.1),
    ]
    await db.insert_lab_values(values)

    snapshot = await db.get_lab_snapshot(doc.id)
    assert len(snapshot) == 3
    params = {v.parameter for v in snapshot}
    assert params == {"WBC", "PLT", "HGB"}


async def test_get_latest_lab_value(db: Database):
    """Get the most recent value for a parameter."""
    doc1 = make_doc(file_id="f1", filename="20260213_labs.pdf")
    doc1 = await db.insert_document(doc1)
    doc2 = make_doc(file_id="f2", filename="20260227_labs.pdf")
    doc2 = await db.insert_document(doc2)

    values = [
        make_lab_value(
            document_id=doc1.id, lab_date=date(2026, 2, 13), parameter="CEA", value=1559.5
        ),
        make_lab_value(
            document_id=doc2.id, lab_date=date(2026, 2, 27), parameter="CEA", value=1200.0
        ),
    ]
    await db.insert_lab_values(values)

    latest = await db.get_latest_lab_value("CEA")
    assert latest is not None
    assert latest.value == 1200.0
    assert latest.lab_date == date(2026, 2, 27)


async def test_get_latest_lab_value_none(db: Database):
    """Returns None when no values exist."""
    result = await db.get_latest_lab_value("NONEXISTENT")
    assert result is None


async def test_lab_trends_date_filter(db: Database):
    """Filter lab values by date range."""
    doc1 = make_doc(file_id="f1", filename="20260213_labs.pdf")
    doc1 = await db.insert_document(doc1)
    doc2 = make_doc(file_id="f2", filename="20260227_labs.pdf")
    doc2 = await db.insert_document(doc2)

    values = [
        make_lab_value(
            document_id=doc1.id, lab_date=date(2026, 2, 13), parameter="PLT", value=200.0
        ),
        make_lab_value(
            document_id=doc2.id, lab_date=date(2026, 2, 27), parameter="PLT", value=180.0
        ),
    ]
    await db.insert_lab_values(values)

    # Only February 20+
    results = await db.get_lab_trends(LabTrendQuery(parameter="PLT", date_from=date(2026, 2, 20)))
    assert len(results) == 1
    assert results[0].value == 180.0


async def test_lab_trends_chronological_order(db: Database):
    """Values are returned in chronological order (oldest first)."""
    doc1 = make_doc(file_id="f1", filename="20260227_labs.pdf")
    doc1 = await db.insert_document(doc1)
    doc2 = make_doc(file_id="f2", filename="20260213_labs.pdf")
    doc2 = await db.insert_document(doc2)

    values = [
        make_lab_value(
            document_id=doc1.id, lab_date=date(2026, 2, 27), parameter="SII", value=1800.0
        ),
        make_lab_value(
            document_id=doc2.id, lab_date=date(2026, 2, 13), parameter="SII", value=2500.0
        ),
    ]
    await db.insert_lab_values(values)

    results = await db.get_lab_trends(LabTrendQuery(parameter="SII"))
    assert len(results) == 2
    assert results[0].lab_date == date(2026, 2, 13)  # older first
    assert results[1].lab_date == date(2026, 2, 27)


# ── Tool-level tests ──────────────────────────────────────────────────────


async def test_store_lab_values_tool(db: Database):
    """store_lab_values tool stores values and returns summary."""
    import json

    from oncofiles.tools.lab_trends import store_lab_values

    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    values_json = json.dumps(
        [
            {
                "parameter": "WBC",
                "value": 6.8,
                "unit": "10^9/L",
                "reference_low": 4.0,
                "reference_high": 10.0,
            },
            {"parameter": "PLT", "value": 220, "unit": "10^9/L"},
            {"parameter": "SII", "value": 1850.5, "unit": ""},
        ]
    )
    result = json.loads(await store_lab_values(ctx, doc.id, "2026-02-27", values_json))
    assert result["stored"] == 3
    assert "SII" in result["parameters"]


async def test_store_lab_values_invalid_document(db: Database):
    """store_lab_values returns error for non-existent document."""
    import json

    from oncofiles.tools.lab_trends import store_lab_values

    ctx = _mock_ctx(db)
    result = json.loads(await store_lab_values(ctx, 9999, "2026-02-27", "[]"))
    assert "error" in result


async def test_get_lab_trends_tool(db: Database):
    """get_lab_trends tool returns stored values."""
    import json

    from oncofiles.tools.lab_trends import get_lab_trends, store_lab_values

    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    values_json = json.dumps(
        [
            {"parameter": "CEA", "value": 1559.5, "unit": "ug/L"},
        ]
    )
    await store_lab_values(ctx, doc.id, "2026-02-27", values_json)

    result = json.loads(await get_lab_trends(ctx, parameter="CEA"))
    assert result["total"] == 1
    assert result["values"][0]["value"] == 1559.5


async def test_get_lab_trends_empty(db: Database):
    """get_lab_trends returns empty array when no values exist."""
    import json

    from oncofiles.tools.lab_trends import get_lab_trends

    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_trends(ctx))
    assert result["total"] == 0
    assert result["values"] == []


# ── Edge cases ───────────────────────────────────────────────────────────


async def test_store_lab_values_invalid_json(db: Database):
    """store_lab_values returns error on invalid JSON."""
    import json

    from oncofiles.tools.lab_trends import store_lab_values

    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    result = json.loads(await store_lab_values(ctx, doc.id, "2026-02-27", "not json"))
    assert "error" in result
    assert "Invalid JSON" in result["error"]


async def test_store_lab_values_empty_array(db: Database):
    """store_lab_values returns error on empty values array."""
    import json

    from oncofiles.tools.lab_trends import store_lab_values

    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    result = json.loads(await store_lab_values(ctx, doc.id, "2026-02-27", "[]"))
    assert "error" in result
    assert "No valid lab values" in result["error"]


async def test_store_lab_values_not_array(db: Database):
    """store_lab_values returns error when values is not an array."""
    import json

    from oncofiles.tools.lab_trends import store_lab_values

    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    result = json.loads(await store_lab_values(ctx, doc.id, "2026-02-27", '{"parameter": "WBC"}'))
    assert "error" in result
    assert "JSON array" in result["error"]


async def test_store_lab_values_duplicate_document_upsert(db: Database):
    """Storing values twice for same document replaces them (idempotent)."""
    import json

    from oncofiles.tools.lab_trends import get_lab_trends, store_lab_values

    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    values_v1 = json.dumps([{"parameter": "CEA", "value": 100.0, "unit": "ug/L"}])
    await store_lab_values(ctx, doc.id, "2026-02-27", values_v1)

    # Second call without force should be skipped (dedup)
    values_v2 = json.dumps([{"parameter": "CEA", "value": 200.0, "unit": "ug/L"}])
    skip_result = json.loads(await store_lab_values(ctx, doc.id, "2026-02-27", values_v2))
    assert skip_result["action"] == "skipped"
    assert skip_result["reason"] == "already_stored"

    # With force=True, should replace
    await store_lab_values(ctx, doc.id, "2026-02-27", values_v2, force=True)

    result = json.loads(await get_lab_trends(ctx, parameter="CEA"))
    assert result["total"] == 1
    assert result["values"][0]["value"] == 200.0
