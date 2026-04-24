"""Regression lock: activity_log cross-patient read scoping + backfill admin gate.

Closes the last Michal-sweep gap discovered during the "no misses" pass:
`search_activity_log` / `get_activity_stats` / `add_activity_log` previously
never used the `activity_log.patient_id` column (added migration 029).
Any caller could enumerate every patient's tool-call history.
`backfill_orphan_prompt_logs` was system-wide with no admin gate.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from oncofiles.database import Database
from oncofiles.models import ActivityLogEntry
from oncofiles.persistent_oauth import _verified_caller_is_admin
from tests.helpers import ERIKA_UUID


def _mock_ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    return ctx


async def _seed_activity(db, pid: str, count: int):
    for i in range(count):
        await db.insert_activity_log(
            ActivityLogEntry(
                session_id=f"sess_{pid[:4]}_{i}",
                agent_id="oncoteam",
                tool_name="search_pubmed",
                status="ok",
                patient_id=pid,
            )
        )


# ── search_activity_log ────────────────────────────────────────────


async def test_search_activity_log_filters_by_caller_pid(db: Database):
    """Non-admin caller with bound pid sees only their patient's entries."""
    from oncofiles.tools.activity import search_activity_log

    other_pid = "other-patient-00000000-0000-0000-0000"
    await _seed_activity(db, ERIKA_UUID, 3)
    await _seed_activity(db, other_pid, 5)

    _verified_caller_is_admin.set(False)
    # Patch the pid resolver to return caller's pid.
    from oncofiles import patient_middleware

    token = patient_middleware._current_patient_id.set(ERIKA_UUID)
    try:
        ctx = _mock_ctx(db)
        raw = await search_activity_log(ctx)
        result = json.loads(raw)
        # Only the 3 ERIKA entries should surface.
        assert result["total"] == 3
        for entry in result["entries"]:
            assert entry["session_id"].startswith("sess_00000000"[:9])
    finally:
        patient_middleware._current_patient_id.reset(token)


async def test_search_activity_log_admin_sees_all(db: Database):
    """Admin caller sees system-wide activity when no patient_slug passed."""
    from oncofiles.tools.activity import search_activity_log

    other_pid = "other-patient-00000000-0000-0000-0000"
    await _seed_activity(db, ERIKA_UUID, 3)
    await _seed_activity(db, other_pid, 5)

    _verified_caller_is_admin.set(True)
    from oncofiles import patient_middleware

    token = patient_middleware._current_patient_id.set("")
    try:
        ctx = _mock_ctx(db)
        raw = await search_activity_log(ctx)
        result = json.loads(raw)
        # Both patients' entries visible (8 total).
        assert result["total"] == 8
    finally:
        _verified_caller_is_admin.set(False)
        patient_middleware._current_patient_id.reset(token)


# ── get_activity_stats ──────────────────────────────────────────────


async def test_get_activity_stats_filters_by_caller_pid(db: Database):
    """Non-admin caller's stats only include their own patient's rows."""
    from oncofiles.tools.activity import get_activity_stats

    other_pid = "other-patient-00000000-0000-0000-0000"
    await _seed_activity(db, ERIKA_UUID, 2)
    await _seed_activity(db, other_pid, 4)

    _verified_caller_is_admin.set(False)
    from oncofiles import patient_middleware

    token = patient_middleware._current_patient_id.set(ERIKA_UUID)
    try:
        ctx = _mock_ctx(db)
        raw = await get_activity_stats(ctx)
        result = json.loads(raw)
        assert result["total_calls"] == 2  # not 6
    finally:
        patient_middleware._current_patient_id.reset(token)


# ── add_activity_log writes caller's pid ───────────────────────────


async def test_add_activity_log_sets_caller_patient_id(db: Database):
    """New entries carry the caller's pid so read-side filter works."""
    from oncofiles.tools.activity import add_activity_log

    _verified_caller_is_admin.set(False)
    from oncofiles import patient_middleware

    token = patient_middleware._current_patient_id.set(ERIKA_UUID)
    try:
        ctx = _mock_ctx(db)
        result = json.loads(
            await add_activity_log(
                ctx, session_id="s1", agent_id="oncoteam", tool_name="search_pubmed"
            )
        )
        # Fetch the inserted row and verify patient_id is set.
        entries = await db.search_activity_log(
            __import__("oncofiles.models", fromlist=["ActivityLogQuery"]).ActivityLogQuery(
                patient_id=ERIKA_UUID, limit=10
            )
        )
        assert any(e.session_id == "s1" for e in entries)
        for e in entries:
            if e.session_id == "s1":
                assert e.patient_id == ERIKA_UUID
    finally:
        patient_middleware._current_patient_id.reset(token)


# ── backfill_orphan_prompt_logs admin gate ─────────────────────────


async def test_search_activity_log_non_admin_no_pid_refuses_to_enumerate(db: Database):
    """Legacy-token regression: non-admin caller with empty caller_pid must
    NOT fall through to unfiltered (the #484 no-misses empirical retest
    caught this — get_activity_stats was returning 36,201 system-wide
    rows for my pre-#478 legacy-email-binding OAuth session).
    """
    from oncofiles.tools.activity import search_activity_log

    await _seed_activity(db, ERIKA_UUID, 2)
    _verified_caller_is_admin.set(False)
    from oncofiles import patient_middleware

    token = patient_middleware._current_patient_id.set("")
    try:
        ctx = _mock_ctx(db)
        raw = await search_activity_log(ctx)
        result = json.loads(raw)
        assert result["total"] == 0
        assert result["entries"] == []
    finally:
        patient_middleware._current_patient_id.reset(token)


async def test_get_activity_stats_non_admin_no_pid_refuses_to_enumerate(db: Database):
    """Same regression, stats endpoint. Returning {"stats": [], "total_calls": 0}
    is the safe default for legacy-token callers who have no bound patient.
    """
    from oncofiles.tools.activity import get_activity_stats

    await _seed_activity(db, ERIKA_UUID, 3)
    _verified_caller_is_admin.set(False)
    from oncofiles import patient_middleware

    token = patient_middleware._current_patient_id.set("")
    try:
        ctx = _mock_ctx(db)
        raw = await get_activity_stats(ctx)
        result = json.loads(raw)
        assert result["total_calls"] == 0
        assert result["stats"] == []
    finally:
        patient_middleware._current_patient_id.reset(token)


async def test_backfill_orphan_prompt_logs_rejects_non_admin(db: Database):
    from oncofiles.tools.prompt_log import backfill_orphan_prompt_logs

    _verified_caller_is_admin.set(False)
    ctx = _mock_ctx(db)
    result = json.loads(await backfill_orphan_prompt_logs(ctx))
    assert "error" in result
    assert "admin scope" in result["error"]


async def test_backfill_orphan_prompt_logs_allows_admin(db: Database):
    """Admin caller proceeds — tool should at least attempt the backfill."""
    from oncofiles.tools.prompt_log import backfill_orphan_prompt_logs

    _verified_caller_is_admin.set(True)
    ctx = _mock_ctx(db)
    try:
        result = json.loads(await backfill_orphan_prompt_logs(ctx, batch_size=10))
        # Don't care about specific values — just that admin-gate passes.
        assert "error" not in result or "admin" not in result["error"]
    finally:
        _verified_caller_is_admin.set(False)
