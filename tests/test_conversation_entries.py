"""Tests for conversation archive / worklog diary (#37)."""

from datetime import date

from oncofiles.database import Database
from oncofiles.models import ConversationEntry, ConversationQuery

# ── Helpers ──────────────────────────────────────────────────────────────────


def make_entry(**overrides) -> ConversationEntry:
    defaults = {
        "entry_date": date(2025, 3, 1),
        "entry_type": "note",
        "title": "Test entry",
        "content": "This is a test diary entry about chemotherapy.",
        "participant": "claude.ai",
        "tags": ["chemo", "test"],
    }
    defaults.update(overrides)
    return ConversationEntry(**defaults)


# ── CRUD ─────────────────────────────────────────────────────────────────────


async def test_insert_and_get(db: Database):
    entry = make_entry()
    inserted = await db.insert_conversation_entry(entry, patient_id="erika")
    assert inserted.id is not None

    fetched = await db.get_conversation_entry(inserted.id)
    assert fetched is not None
    assert fetched.title == "Test entry"
    assert fetched.entry_date == date(2025, 3, 1)
    assert fetched.tags == ["chemo", "test"]


async def test_get_not_found(db: Database):
    result = await db.get_conversation_entry(9999)
    assert result is None


async def test_delete(db: Database):
    entry = await db.insert_conversation_entry(make_entry(), patient_id="erika")
    deleted = await db.delete_conversation_entry(entry.id)
    assert deleted is True
    assert await db.get_conversation_entry(entry.id) is None


async def test_delete_not_found(db: Database):
    deleted = await db.delete_conversation_entry(9999)
    assert deleted is False


# ── FTS search ───────────────────────────────────────────────────────────────


async def test_search_fts(db: Database):
    await db.insert_conversation_entry(
        make_entry(title="FOLFOX cycle 3 summary", content="Started FOLFOX cycle 3 today."),
        patient_id="erika",
    )
    await db.insert_conversation_entry(
        make_entry(title="Lab review", content="Blood counts normal."),
        patient_id="erika",
    )

    results = await db.search_conversation_entries(
        ConversationQuery(text="FOLFOX"), patient_id="erika"
    )
    assert len(results) == 1
    assert "FOLFOX" in results[0].title


async def test_search_by_entry_type(db: Database):
    await db.insert_conversation_entry(make_entry(entry_type="decision"), patient_id="erika")
    await db.insert_conversation_entry(make_entry(entry_type="note"), patient_id="erika")

    results = await db.search_conversation_entries(
        ConversationQuery(entry_type="decision"), patient_id="erika"
    )
    assert len(results) == 1
    assert results[0].entry_type == "decision"


async def test_search_by_participant(db: Database):
    await db.insert_conversation_entry(make_entry(participant="claude-code"), patient_id="erika")
    await db.insert_conversation_entry(make_entry(participant="claude.ai"), patient_id="erika")

    results = await db.search_conversation_entries(
        ConversationQuery(participant="claude-code"), patient_id="erika"
    )
    assert len(results) == 1
    assert results[0].participant == "claude-code"


async def test_search_by_date_range(db: Database):
    await db.insert_conversation_entry(make_entry(entry_date=date(2025, 1, 10)), patient_id="erika")
    await db.insert_conversation_entry(make_entry(entry_date=date(2025, 2, 15)), patient_id="erika")
    await db.insert_conversation_entry(make_entry(entry_date=date(2025, 3, 20)), patient_id="erika")

    results = await db.search_conversation_entries(
        ConversationQuery(date_from=date(2025, 2, 1), date_to=date(2025, 2, 28)),
        patient_id="erika",
    )
    assert len(results) == 1
    assert results[0].entry_date == date(2025, 2, 15)


async def test_search_by_tags(db: Database):
    await db.insert_conversation_entry(make_entry(tags=["chemo", "FOLFOX"]), patient_id="erika")
    await db.insert_conversation_entry(make_entry(tags=["labs", "blood"]), patient_id="erika")

    results = await db.search_conversation_entries(
        ConversationQuery(tags=["FOLFOX"]), patient_id="erika"
    )
    assert len(results) == 1
    assert "FOLFOX" in results[0].tags


# ── Timeline ─────────────────────────────────────────────────────────────────


async def test_timeline_chronological_order(db: Database):
    await db.insert_conversation_entry(
        make_entry(entry_date=date(2025, 3, 1), title="March"), patient_id="erika"
    )
    await db.insert_conversation_entry(
        make_entry(entry_date=date(2025, 1, 1), title="January"), patient_id="erika"
    )
    await db.insert_conversation_entry(
        make_entry(entry_date=date(2025, 2, 1), title="February"), patient_id="erika"
    )

    timeline = await db.get_conversation_timeline(patient_id="erika")
    assert len(timeline) == 3
    assert timeline[0].title == "January"
    assert timeline[1].title == "February"
    assert timeline[2].title == "March"


async def test_timeline_with_date_range(db: Database):
    await db.insert_conversation_entry(make_entry(entry_date=date(2025, 1, 1)), patient_id="erika")
    await db.insert_conversation_entry(make_entry(entry_date=date(2025, 6, 1)), patient_id="erika")

    timeline = await db.get_conversation_timeline(
        date_from=date(2025, 5, 1),
        date_to=date(2025, 7, 1),
        patient_id="erika",
    )
    assert len(timeline) == 1
    assert timeline[0].entry_date == date(2025, 6, 1)


# ── Source ref idempotency ───────────────────────────────────────────────────


async def test_source_ref_idempotency(db: Database):
    entry = make_entry(source="import", source_ref="session_abc.jsonl")
    await db.insert_conversation_entry(entry, patient_id="erika")

    found = await db.get_entry_by_source_ref("session_abc.jsonl", patient_id="erika")
    assert found is not None
    assert found.source_ref == "session_abc.jsonl"


async def test_source_ref_not_found(db: Database):
    result = await db.get_entry_by_source_ref("nonexistent.jsonl", patient_id="erika")
    assert result is None


# ── Document IDs ─────────────────────────────────────────────────────────────


async def test_document_ids_stored_and_retrieved(db: Database):
    entry = make_entry(document_ids=[3, 15, 22])
    inserted = await db.insert_conversation_entry(entry, patient_id="erika")

    fetched = await db.get_conversation_entry(inserted.id)
    assert fetched.document_ids == [3, 15, 22]
