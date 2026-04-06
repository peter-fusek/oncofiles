"""Tests for _converters.py date safety functions (#258)."""

from datetime import date

from oncofiles.database._converters import _safe_date


def test_safe_date_valid():
    assert _safe_date("2026-03-15") == date(2026, 3, 15)


def test_safe_date_none():
    assert _safe_date(None) is None


def test_safe_date_empty():
    assert _safe_date("") is None


def test_safe_date_invalid_month():
    """AI-hallucinated date like 2222-14-81 should return None."""
    assert _safe_date("2222-14-81") is None


def test_safe_date_invalid_day():
    assert _safe_date("2026-02-30") is None


def test_safe_date_garbage():
    assert _safe_date("not-a-date") is None


def test_safe_date_partial():
    assert _safe_date("2026-03") is None


async def test_list_documents_survives_bad_date(db):
    """list_documents should skip rows with corrupt data, not crash (#258)."""
    from tests.helpers import ERIKA_UUID, make_doc

    # Insert a valid doc
    doc = make_doc(file_id="file_good", document_date=date(2026, 1, 1))
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    # Manually corrupt a row's date
    await db.db.execute(
        "INSERT INTO documents (file_id, filename, original_filename, document_date, "
        "category, patient_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("file_bad", "bad.pdf", "bad.pdf", "2222-14-81", "other", ERIKA_UUID),
    )
    await db.db.commit()

    # list_documents should return both — bad date becomes None, doesn't crash
    docs = await db.list_documents(limit=50, patient_id=ERIKA_UUID)
    filenames = [d.filename for d in docs]
    assert "20240115_NOUonko_labs_krvnyObraz.pdf" in filenames
    assert "bad.pdf" in filenames
    bad_doc = next(d for d in docs if d.filename == "bad.pdf")
    assert bad_doc.document_date is None  # invalid date → None, not crash
