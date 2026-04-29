"""Tests for treatment events (#34)."""

from datetime import date

from oncofiles.database import Database
from oncofiles.models import TreatmentEventQuery

from .helpers import ERIKA_UUID, make_treatment_event


async def test_insert_and_get(db: Database):
    event = make_treatment_event()
    saved = await db.insert_treatment_event(event, patient_id=ERIKA_UUID)
    assert saved.id is not None

    fetched = await db.get_treatment_event(saved.id, patient_id=ERIKA_UUID)
    assert fetched is not None
    assert fetched.title == "FOLFOX cycle 3"
    assert fetched.event_type == "chemo"


async def test_get_not_found(db: Database):
    result = await db.get_treatment_event(9999, patient_id=ERIKA_UUID)
    assert result is None


async def test_list_all(db: Database):
    await db.insert_treatment_event(
        make_treatment_event(event_date=date(2025, 3, 1)), patient_id=ERIKA_UUID
    )
    await db.insert_treatment_event(
        make_treatment_event(event_date=date(2025, 2, 1)), patient_id=ERIKA_UUID
    )

    events = await db.list_treatment_events(TreatmentEventQuery(), patient_id=ERIKA_UUID)
    assert len(events) == 2
    # DESC order — newest first
    assert events[0].event_date == date(2025, 3, 1)


async def test_list_filter_by_type(db: Database):
    await db.insert_treatment_event(make_treatment_event(event_type="chemo"), patient_id=ERIKA_UUID)
    await db.insert_treatment_event(
        make_treatment_event(event_type="surgery"), patient_id=ERIKA_UUID
    )

    events = await db.list_treatment_events(
        TreatmentEventQuery(event_type="surgery"), patient_id=ERIKA_UUID
    )
    assert len(events) == 1
    assert events[0].event_type == "surgery"


async def test_list_filter_by_date_range(db: Database):
    await db.insert_treatment_event(
        make_treatment_event(event_date=date(2025, 1, 10)), patient_id=ERIKA_UUID
    )
    await db.insert_treatment_event(
        make_treatment_event(event_date=date(2025, 2, 15)), patient_id=ERIKA_UUID
    )
    await db.insert_treatment_event(
        make_treatment_event(event_date=date(2025, 3, 20)), patient_id=ERIKA_UUID
    )

    events = await db.list_treatment_events(
        TreatmentEventQuery(date_from=date(2025, 2, 1), date_to=date(2025, 2, 28)),
        patient_id=ERIKA_UUID,
    )
    assert len(events) == 1
    assert events[0].event_date == date(2025, 2, 15)


async def test_list_with_limit(db: Database):
    for i in range(5):
        await db.insert_treatment_event(
            make_treatment_event(event_date=date(2025, 1, i + 1), title=f"Event {i}"),
            patient_id=ERIKA_UUID,
        )

    events = await db.list_treatment_events(TreatmentEventQuery(limit=3), patient_id=ERIKA_UUID)
    assert len(events) == 3


async def test_timeline_chronological(db: Database):
    await db.insert_treatment_event(
        make_treatment_event(event_date=date(2025, 3, 1), title="March"),
        patient_id=ERIKA_UUID,
    )
    await db.insert_treatment_event(
        make_treatment_event(event_date=date(2025, 1, 1), title="January"),
        patient_id=ERIKA_UUID,
    )

    timeline = await db.get_treatment_events_timeline(patient_id=ERIKA_UUID)
    assert len(timeline) == 2
    assert timeline[0].title == "January"
    assert timeline[1].title == "March"


async def test_metadata_stored(db: Database):
    event = make_treatment_event(metadata='{"drug": "oxaliplatin", "dose": "85mg/m2"}')
    saved = await db.insert_treatment_event(event, patient_id=ERIKA_UUID)

    fetched = await db.get_treatment_event(saved.id, patient_id=ERIKA_UUID)
    assert '"drug": "oxaliplatin"' in fetched.metadata


async def test_notes_stored(db: Database):
    event = make_treatment_event(notes="Patient tolerated well. No nausea.")
    saved = await db.insert_treatment_event(event, patient_id=ERIKA_UUID)

    fetched = await db.get_treatment_event(saved.id, patient_id=ERIKA_UUID)
    assert fetched.notes == "Patient tolerated well. No nausea."


async def test_timestamps_set(db: Database):
    saved = await db.insert_treatment_event(make_treatment_event(), patient_id=ERIKA_UUID)
    fetched = await db.get_treatment_event(saved.id, patient_id=ERIKA_UUID)
    assert fetched.created_at is not None
