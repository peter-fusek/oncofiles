"""Tests for batch query methods and query_db hardening."""

from __future__ import annotations

from tests.helpers import ERIKA_UUID, make_doc

# ── get_documents_by_ids ─────────────────────────────────────────────


async def test_get_documents_by_ids_empty(db):
    """Empty ID set returns empty dict."""
    result = await db.get_documents_by_ids(set(), patient_id=ERIKA_UUID)
    assert result == {}


async def test_get_documents_by_ids_single(db):
    """Single ID returns matching document."""
    doc = await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    result = await db.get_documents_by_ids({doc.id}, patient_id=ERIKA_UUID)
    assert doc.id in result
    assert result[doc.id].filename == doc.filename


async def test_get_documents_by_ids_multiple(db):
    """Multiple IDs returned in single query."""
    d1 = await db.insert_document(make_doc(file_id="f1", filename="a.pdf"), patient_id=ERIKA_UUID)
    d2 = await db.insert_document(make_doc(file_id="f2", filename="b.pdf"), patient_id=ERIKA_UUID)
    d3 = await db.insert_document(make_doc(file_id="f3", filename="c.pdf"), patient_id=ERIKA_UUID)
    result = await db.get_documents_by_ids({d1.id, d2.id, d3.id}, patient_id=ERIKA_UUID)
    assert len(result) == 3
    assert result[d1.id].filename == "a.pdf"
    assert result[d3.id].filename == "c.pdf"


async def test_get_documents_by_ids_missing(db):
    """Missing IDs are simply absent from result."""
    doc = await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    result = await db.get_documents_by_ids({doc.id, 9999}, patient_id=ERIKA_UUID)
    assert len(result) == 1
    assert doc.id in result


# ── get_previous_lab_values ──────────────────────────────────────────


async def test_get_previous_lab_values_empty(db):
    """No lab values returns empty dict."""
    result = await db.get_previous_lab_values()
    assert result == {}


async def test_get_previous_lab_values_single_entry(db):
    """Single entry per parameter means no previous — returns empty."""
    from datetime import date

    from oncofiles.models import LabValue

    d1 = await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    v1 = LabValue(
        lab_date=date(2026, 3, 1), parameter="WBC", value=5.0, unit="10^9/L", document_id=d1.id
    )
    await db.insert_lab_values([v1])
    result = await db.get_previous_lab_values()
    assert result == {}


async def test_get_previous_lab_values_two_entries(db):
    """Two entries returns the older one as previous."""
    from datetime import date

    from oncofiles.models import LabValue

    d1 = await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    d2 = await db.insert_document(make_doc(file_id="f2"), patient_id=ERIKA_UUID)
    await db.insert_lab_values(
        [
            LabValue(
                lab_date=date(2026, 3, 1),
                parameter="WBC",
                value=5.0,
                unit="10^9/L",
                document_id=d1.id,
            ),
            LabValue(
                lab_date=date(2026, 3, 10),
                parameter="WBC",
                value=6.0,
                unit="10^9/L",
                document_id=d2.id,
            ),
        ]
    )
    result = await db.get_previous_lab_values()
    assert "WBC" in result
    assert result["WBC"].value == 5.0
    assert result["WBC"].lab_date == date(2026, 3, 1)


# ── query_db hardening ───────────────────────────────────────────────
# The regex-based `_ALLOWED_PREFIX` / `_FORBIDDEN_KEYWORDS` bypasses were
# replaced by sqlglot-AST validation in #486. Bypass corpus lives in
# tests/test_query_db_hardened.py. This file keeps just the timeout guard.


def test_query_db_has_timeout():
    """query_db has a timeout guard."""
    from oncofiles.tools.db_query import QUERY_TIMEOUT_S

    assert QUERY_TIMEOUT_S > 0
    assert QUERY_TIMEOUT_S <= 30  # reasonable upper bound
