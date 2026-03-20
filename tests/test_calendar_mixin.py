"""Tests for Calendar entry database operations."""

from datetime import datetime

from oncofiles.database import Database
from oncofiles.models import CalendarEntry, CalendarQuery


def _make_event(**kwargs) -> CalendarEntry:
    defaults = {
        "google_event_id": "evt_001",
        "summary": "Chemo cycle 3",
        "description": "FOLFOX C3 at University Hospital",
        "start_time": datetime(2026, 3, 20, 9, 0),
        "end_time": datetime(2026, 3, 20, 14, 0),
        "location": "University Hospital Bratislava",
        "attendees": '["doctor@hospital.sk"]',
        "status": "confirmed",
        "is_medical": True,
    }
    defaults.update(kwargs)
    return CalendarEntry(**defaults)


async def test_upsert_and_get(db: Database):
    entry = _make_event()
    saved = await db.upsert_calendar_entry(entry)
    assert saved.id is not None
    assert saved.summary == "Chemo cycle 3"

    fetched = await db.get_calendar_entry(saved.id)
    assert fetched is not None
    assert fetched.google_event_id == "evt_001"


async def test_get_by_google_id(db: Database):
    await db.upsert_calendar_entry(_make_event())
    fetched = await db.get_calendar_entry_by_google_id("evt_001")
    assert fetched is not None
    assert fetched.summary == "Chemo cycle 3"


async def test_upsert_idempotent(db: Database):
    entry = _make_event()
    first = await db.upsert_calendar_entry(entry)
    entry.summary = "Updated chemo"
    second = await db.upsert_calendar_entry(entry)
    assert first.id == second.id
    assert second.summary == "Updated chemo"


async def test_search_by_text(db: Database):
    await db.upsert_calendar_entry(_make_event(google_event_id="e1", summary="Chemo"))
    await db.upsert_calendar_entry(_make_event(google_event_id="e2", summary="Dentist"))
    results = await db.search_calendar_entries(CalendarQuery(text="Chemo"))
    assert len(results) == 1


async def test_search_medical_only(db: Database):
    await db.upsert_calendar_entry(_make_event(google_event_id="e1", is_medical=True))
    await db.upsert_calendar_entry(_make_event(google_event_id="e2", is_medical=False))
    results = await db.search_calendar_entries(CalendarQuery(is_medical=True))
    assert len(results) == 1


async def test_list_entries(db: Database):
    for i in range(5):
        await db.upsert_calendar_entry(_make_event(google_event_id=f"e{i}"))
    entries = await db.list_calendar_entries(limit=3)
    assert len(entries) == 3


async def test_count(db: Database):
    assert await db.count_calendar_entries() == 0
    await db.upsert_calendar_entry(_make_event())
    assert await db.count_calendar_entries() == 1


async def test_get_nonexistent(db: Database):
    assert await db.get_calendar_entry(999) is None
    assert await db.get_calendar_entry_by_google_id("nonexistent") is None
