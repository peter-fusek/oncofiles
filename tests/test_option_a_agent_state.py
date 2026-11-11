"""Tests for Option A (#429) `patient_slug` rollout on agent_state MCP tools.

Closes #452 — the set/get/list agent_state MCP tools previously dropped
patient_id on the floor (no kwarg plumbed to the DB layer), so every caller
converged on ``patient_id=''`` and agents running for different patients
silently overwrote each other's state.

These tests ride the ``db`` fixture (ERIKA_UUID scoped via middleware
ContextVar) but then call the tool with an explicit ``patient_slug`` —
proving that the resolved pid is driven by the slug, not the ContextVar.
That's the stateless-HTTP guarantee needed for Claude.ai / ChatGPT
connectors.

#721 (Oncoteam sunset): ``set_agent_state`` refuses new writes, so the
archive rows are seeded through the DB layer and only the read tools are
exercised for isolation.
"""

from __future__ import annotations

import json

import pytest

from oncofiles.database import Database
from oncofiles.models import AgentState
from oncofiles.tools import agent_state as ast
from tests.conftest import ERIKA_UUID

# These tests verify slug-routing across patients — admin scope per #497/#498.
pytestmark = pytest.mark.usefixtures("admin_scope")

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


async def _seed_state(db: Database, pid: str, key: str, value: str) -> None:
    """Seed an archive row directly (the MCP write tool is deprecated, #721)."""
    await db.set_agent_state(AgentState(agent_id="oncoteam", key=key, value=value, patient_id=pid))


class _StubCtx:
    """Minimal Context stub exposing request_context.lifespan_context['db']."""

    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


async def test_get_respects_patient_slug(db: Database):
    """Archive rows for slug A and slug B must not cross on read."""
    await _seed_second_patient(db)
    ctx = _StubCtx(db)

    await _seed_state(db, ERIKA_UUID, "last_briefing_date", '"2027-04-20"')
    await _seed_state(db, SECOND_UUID, "last_briefing_date", '"2027-04-24"')

    ra = json.loads(await ast.get_agent_state(ctx, key="last_briefing_date", patient_slug="q1b"))
    rb = json.loads(
        await ast.get_agent_state(ctx, key="last_briefing_date", patient_slug=SECOND_SLUG)
    )
    assert ra["value"] == '"2027-04-20"'
    assert rb["value"] == '"2027-04-24"'
    assert ra["patient_id"] == ERIKA_UUID
    assert rb["patient_id"] == SECOND_UUID


async def test_get_missing_key_is_isolated(db: Database):
    """A key stored for patient A must be NOT FOUND under patient B."""
    await _seed_second_patient(db)
    ctx = _StubCtx(db)

    await _seed_state(db, ERIKA_UUID, "only_for_erika", '"secret"')

    fetched = json.loads(
        await ast.get_agent_state(ctx, key="only_for_erika", patient_slug=SECOND_SLUG)
    )
    assert fetched["value"] is None
    assert fetched["patient_id"] == SECOND_UUID


async def test_list_agent_states_scoped_to_slug(db: Database):
    """list_agent_states under slug B must not see slug A's keys."""
    await _seed_second_patient(db)
    ctx = _StubCtx(db)

    await _seed_state(db, ERIKA_UUID, "erika_key_1", '"1"')
    await _seed_state(db, ERIKA_UUID, "erika_key_2", '"2"')
    await _seed_state(db, SECOND_UUID, "bob_key_1", '"x"')

    erika_states = json.loads(await ast.list_agent_states(ctx, patient_slug="q1b"))
    bob_states = json.loads(await ast.list_agent_states(ctx, patient_slug=SECOND_SLUG))

    erika_keys = {s["key"] for s in erika_states}
    bob_keys = {s["key"] for s in bob_states}

    assert erika_keys == {"erika_key_1", "erika_key_2"}
    assert bob_keys == {"bob_key_1"}
    # Every returned row must carry the correct patient_id
    assert all(s["patient_id"] == ERIKA_UUID for s in erika_states)
    assert all(s["patient_id"] == SECOND_UUID for s in bob_states)


async def test_same_key_under_two_patients_reads_back_distinct(db: Database):
    """#452 regression: the same (agent_id, key) under two patients is two rows."""
    await _seed_second_patient(db)
    ctx = _StubCtx(db)

    await _seed_state(db, ERIKA_UUID, "counter", "1")
    await _seed_state(db, SECOND_UUID, "counter", "2")

    erika = json.loads(await ast.get_agent_state(ctx, key="counter", patient_slug="q1b"))
    bob = json.loads(await ast.get_agent_state(ctx, key="counter", patient_slug=SECOND_SLUG))
    assert erika["value"] == "1"
    assert bob["value"] == "2"


async def test_unknown_slug_raises(db: Database):
    """Requesting a non-existent slug must fail cleanly — never silently
    fall through to some other patient's state."""
    ctx = _StubCtx(db)
    try:
        await ast.get_agent_state(ctx, key="k", patient_slug="no-such-patient")
    except ValueError as exc:
        assert "no-such-patient" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown slug")


async def test_no_slug_uses_middleware_context(db: Database):
    """Backward-compat: if patient_slug is omitted, fall back to the
    middleware-resolved ContextVar (ERIKA_UUID in this fixture)."""
    ctx = _StubCtx(db)
    await _seed_state(db, ERIKA_UUID, "implicit", '"ok"')

    fetched = json.loads(await ast.get_agent_state(ctx, key="implicit"))
    assert fetched["patient_id"] == ERIKA_UUID
    assert fetched["value"] == '"ok"'


async def test_set_agent_state_refuses_and_stores_nothing(db: Database):
    """#721: the write tool is deprecated — it refuses and writes no row."""
    ctx = _StubCtx(db)
    out = json.loads(await ast.set_agent_state(ctx, key="new", value='"x"', patient_slug="q1b"))
    assert out["error"] == "deprecated"
    assert out["stored"] is False
    assert await db.list_agent_states("oncoteam", patient_id=ERIKA_UUID) == []
