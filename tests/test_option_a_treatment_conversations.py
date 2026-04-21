"""Tests for Option A (#429) on treatment.py + conversations.py.

Covers the cross-patient ownership check added alongside the patient_slug
param — previously get/delete/update_treatment_event and get_conversation
fetched by id only, which let a caller with slug A read another patient's
record by guessing the id. The check_*_ownership DB helpers close that gap.
"""

from __future__ import annotations

import json
from datetime import date

from oncofiles.database import Database
from oncofiles.models import ConversationEntry, TreatmentEvent
from oncofiles.tools import conversations as conv_tools
from oncofiles.tools import treatment as tx_tools
from tests.conftest import ERIKA_UUID

SECOND_UUID = "00000000-0000-4000-8000-000000000002"
SECOND_SLUG = "bob-test"


async def _seed_two_patients(db: Database) -> tuple[int, int, int, int]:
    """Return (erika_event_id, bob_event_id, erika_conv_id, bob_conv_id)."""
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (SECOND_UUID, SECOND_SLUG, "Bob Test", "bob@example.com"),
    )
    await db.db.commit()

    erika_event = await db.insert_treatment_event(
        TreatmentEvent(event_date=date(2026, 1, 1), event_type="chemo", title="Erika C1"),
        patient_id=ERIKA_UUID,
    )
    bob_event = await db.insert_treatment_event(
        TreatmentEvent(event_date=date(2026, 1, 2), event_type="chemo", title="Bob C1"),
        patient_id=SECOND_UUID,
    )

    erika_conv = await db.insert_conversation_entry(
        ConversationEntry(
            entry_date=date(2026, 1, 3),
            entry_type="note",
            title="Erika note",
            content="Erika private content",
            participant="claude.ai",
        ),
        patient_id=ERIKA_UUID,
    )
    bob_conv = await db.insert_conversation_entry(
        ConversationEntry(
            entry_date=date(2026, 1, 4),
            entry_type="note",
            title="Bob note",
            content="Bob private content",
            participant="claude.ai",
        ),
        patient_id=SECOND_UUID,
    )
    return erika_event.id, bob_event.id, erika_conv.id, bob_conv.id


class _StubCtx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


# ── treatment_events ──────────────────────────────────────────────────────


async def test_treatment_event_ownership_helper(db: Database):
    await _seed_two_patients(db)
    # Pull out Erika's event id
    erika_events = await db.list_treatment_events(
        type("Q", (), {"event_type": None, "date_from": None, "date_to": None, "limit": 10})(),
        patient_id=ERIKA_UUID,
    )
    eid = erika_events[0].id
    assert await db.check_treatment_event_ownership(eid, ERIKA_UUID) is True
    assert await db.check_treatment_event_ownership(eid, SECOND_UUID) is False
    assert await db.check_treatment_event_ownership(999_999, ERIKA_UUID) is False


async def test_get_treatment_event_blocks_cross_patient(db: Database):
    e_id, b_id, _, _ = await _seed_two_patients(db)
    ctx = _StubCtx(db)

    # Default (Erika) scope: can read Erika's event, Bob's is hidden.
    ok = json.loads(await tx_tools.get_treatment_event(ctx, e_id))
    assert ok.get("title") == "Erika C1"

    blocked = json.loads(await tx_tools.get_treatment_event(ctx, b_id))
    assert "error" in blocked

    # Explicit slug — switch to Bob, now Bob's is visible.
    bob_ok = json.loads(await tx_tools.get_treatment_event(ctx, b_id, patient_slug=SECOND_SLUG))
    assert bob_ok.get("title") == "Bob C1"


async def test_delete_treatment_event_blocks_cross_patient(db: Database):
    e_id, b_id, _, _ = await _seed_two_patients(db)
    ctx = _StubCtx(db)

    # Erika tries to delete Bob's event — rejected, Bob's event still exists.
    result = json.loads(await tx_tools.delete_treatment_event(ctx, b_id))
    assert "error" in result
    assert await db.check_treatment_event_ownership(b_id, SECOND_UUID) is True

    # Erika deletes her own event — succeeds.
    result = json.loads(await tx_tools.delete_treatment_event(ctx, e_id))
    assert result.get("deleted") is True
    assert await db.check_treatment_event_ownership(e_id, ERIKA_UUID) is False


async def test_update_treatment_event_blocks_cross_patient(db: Database):
    e_id, b_id, _, _ = await _seed_two_patients(db)
    ctx = _StubCtx(db)

    # Erika tries to overwrite Bob's event title — rejected.
    result = json.loads(await tx_tools.update_treatment_event(ctx, b_id, title="HACKED"))
    assert "error" in result
    bob_event = await db.get_treatment_event(b_id)
    assert bob_event.title == "Bob C1"

    # Erika updates her own event — succeeds.
    result = json.loads(
        await tx_tools.update_treatment_event(ctx, e_id, title="Erika C1 (revised)")
    )
    assert result.get("title") == "Erika C1 (revised)"


async def test_add_treatment_event_respects_slug(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)

    # Add an event scoped to Bob via slug — default ContextVar is Erika.
    json.loads(
        await tx_tools.add_treatment_event(
            ctx,
            event_date="2026-02-15",
            event_type="scan",
            title="Bob scan",
            patient_slug=SECOND_SLUG,
        )
    )
    bob_events = await db.list_treatment_events(
        type("Q", (), {"event_type": None, "date_from": None, "date_to": None, "limit": 10})(),
        patient_id=SECOND_UUID,
    )
    titles = {e.title for e in bob_events}
    assert "Bob scan" in titles


# ── conversation_entries ──────────────────────────────────────────────────


async def test_conversation_entry_ownership_helper(db: Database):
    _, _, e_conv, b_conv = await _seed_two_patients(db)
    assert await db.check_conversation_entry_ownership(e_conv, ERIKA_UUID) is True
    assert await db.check_conversation_entry_ownership(e_conv, SECOND_UUID) is False
    assert await db.check_conversation_entry_ownership(999_999, ERIKA_UUID) is False


async def test_get_conversation_blocks_cross_patient(db: Database):
    _, _, e_conv, b_conv = await _seed_two_patients(db)
    ctx = _StubCtx(db)

    # Default (Erika) scope: Erika's entry visible, Bob's hidden.
    ok = json.loads(await conv_tools.get_conversation(ctx, e_conv))
    assert ok.get("content") == "Erika private content"

    blocked = json.loads(await conv_tools.get_conversation(ctx, b_conv))
    assert "error" in blocked
    # Critical: content body must NOT leak through any field
    assert "Bob private content" not in json.dumps(blocked)

    # Explicit slug → Bob's entry visible.
    bob_ok = json.loads(await conv_tools.get_conversation(ctx, b_conv, patient_slug=SECOND_SLUG))
    assert bob_ok.get("content") == "Bob private content"


async def test_log_conversation_respects_slug(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)

    # Log under Bob's slug despite default ContextVar = Erika
    result = json.loads(
        await conv_tools.log_conversation(
            ctx,
            title="Bob scoped note",
            content="body",
            patient_slug=SECOND_SLUG,
        )
    )
    assert result.get("title") == "Bob scoped note"

    # Verify DB row lives under Bob, not Erika
    ok = await db.check_conversation_entry_ownership(result["id"], SECOND_UUID)
    leak = await db.check_conversation_entry_ownership(result["id"], ERIKA_UUID)
    assert ok is True
    assert leak is False
