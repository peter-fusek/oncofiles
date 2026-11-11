"""Soft delete for conversation entries and treatment events (#723).

Deletion policy: nothing patient-owned is hard-deleted. ``delete_*`` sets
``deleted_at`` / ``deleted_by``, every reader hides the row, ``restore_*``
brings it back, and each action leaves an ``activity_log`` row (ids only).

The test at the bottom checks both tables against the repo's single
hard-DELETE allowlist in ``tests/test_no_automatic_deletion.py`` (#734).
"""

from __future__ import annotations

import json
from datetime import date

from oncofiles.database import Database
from oncofiles.manifest import export_manifest
from oncofiles.models import (
    ActivityLogQuery,
    ConversationEntry,
    ConversationQuery,
    TreatmentEvent,
    TreatmentEventQuery,
)
from oncofiles.tools import conversations as conv_tools
from oncofiles.tools import treatment as tx_tools
from tests.conftest import ERIKA_UUID

_YEAR = 2027  # synthetic dates only (CLAUDE.md fixture rule)


class _StubCtx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


async def _event(db: Database, title: str = "Invented cycle") -> TreatmentEvent:
    return await db.insert_treatment_event(
        TreatmentEvent(event_date=date(_YEAR, 5, 1), event_type="chemo", title=title),
        patient_id=ERIKA_UUID,
    )


async def _entry(db: Database, title: str = "Invented note") -> ConversationEntry:
    return await db.insert_conversation_entry(
        ConversationEntry(
            entry_date=date(_YEAR, 5, 2),
            entry_type="note",
            title=title,
            content="zebrafish placeholder content",
            participant="claude.ai",
            tags=["softdel"],
        ),
        patient_id=ERIKA_UUID,
    )


async def _activity(db: Database, tool_name: str) -> list:
    return await db.search_activity_log(
        ActivityLogQuery(patient_id=ERIKA_UUID, tool_name=tool_name, limit=50)
    )


# ── schema ───────────────────────────────────────────────────────────────


async def test_migration_adds_soft_delete_columns(db: Database):
    for table in ("conversation_entries", "treatment_events"):
        async with db.db.execute(f"PRAGMA table_info({table})") as cur:
            cols = {r["name"] for r in await cur.fetchall()}
        assert {"deleted_at", "deleted_by"} <= cols, table


# ── treatment events ─────────────────────────────────────────────────────


async def test_delete_treatment_event_is_soft(db: Database):
    ev = await _event(db)
    assert await db.delete_treatment_event(ev.id, patient_id=ERIKA_UUID, deleted_by="tester")

    # The row is still there, marked deleted.
    async with db.db.execute(
        "SELECT deleted_at, deleted_by FROM treatment_events WHERE id = ?", (ev.id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and row["deleted_at"] and row["deleted_by"] == "tester"

    # Hidden from every reader.
    assert await db.get_treatment_event(ev.id, patient_id=ERIKA_UUID) is None
    listed = await db.list_treatment_events(TreatmentEventQuery(), patient_id=ERIKA_UUID)
    assert ev.id not in {e.id for e in listed}
    timeline = await db.get_treatment_events_timeline(patient_id=ERIKA_UUID)
    assert ev.id not in {e.id for e in timeline}
    async with db.db.execute(
        "SELECT COUNT(*) AS c FROM timeline_v WHERE entity = 'treatment_event' AND entity_id = ?",
        (ev.id,),
    ) as cur:
        assert (await cur.fetchone())["c"] == 0

    # Still reachable explicitly, for review.
    trashed = await db.get_treatment_event(ev.id, patient_id=ERIKA_UUID, include_deleted=True)
    assert trashed is not None and trashed.deleted_at


async def test_delete_treatment_event_twice_returns_false(db: Database):
    ev = await _event(db)
    assert await db.delete_treatment_event(ev.id, patient_id=ERIKA_UUID)
    assert await db.delete_treatment_event(ev.id, patient_id=ERIKA_UUID) is False


async def test_update_does_not_touch_deleted_treatment_event(db: Database):
    ev = await _event(db)
    await db.delete_treatment_event(ev.id, patient_id=ERIKA_UUID)
    assert await db.update_treatment_event(ev.id, patient_id=ERIKA_UUID, title="changed") is None


async def test_restore_treatment_event(db: Database):
    ev = await _event(db)
    await db.delete_treatment_event(ev.id, patient_id=ERIKA_UUID, deleted_by="tester")
    restored = await db.restore_treatment_event(ev.id, patient_id=ERIKA_UUID, restored_by="tester")
    assert restored is not None and restored.deleted_at is None
    assert await db.get_treatment_event(ev.id, patient_id=ERIKA_UUID) is not None
    # Not in the trash any more → second restore is a no-op.
    assert await db.restore_treatment_event(ev.id, patient_id=ERIKA_UUID) is None


async def test_treatment_event_soft_delete_and_restore_are_audited(db: Database):
    ev = await _event(db, title="Secret title stays out of the log")
    await db.delete_treatment_event(ev.id, patient_id=ERIKA_UUID, deleted_by="tester")
    await db.restore_treatment_event(ev.id, patient_id=ERIKA_UUID, restored_by="tester")

    deletes = await _activity(db, "soft_delete:treatment_events")
    restores = await _activity(db, "restore:treatment_events")
    assert len(deletes) == 1 and len(restores) == 1
    assert deletes[0].input_summary == f"treatment_events id={ev.id}"
    assert deletes[0].agent_id == "tester"
    # Ids only — never row content.
    assert "Secret" not in json.dumps([d.model_dump(mode="json") for d in deletes + restores])


async def test_treatment_tools_soft_delete_and_restore(db: Database):
    ev = await _event(db)
    ctx = _StubCtx(db)

    deleted = json.loads(await tx_tools.delete_treatment_event(ctx, ev.id))
    assert deleted["deleted"] is True and "restore_treatment_event" in deleted["message"]
    assert "error" in json.loads(await tx_tools.get_treatment_event(ctx, ev.id))
    listed = json.loads(await tx_tools.list_treatment_events(ctx))
    assert ev.id not in {e["id"] for e in listed["events"]}

    restored = json.loads(await tx_tools.restore_treatment_event(ctx, ev.id))
    assert restored["restored"] is True
    assert json.loads(await tx_tools.get_treatment_event(ctx, ev.id))["id"] == ev.id
    again = json.loads(await tx_tools.restore_treatment_event(ctx, ev.id))
    assert "error" in again


# ── conversation entries ────────────────────────────────────────────────


async def test_delete_conversation_entry_is_soft(db: Database):
    entry = await _entry(db)
    assert await db.delete_conversation_entry(entry.id, patient_id=ERIKA_UUID, deleted_by="tester")

    async with db.db.execute(
        "SELECT deleted_at, deleted_by FROM conversation_entries WHERE id = ?", (entry.id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and row["deleted_at"] and row["deleted_by"] == "tester"

    assert await db.get_conversation_entry(entry.id, patient_id=ERIKA_UUID) is None
    found = await db.search_conversation_entries(
        ConversationQuery(text="zebrafish"), patient_id=ERIKA_UUID
    )
    assert found == []
    assert await db.get_conversation_timeline(patient_id=ERIKA_UUID) == []
    # Re-import dedupe still sees it, so a deleted entry is not resurrected.
    trashed = await db.get_conversation_entry(entry.id, patient_id=ERIKA_UUID, include_deleted=True)
    assert trashed is not None and trashed.deleted_by == "tester"


async def test_restore_conversation_entry_and_audit(db: Database):
    entry = await _entry(db)
    await db.delete_conversation_entry(entry.id, patient_id=ERIKA_UUID)
    assert await db.delete_conversation_entry(entry.id, patient_id=ERIKA_UUID) is False
    restored = await db.restore_conversation_entry(entry.id, patient_id=ERIKA_UUID)
    assert restored is not None and restored.title == "Invented note"
    assert await db.restore_conversation_entry(entry.id, patient_id=ERIKA_UUID) is None
    assert len(await _activity(db, "soft_delete:conversation_entries")) == 1
    assert len(await _activity(db, "restore:conversation_entries")) == 1


async def test_conversation_tools_hide_deleted(db: Database):
    keep = await _entry(db, title="Kept note")
    gone = await _entry(db, title="Gone note")
    await db.delete_conversation_entry(gone.id, patient_id=ERIKA_UUID)
    ctx = _StubCtx(db)

    assert "error" in json.loads(await conv_tools.get_conversation(ctx, gone.id))

    stats = json.loads(await conv_tools.get_conversation_stats(ctx))
    assert stats["total"] == 1
    assert {t["tag"]: t["count"] for t in stats["top_tags"]} == {"softdel": 1}

    journey = json.loads(await conv_tools.get_journey_timeline(ctx))
    ids = {(i["source_table"], i["id"]) for i in journey["items"]}
    assert ("conversations", keep.id) in ids
    assert ("conversations", gone.id) not in ids


async def test_journey_timeline_and_manifest_hide_deleted(db: Database):
    ev_keep = await _event(db, title="Kept cycle")
    ev_gone = await _event(db, title="Gone cycle")
    en_gone = await _entry(db, title="Gone note")
    await db.delete_treatment_event(ev_gone.id, patient_id=ERIKA_UUID)
    await db.delete_conversation_entry(en_gone.id, patient_id=ERIKA_UUID)

    journey = json.loads(await conv_tools.get_journey_timeline(_StubCtx(db)))
    te_ids = {i["id"] for i in journey["items"] if i["source_table"] == "treatment_events"}
    assert te_ids == {ev_keep.id}

    manifest = await export_manifest(db, patient_id=ERIKA_UUID)
    assert [e["title"] for e in manifest["treatment_events"]] == ["Kept cycle"]
    assert manifest["conversation_entries"] == []


# ── lock: no hard DELETE on patient data ─────────────────────────────────
# One allowlist for the whole repo: tests/test_no_automatic_deletion.py
# (SQL_DELETE_ALLOWLIST, #734). treatment_events and conversation_entries are
# in its NEVER_HARD_DELETED set.


def test_conversation_and_treatment_tables_never_hard_deleted():
    from tests.test_no_automatic_deletion import NEVER_HARD_DELETED, _sql_hard_deletes

    assert {"conversation_entries", "treatment_events"} <= NEVER_HARD_DELETED
    tables = {t for _, t in _sql_hard_deletes()}
    assert "conversation_entries" not in tables
    assert "treatment_events" not in tables
