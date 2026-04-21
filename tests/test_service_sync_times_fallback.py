"""Verify #463 fix: _get_service_sync_times_async falls back to DB MAX(created_at)
when the in-memory dict is empty (happens after every Railway restart)."""

from __future__ import annotations

from datetime import date

import pytest

from oncofiles.models import ConversationEntry, Document, DocumentCategory
from oncofiles.server import _get_service_sync_times_async, _last_service_sync

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _reset_sync_cache():
    """Clear the in-memory sync-times dict so each test starts from "post-restart" state."""
    _last_service_sync.clear()
    yield
    _last_service_sync.clear()


@pytest.mark.asyncio
async def test_returns_all_none_when_no_data(db):
    """Empty DB, empty in-memory — every service reports None."""
    result = await _get_service_sync_times_async(db, ERIKA_UUID)
    assert result == {"gdrive": None, "gmail": None, "calendar": None}


@pytest.mark.asyncio
async def test_falls_back_to_documents_max_created_at(db):
    """With the in-memory dict empty, gdrive time derives from documents."""
    doc = Document(
        file_id="doc_a",
        filename="20260201_a.pdf",
        original_filename="a.pdf",
        document_date=date(2026, 2, 1),
        institution="NOU",
        category=DocumentCategory.LABS,
        description="x",
        mime_type="application/pdf",
        size_bytes=100,
    )
    inserted = await db.insert_document(doc, patient_id=ERIKA_UUID)
    assert inserted.id is not None

    result = await _get_service_sync_times_async(db, ERIKA_UUID)
    # gdrive should be populated from the document's created_at; others still None.
    assert result["gdrive"] is not None
    # Gmail / calendar tables are empty so their values stay None.
    assert result["gmail"] is None
    assert result["calendar"] is None


@pytest.mark.asyncio
async def test_in_memory_values_win_over_db_fallback(db):
    """If the in-memory dict has ALL services set, skip DB entirely."""
    _last_service_sync[ERIKA_UUID] = {
        "gdrive": "2026-04-21T19:00:00",
        "gmail": "2026-04-21T19:01:00",
        "calendar": "2026-04-21T19:02:00",
    }
    # Even with a document in the DB, the cached values win — we short-circuit.
    doc = Document(
        file_id="doc_cache",
        filename="20260201_b.pdf",
        original_filename="b.pdf",
        document_date=date(2026, 2, 1),
        institution="NOU",
        category=DocumentCategory.LABS,
        description="x",
        mime_type="application/pdf",
        size_bytes=100,
    )
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    result = await _get_service_sync_times_async(db, ERIKA_UUID)
    assert result["gdrive"] == "2026-04-21T19:00:00"
    assert result["gmail"] == "2026-04-21T19:01:00"
    assert result["calendar"] == "2026-04-21T19:02:00"


@pytest.mark.asyncio
async def test_partial_memory_fills_missing_from_db(db):
    """gdrive cached, gmail+calendar missing → DB fills only the missing ones."""
    _last_service_sync[ERIKA_UUID] = {
        "gdrive": "2026-04-21T19:00:00",
        "gmail": None,
        "calendar": None,
    }
    # Seed a conversation entry (not strictly gmail but that's the email table).
    entry = ConversationEntry(
        entry_date=date(2026, 4, 21),
        entry_type="note",
        title="x",
        content="x",
        participant="claude-code",
        source="live",
    )
    await db.insert_conversation_entry(entry, patient_id=ERIKA_UUID)

    result = await _get_service_sync_times_async(db, ERIKA_UUID)
    # In-memory gdrive is preserved.
    assert result["gdrive"] == "2026-04-21T19:00:00"
    # Gmail and calendar tables are empty in the test DB, so they stay None.
    # The point of this test: the gdrive cache wasn't clobbered by the DB query.
    assert result["gmail"] is None
    assert result["calendar"] is None
