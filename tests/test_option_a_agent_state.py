"""Tests for Option A (#429) `patient_slug` rollout on agent_state MCP tools.

Closes #452 — the set/get/list agent_state MCP tools previously dropped
patient_id on the floor (no kwarg plumbed to the DB layer), so every caller
converged on ``patient_id=''`` and Oncoteam agents running for different
patients silently overwrote each other's state.

These tests ride the ``db`` fixture (ERIKA_UUID scoped via middleware
ContextVar) but then call the tool with an explicit ``patient_slug`` —
proving that the resolved pid is driven by the slug, not the ContextVar.
That's the stateless-HTTP guarantee needed for Claude.ai / ChatGPT
connectors.
"""

from __future__ import annotations

import json

from oncofiles.database import Database
from oncofiles.tools import agent_state as ast
from tests.conftest import ERIKA_UUID

SECOND_UUID = "00000000-0000-4000-8000-000000000002"
SECOND_SLUG = "bob-test"


async def _seed_second_patient(db: Database) -> None:
    """Insert a second patient (fixture already seeds ERIKA_UUID/q1b)."""
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (SECOND_UUID, SECOND_SLUG, "Bob Test", "bob@example.com"),
    )
    await db.db.commit()


class _StubCtx:
    """Minimal Context stub exposing request_context.lifespan_context['db']."""

    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


async def test_set_then_get_respects_patient_slug(db: Database):
    """Writing under slug A and reading under slug B must not cross."""
    await _seed_second_patient(db)
    ctx = _StubCtx(db)

    # Write the same key under two different patients
    a = json.loads(
        await ast.set_agent_state(
            ctx, key="last_briefing_date", value='"2026-04-20"', patient_slug="q1b"
        )
    )
    b = json.loads(
        await ast.set_agent_state(
            ctx, key="last_briefing_date", value='"2026-04-24"', patient_slug=SECOND_SLUG
        )
    )

    assert a["patient_id"] == ERIKA_UUID
    assert b["patient_id"] == SECOND_UUID

    # Read back under each patient — must be isolated
    ra = json.loads(await ast.get_agent_state(ctx, key="last_briefing_date", patient_slug="q1b"))
    rb = json.loads(
        await ast.get_agent_state(ctx, key="last_briefing_date", patient_slug=SECOND_SLUG)
    )
    assert ra["value"] == '"2026-04-20"'
    assert rb["value"] == '"2026-04-24"'
    assert ra["patient_id"] == ERIKA_UUID
    assert rb["patient_id"] == SECOND_UUID


async def test_get_missing_key_is_isolated(db: Database):
    """A key written for patient A must be NOT FOUND under patient B."""
    await _seed_second_patient(db)
    ctx = _StubCtx(db)

    await ast.set_agent_state(ctx, key="only_for_erika", value='"secret"', patient_slug="q1b")

    fetched = json.loads(
        await ast.get_agent_state(ctx, key="only_for_erika", patient_slug=SECOND_SLUG)
    )
    assert fetched["value"] is None
    assert fetched["patient_id"] == SECOND_UUID


async def test_list_agent_states_scoped_to_slug(db: Database):
    """list_agent_states under slug B must not see slug A's keys."""
    await _seed_second_patient(db)
    ctx = _StubCtx(db)

    await ast.set_agent_state(ctx, key="erika_key_1", value='"1"', patient_slug="q1b")
    await ast.set_agent_state(ctx, key="erika_key_2", value='"2"', patient_slug="q1b")
    await ast.set_agent_state(ctx, key="bob_key_1", value='"x"', patient_slug=SECOND_SLUG)

    erika_states = json.loads(await ast.list_agent_states(ctx, patient_slug="q1b"))
    bob_states = json.loads(await ast.list_agent_states(ctx, patient_slug=SECOND_SLUG))

    erika_keys = {s["key"] for s in erika_states}
    bob_keys = {s["key"] for s in bob_states}

    assert erika_keys == {"erika_key_1", "erika_key_2"}
    assert bob_keys == {"bob_key_1"}
    # Every returned row must carry the correct patient_id
    assert all(s["patient_id"] == ERIKA_UUID for s in erika_states)
    assert all(s["patient_id"] == SECOND_UUID for s in bob_states)


async def test_upsert_under_different_slugs_does_not_overwrite(db: Database):
    """The #452 regression test: writing the same (agent_id, key) under two
    different slugs must produce two distinct rows, not one overwritten row."""
    await _seed_second_patient(db)
    ctx = _StubCtx(db)

    await ast.set_agent_state(ctx, key="counter", value="1", patient_slug="q1b")
    await ast.set_agent_state(ctx, key="counter", value="2", patient_slug=SECOND_SLUG)
    # Pre-#452 fix: this second write would have overwritten the first
    # (both stored with patient_id='') and the assertion below would fail.

    erika = json.loads(await ast.get_agent_state(ctx, key="counter", patient_slug="q1b"))
    bob = json.loads(await ast.get_agent_state(ctx, key="counter", patient_slug=SECOND_SLUG))
    assert erika["value"] == "1"
    assert bob["value"] == "2"


async def test_unknown_slug_raises(db: Database):
    """Requesting a non-existent slug must fail cleanly — never silently
    fall through to some other patient's state."""
    ctx = _StubCtx(db)
    try:
        await ast.set_agent_state(ctx, key="k", value="v", patient_slug="no-such-patient")
    except ValueError as exc:
        assert "no-such-patient" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown slug")


async def test_no_slug_uses_middleware_context(db: Database):
    """Backward-compat: if patient_slug is omitted, fall back to the
    middleware-resolved ContextVar (ERIKA_UUID in this fixture)."""
    ctx = _StubCtx(db)

    saved = json.loads(await ast.set_agent_state(ctx, key="implicit", value='"ok"'))
    assert saved["patient_id"] == ERIKA_UUID

    fetched = json.loads(await ast.get_agent_state(ctx, key="implicit"))
    assert fetched["patient_id"] == ERIKA_UUID
    assert fetched["value"] == '"ok"'
