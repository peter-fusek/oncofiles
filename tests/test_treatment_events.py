"""Tests for treatment events (#34)."""

from datetime import date

from erika_files_mcp.database import Database
from erika_files_mcp.models import TreatmentEventQuery

from .helpers import make_treatment_event


async def test_insert_and_get(db: Database):
    event = make_treatment_event()
    saved = await db.insert_treatment_event(event)
    assert saved.id is not None

    fetched = await db.get_treatment_event(saved.id)
    assert fetched is not None
    assert fetched.title == "FOLFOX cycle 3"
    assert fetched.event_type == "chemo"


async def test_get_not_found(db: Database):
    result = await db.get_treatment_event(9999)
    assert result is None


async def test_list_all(db: Database):
    await db.insert_treatment_event(make_treatment_event(event_date=date(2025, 3, 1)))
    await db.insert_treatment_event(make_treatment_event(event_date=date(2025, 2, 1)))

    events = await db.list_treatment_events(TreatmentEventQuery())
    assert len(events) == 2
    # DESC order — newest first
    assert events[0].event_date == date(2025, 3, 1)


async def test_list_filter_by_type(db: Database):
    await db.insert_treatment_event(make_treatment_event(event_type="chemo"))
    await db.insert_treatment_event(make_treatment_event(event_type="surgery"))

    events = await db.list_treatment_events(TreatmentEventQuery(event_type="surgery"))
    assert len(events) == 1
    assert events[0].event_type == "surgery"


async def test_list_filter_by_date_range(db: Database):
    await db.insert_treatment_event(make_treatment_event(event_date=date(2025, 1, 10)))
    await db.insert_treatment_event(make_treatment_event(event_date=date(2025, 2, 15)))
    await db.insert_treatment_event(make_treatment_event(event_date=date(2025, 3, 20)))

    events = await db.list_treatment_events(
        TreatmentEventQuery(date_from=date(2025, 2, 1), date_to=date(2025, 2, 28))
    )
    assert len(events) == 1
    assert events[0].event_date == date(2025, 2, 15)


async def test_list_with_limit(db: Database):
    for i in range(5):
        await db.insert_treatment_event(
            make_treatment_event(event_date=date(2025, 1, i + 1), title=f"Event {i}")
        )

    events = await db.list_treatment_events(TreatmentEventQuery(limit=3))
    assert len(events) == 3


async def test_timeline_chronological(db: Database):
    await db.insert_treatment_event(
        make_treatment_event(event_date=date(2025, 3, 1), title="March")
    )
    await db.insert_treatment_event(
        make_treatment_event(event_date=date(2025, 1, 1), title="January")
    )

    timeline = await db.get_treatment_events_timeline()
    assert len(timeline) == 2
    assert timeline[0].title == "January"
    assert timeline[1].title == "March"


async def test_metadata_stored(db: Database):
    event = make_treatment_event(metadata='{"drug": "oxaliplatin", "dose": "85mg/m2"}')
    saved = await db.insert_treatment_event(event)

    fetched = await db.get_treatment_event(saved.id)
    assert '"drug": "oxaliplatin"' in fetched.metadata


async def test_notes_stored(db: Database):
    event = make_treatment_event(notes="Patient tolerated well. No nausea.")
    saved = await db.insert_treatment_event(event)

    fetched = await db.get_treatment_event(saved.id)
    assert fetched.notes == "Patient tolerated well. No nausea."


async def test_timestamps_set(db: Database):
    saved = await db.insert_treatment_event(make_treatment_event())
    fetched = await db.get_treatment_event(saved.id)
    assert fetched.created_at is not None
