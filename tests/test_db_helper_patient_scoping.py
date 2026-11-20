"""DB-layer patient-scoping lock-in for #499 / #514 / #515 / #516 / #517.

These tests bypass the MCP tool layer (no ownership check, no resolver) and
hit the database helpers directly with a *foreign* ``patient_id``. They lock
in the data-layer guarantee added in v5.17 Session 2: the SQL ``AND
patient_id = ?`` filter (or, for cross-references, the join filter) is the
chokepoint, not the caller's pre-check. If a future caller forgets the tool-
layer ownership check, the helper itself still refuses to read, mutate, or
disclose another patient's row.

Companion tests in ``test_option_a_*`` exercise the same invariants from the
tool layer; this file proves the DB itself is the line of defence.
"""

from __future__ import annotations

from datetime import date

import pytest

from oncofiles.database import Database
from oncofiles.models import (
    ClinicalRecord,
    ConversationEntry,
    Document,
    DocumentCategory,
    TreatmentEvent,
)
from tests.conftest import ERIKA_UUID

SECOND_UUID = "00000000-0000-4000-8000-000000000002"
SECOND_SLUG = "bob-test"


async def _seed_two_patients(db: Database) -> dict:
    """Seed Bob alongside Jana and return one row id per scoped helper."""
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (SECOND_UUID, SECOND_SLUG, "Bob Test", "bob@example.com"),
    )
    await db.db.commit()

    jana_doc = await db.insert_document(
        Document(
            file_id="jana-doc-file",
            filename="jana.pdf",
            original_filename="jana.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
            category=DocumentCategory.LABS,
        ),
        patient_id=ERIKA_UUID,
    )
    bob_doc = await db.insert_document(
        Document(
            file_id="bob-doc-file",
            filename="bob.pdf",
            original_filename="bob.pdf",
            mime_type="application/pdf",
            size_bytes=2048,
            category=DocumentCategory.LABS,
        ),
        patient_id=SECOND_UUID,
    )

    jana_event = await db.insert_treatment_event(
        TreatmentEvent(event_date=date(2031, 6, 14), event_type="chemo", title="Jana C1"),
        patient_id=ERIKA_UUID,
    )
    bob_event = await db.insert_treatment_event(
        TreatmentEvent(event_date=date(2031, 6, 15), event_type="chemo", title="Bob C1"),
        patient_id=SECOND_UUID,
    )

    jana_conv = await db.insert_conversation_entry(
        ConversationEntry(
            entry_date=date(2030, 1, 3),
            entry_type="note",
            title="Jana note",
            content="Jana private content",
            participant="claude.ai",
        ),
        patient_id=ERIKA_UUID,
    )
    bob_conv = await db.insert_conversation_entry(
        ConversationEntry(
            entry_date=date(2030, 1, 4),
            entry_type="note",
            title="Bob note",
            content="Bob private content",
            participant="claude.ai",
        ),
        patient_id=SECOND_UUID,
    )

    jana_rec = await db.insert_clinical_record(
        ClinicalRecord(
            patient_id=ERIKA_UUID,
            record_type="lab",
            occurred_at="2031-06-17T08:30:00Z",
            param="CEA",
            value_num=3.2,
            source="manual",
        )
    )
    bob_rec = await db.insert_clinical_record(
        ClinicalRecord(
            patient_id=SECOND_UUID,
            record_type="lab",
            occurred_at="2031-06-18T08:30:00Z",
            param="CEA",
            value_num=4.1,
            source="manual",
        )
    )

    return {
        "jana_doc": jana_doc.id,
        "bob_doc": bob_doc.id,
        "jana_event": jana_event.id,
        "bob_event": bob_event.id,
        "jana_conv": jana_conv.id,
        "bob_conv": bob_conv.id,
        "jana_rec": jana_rec.id,
        "bob_rec": bob_rec.id,
    }


# ── #499 / #516: get_document ─────────────────────────────────────────────


async def test_get_document_blocks_foreign_patient(db: Database):
    ids = await _seed_two_patients(db)
    # Jana scope sees Jana's doc.
    assert (await db.get_document(ids["jana_doc"], patient_id=ERIKA_UUID)) is not None
    # Same id, foreign scope: SQL filter drops the row.
    assert (await db.get_document(ids["jana_doc"], patient_id=SECOND_UUID)) is None
    # Bob's id under Jana's scope is also dropped.
    assert (await db.get_document(ids["bob_doc"], patient_id=ERIKA_UUID)) is None


# ── #516: get_documents_by_ids ────────────────────────────────────────────


async def test_get_documents_by_ids_filters_to_owner(db: Database):
    ids = await _seed_two_patients(db)
    mixed = {ids["jana_doc"], ids["bob_doc"]}

    jana_view = await db.get_documents_by_ids(mixed, patient_id=ERIKA_UUID)
    assert set(jana_view.keys()) == {ids["jana_doc"]}

    bob_view = await db.get_documents_by_ids(mixed, patient_id=SECOND_UUID)
    assert set(bob_view.keys()) == {ids["bob_doc"]}


# ── #517: get_cross_references ────────────────────────────────────────────


async def test_get_cross_references_filters_both_ends(db: Database):
    ids = await _seed_two_patients(db)
    # Plant a malformed cross-reference linking Jana's doc → Bob's doc. This
    # is the exact "stray relationship row" #517 calls out: a join-and-filter
    # query must drop it so a caregiver scoped to Jana can never see (or
    # follow) the cross-patient edge.
    await db.insert_cross_reference(
        ids["jana_doc"], ids["bob_doc"], relationship="related", confidence=0.9
    )
    # Plus a legitimate Jana-only cross-ref for the negative control.
    jana_doc2 = await db.insert_document(
        Document(
            file_id="jana-doc2-file",
            filename="jana2.pdf",
            original_filename="jana2.pdf",
            mime_type="application/pdf",
            size_bytes=512,
            category=DocumentCategory.IMAGING,
        ),
        patient_id=ERIKA_UUID,
    )
    await db.insert_cross_reference(
        ids["jana_doc"], jana_doc2.id, relationship="same_visit", confidence=0.8
    )

    jana_refs = await db.get_cross_references(ids["jana_doc"], patient_id=ERIKA_UUID)
    # Only the Jana→Jana edge survives the join filter.
    assert len(jana_refs) == 1
    assert jana_refs[0]["target_document_id"] == jana_doc2.id

    # From Bob's scope, the Jana→Bob edge is also dropped (Bob does not own
    # the source side either) — defence-in-depth.
    bob_refs = await db.get_cross_references(ids["jana_doc"], patient_id=SECOND_UUID)
    assert bob_refs == []


# ── #499 / #514: treatment events ─────────────────────────────────────────


async def test_get_treatment_event_blocks_foreign_patient(db: Database):
    ids = await _seed_two_patients(db)
    assert (await db.get_treatment_event(ids["bob_event"], patient_id=ERIKA_UUID)) is None
    assert (await db.get_treatment_event(ids["bob_event"], patient_id=SECOND_UUID)) is not None


async def test_delete_treatment_event_blocks_foreign_patient(db: Database):
    ids = await _seed_two_patients(db)
    # Jana scope cannot delete Bob's row — DELETE WHERE id=? AND patient_id=?
    # matches zero rows → returns False.
    deleted = await db.delete_treatment_event(ids["bob_event"], patient_id=ERIKA_UUID)
    assert deleted is False
    # Bob's row is still there.
    assert (await db.get_treatment_event(ids["bob_event"], patient_id=SECOND_UUID)) is not None
    # Bob can delete his own.
    deleted = await db.delete_treatment_event(ids["bob_event"], patient_id=SECOND_UUID)
    assert deleted is True


async def test_update_treatment_event_blocks_foreign_patient(db: Database):
    ids = await _seed_two_patients(db)
    # Jana scope cannot mutate Bob's row.
    updated = await db.update_treatment_event(
        ids["bob_event"], patient_id=ERIKA_UUID, title="HACKED"
    )
    assert updated is None
    # Bob's title is unchanged.
    bob = await db.get_treatment_event(ids["bob_event"], patient_id=SECOND_UUID)
    assert bob is not None and bob.title == "Bob C1"


# ── #499 / #515: conversation entries ─────────────────────────────────────


async def test_get_conversation_entry_blocks_foreign_patient(db: Database):
    ids = await _seed_two_patients(db)
    assert (await db.get_conversation_entry(ids["bob_conv"], patient_id=ERIKA_UUID)) is None
    assert (await db.get_conversation_entry(ids["bob_conv"], patient_id=SECOND_UUID)) is not None


async def test_delete_conversation_entry_blocks_foreign_patient(db: Database):
    ids = await _seed_two_patients(db)
    deleted = await db.delete_conversation_entry(ids["bob_conv"], patient_id=ERIKA_UUID)
    assert deleted is False
    assert (await db.get_conversation_entry(ids["bob_conv"], patient_id=SECOND_UUID)) is not None
    deleted = await db.delete_conversation_entry(ids["bob_conv"], patient_id=SECOND_UUID)
    assert deleted is True


# ── #499: clinical_records ────────────────────────────────────────────────


async def test_get_clinical_record_blocks_foreign_patient(db: Database):
    ids = await _seed_two_patients(db)
    assert (await db.get_clinical_record(ids["bob_rec"], patient_id=ERIKA_UUID)) is None
    assert (await db.get_clinical_record(ids["bob_rec"], patient_id=SECOND_UUID)) is not None


async def test_update_clinical_record_blocks_foreign_patient(db: Database):
    ids = await _seed_two_patients(db)
    after = await db.update_clinical_record(
        ids["bob_rec"],
        {"value_num": 99.9},
        patient_id=ERIKA_UUID,
        source="test-foreign",
        changed_by="jana",
    )
    assert after is None
    bob = await db.get_clinical_record(ids["bob_rec"], patient_id=SECOND_UUID)
    assert bob is not None and bob.value_num == 4.1


async def test_delete_and_restore_clinical_record_block_foreign_patient(
    db: Database,
):
    ids = await _seed_two_patients(db)

    did = await db.delete_clinical_record(
        ids["bob_rec"], patient_id=ERIKA_UUID, source="test-foreign"
    )
    assert did is False
    bob = await db.get_clinical_record(ids["bob_rec"], patient_id=SECOND_UUID)
    assert bob is not None and bob.deleted_at is None

    # Bob can soft-delete and restore his own row.
    assert (
        await db.delete_clinical_record(ids["bob_rec"], patient_id=SECOND_UUID, source="test-self")
        is True
    )
    # Foreign restore is also denied.
    restored = await db.restore_clinical_record(
        ids["bob_rec"], patient_id=ERIKA_UUID, source="test-foreign"
    )
    assert restored is None
    # Bob restores his own.
    restored = await db.restore_clinical_record(
        ids["bob_rec"], patient_id=SECOND_UUID, source="test-self"
    )
    assert restored is not None and restored.deleted_at is None


# ── Required keyword sanity ───────────────────────────────────────────────


async def test_helpers_require_patient_id_keyword(db: Database):
    """Calling these without ``patient_id=`` is now a TypeError — the static
    type checker, the test suite, and the runtime all reject the unscoped
    form so a future caller cannot silently regress to the pre-#499 shape."""
    ids = await _seed_two_patients(db)

    with pytest.raises(TypeError):
        await db.get_document(ids["jana_doc"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await db.get_treatment_event(ids["jana_event"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await db.get_conversation_entry(ids["jana_conv"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await db.get_clinical_record(ids["jana_rec"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await db.delete_treatment_event(ids["jana_event"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await db.delete_conversation_entry(ids["jana_conv"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await db.get_documents_by_ids({ids["jana_doc"]})  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await db.get_cross_references(ids["jana_doc"])  # type: ignore[call-arg]


# ── #533: email_entries / calendar_entries ────────────────────────────────


async def _seed_mail_and_calendar(db: Database) -> dict:
    """One email and one calendar entry per patient (invented values)."""
    from datetime import datetime

    from oncofiles.models import CalendarEntry, EmailEntry

    await _seed_two_patients(db)
    ids: dict = {}
    for who, pid, day in (("jana", ERIKA_UUID, 5), ("bob", SECOND_UUID, 6)):
        email = await db.upsert_email_entry(
            EmailEntry(
                patient_id=pid,
                gmail_message_id=f"{who}-msg-533",
                thread_id=f"{who}-th-533",
                subject=f"{who} invented subject",
                sender="sender@example.com",
                date=datetime(2031, 7, day, hour=11, minute=20),
                body_snippet=f"{who} snippet",
                body_text=f"{who} body",
            )
        )
        event = await db.upsert_calendar_entry(
            CalendarEntry(
                patient_id=pid,
                google_event_id=f"{who}-evt-533",
                summary=f"{who} invented visit",
                start_time=datetime(2031, 7, day, hour=16, minute=40),
            )
        )
        ids[f"{who}_email"] = email.id
        ids[f"{who}_event"] = event.id
    return ids


async def test_get_email_entry_filters_by_patient_id(db: Database):
    ids = await _seed_mail_and_calendar(db)

    own = await db.get_email_entry(ids["bob_email"], patient_id=SECOND_UUID)
    assert own is not None and own.patient_id == SECOND_UUID
    assert await db.get_email_entry(ids["bob_email"], patient_id=ERIKA_UUID) is None
    assert await db.get_email_entry(ids["jana_email"], patient_id=SECOND_UUID) is None


async def test_get_calendar_entry_filters_by_patient_id(db: Database):
    ids = await _seed_mail_and_calendar(db)

    own = await db.get_calendar_entry(ids["bob_event"], patient_id=SECOND_UUID)
    assert own is not None and own.patient_id == SECOND_UUID
    assert await db.get_calendar_entry(ids["bob_event"], patient_id=ERIKA_UUID) is None
    assert await db.get_calendar_entry(ids["jana_event"], patient_id=SECOND_UUID) is None


async def test_upsert_returns_the_row_of_its_own_patient(db: Database):
    """The upsert re-reads through the scoped helper: the same external id
    under two patients stays two rows, each returned to its own patient."""
    from datetime import datetime

    from oncofiles.models import CalendarEntry, EmailEntry

    await _seed_two_patients(db)
    saved = {}
    for pid in (ERIKA_UUID, SECOND_UUID):
        email = await db.upsert_email_entry(
            EmailEntry(
                patient_id=pid,
                gmail_message_id="shared-msg-533",
                thread_id="shared-th-533",
                subject="invented",
                sender="sender@example.com",
                date=datetime(2031, 8, 21, hour=11, minute=20),
            )
        )
        event = await db.upsert_calendar_entry(
            CalendarEntry(
                patient_id=pid,
                google_event_id="shared-evt-533",
                summary="invented",
                start_time=datetime(2031, 8, 21, hour=16, minute=40),
            )
        )
        assert email.patient_id == pid and event.patient_id == pid
        saved[pid] = (email.id, event.id)
    assert saved[ERIKA_UUID][0] != saved[SECOND_UUID][0]
    assert saved[ERIKA_UUID][1] != saved[SECOND_UUID][1]


async def test_mail_and_calendar_helpers_require_patient_id_keyword(db: Database):
    """No unscoped form: a missing or positional patient is a TypeError."""
    ids = await _seed_mail_and_calendar(db)

    with pytest.raises(TypeError):
        await db.get_email_entry(ids["jana_email"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await db.get_email_entry(ids["jana_email"], ERIKA_UUID)  # type: ignore[misc]
    with pytest.raises(TypeError):
        await db.get_calendar_entry(ids["jana_event"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await db.get_calendar_entry(ids["jana_event"], ERIKA_UUID)  # type: ignore[misc]


def test_no_unscoped_id_statement_on_mail_or_calendar_tables():
    """Every statement in src/ that addresses an ``email_entries`` or
    ``calendar_entries`` row by its internal id also filters by patient."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
    by_id = re.compile(
        r"(?:FROM|UPDATE)\s+(?:email_entries|calendar_entries)\b[^;]*?\bWHERE\s+id\s*=\s*\?"
        r"(?P<rest>[^\"]*)",
        re.IGNORECASE | re.DOTALL,
    )
    offenders = []
    for path in sorted(src.rglob("*.py")):
        for match in by_id.finditer(path.read_text(encoding="utf-8")):
            if "patient_id = ?" not in match.group("rest"):
                offenders.append(f"{path.relative_to(src)}: {match.group(0)[:80]!r}")
    assert offenders == []
