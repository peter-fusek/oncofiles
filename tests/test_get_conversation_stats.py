"""Tests for get_conversation_stats MCP tool (#462)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from oncofiles.models import ConversationEntry
from oncofiles.tools.conversations import get_conversation_stats

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"


class _StubCtx:
    session_id = "stub-session"


async def _insert(db, *, entry_date, entry_type, title, tags=None, participant="claude.ai"):
    entry = ConversationEntry(
        entry_date=entry_date,
        entry_type=entry_type,
        title=title,
        content=f"body of {title}",
        participant=participant,
        tags=tags,
        source="live",
    )
    return await db.insert_conversation_entry(entry, patient_id=ERIKA_UUID)


async def _call(db, **kwargs):
    """Invoke the tool with helpers stubbed to return our test db + patient."""
    from oncofiles.tools import _helpers as helpers
    from oncofiles.tools import conversations as conv_mod

    orig_db = conv_mod._get_db
    orig_resolve = conv_mod._resolve_patient_id

    async def fake_resolve(_slug, _ctx):
        return ERIKA_UUID

    conv_mod._get_db = lambda _ctx: db
    conv_mod._resolve_patient_id = fake_resolve
    try:
        result = await get_conversation_stats(_StubCtx(), **kwargs)
    finally:
        conv_mod._get_db = orig_db
        conv_mod._resolve_patient_id = orig_resolve
        helpers._get_db = orig_db
        helpers._resolve_patient_id = orig_resolve
    return json.loads(result)


@pytest.mark.asyncio
async def test_stats_empty_patient(db):
    out = await _call(db)
    assert out["total"] == 0
    assert out["by_entry_type"] == {}
    assert out["by_month"] == []
    assert out["top_tags"] == []
    assert out["date_range"] == {"first": None, "last": None}


@pytest.mark.asyncio
async def test_stats_aggregates_type_participant_month(db):
    await _insert(db, entry_date=date(2026, 2, 1), entry_type="summary", title="a", tags=["chemo"])
    await _insert(
        db, entry_date=date(2026, 2, 15), entry_type="decision", title="b", tags=["chemo"]
    )
    await _insert(
        db,
        entry_date=date(2026, 4, 3),
        entry_type="note",
        title="c",
        tags=["FOLFOX", "cycle-3"],
        participant="oncoteam",
    )

    out = await _call(db)

    assert out["total"] == 3
    assert out["by_entry_type"]["summary"] == 1
    assert out["by_entry_type"]["decision"] == 1
    assert out["by_entry_type"]["note"] == 1
    assert out["by_participant"]["claude.ai"] == 2
    assert out["by_participant"]["oncoteam"] == 1

    months = {m["month"]: m["count"] for m in out["by_month"]}
    assert months == {"2026-02": 2, "2026-04": 1}
    assert out["date_range"] == {"first": "2026-02-01", "last": "2026-04-03"}


@pytest.mark.asyncio
async def test_stats_top_tags_orders_by_frequency(db):
    await _insert(db, entry_date=date(2026, 3, 1), entry_type="note", title="a", tags=["chemo"])
    await _insert(
        db, entry_date=date(2026, 3, 2), entry_type="note", title="b", tags=["chemo", "side-effect"]
    )
    await _insert(
        db,
        entry_date=date(2026, 3, 3),
        entry_type="note",
        title="c",
        tags=["chemo", "side-effect", "labs"],
    )

    out = await _call(db, top_tags_limit=5)

    tag_order = [t["tag"] for t in out["top_tags"]]
    # chemo=3, side-effect=2, labs=1 — order must be descending by count.
    assert tag_order[:3] == ["chemo", "side-effect", "labs"]
    assert dict((t["tag"], t["count"]) for t in out["top_tags"]) == {
        "chemo": 3,
        "side-effect": 2,
        "labs": 1,
    }


@pytest.mark.asyncio
async def test_stats_date_filters(db):
    await _insert(db, entry_date=date(2026, 1, 15), entry_type="note", title="jan")
    await _insert(db, entry_date=date(2026, 3, 15), entry_type="note", title="mar")
    await _insert(db, entry_date=date(2026, 5, 15), entry_type="note", title="may")

    out = await _call(db, date_from="2026-02-01", date_to="2026-04-30")

    assert out["total"] == 1
    assert {m["month"]: m["count"] for m in out["by_month"]} == {"2026-03": 1}
    assert out["date_range"] == {"first": "2026-03-15", "last": "2026-03-15"}
