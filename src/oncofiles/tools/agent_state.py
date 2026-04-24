"""Agent state persistence tools.

All three tools are patient-scoped via Option A (#429): every call resolves
the acting patient via ``_resolve_patient_id(patient_slug, ctx)`` and passes
it through to the DB layer's ``patient_id=`` kwarg. Prior to #452 the MCP
tools dropped the patient_id on the floor and every caller converged on
``patient_id=''``, silently overwriting each other's state and making
per-patient isolation impossible.
"""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import AgentState
from oncofiles.tools._helpers import _get_db, _resolve_patient_id


async def set_agent_state(
    ctx: Context,
    key: str,
    value: str,
    agent_id: str = "oncoteam",
    patient_slug: str | None = None,
) -> str:
    """Set a persistent key-value pair for an agent, scoped to a patient.

    Upserts: creates the key if new, updates if it already exists. The
    UNIQUE constraint is ``(patient_id, agent_id, key)`` — writes for
    different patients never collide.

    Args:
        key: State key name (e.g. "last_briefing_date", "treatment_protocol").
        value: JSON string value to store.
        agent_id: Agent identifier (default: oncoteam).
        patient_slug: Optional — explicit patient slug (e.g. 'q1b'). Required
            in stateless HTTP flows (Claude.ai connector, ChatGPT) where
            select_patient() state does not survive across tool calls (#429).
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    state = AgentState(agent_id=agent_id, key=key, value=value, patient_id=pid)
    saved = await db.set_agent_state(state)
    return json.dumps(
        {
            "id": saved.id,
            "agent_id": saved.agent_id,
            "key": saved.key,
            "value": saved.value,
            "patient_id": saved.patient_id,
            "updated_at": saved.updated_at.isoformat() if saved.updated_at else None,
        }
    )


async def get_agent_state(
    ctx: Context,
    key: str,
    agent_id: str = "oncoteam",
    patient_slug: str | None = None,
) -> str:
    """Get a persistent state value by key, scoped to a patient.

    Returns ``{value: null}`` if the key does not exist for this patient.

    Args:
        key: State key name.
        agent_id: Agent identifier (default: oncoteam).
        patient_slug: Optional — explicit patient slug (#429).
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    state = await db.get_agent_state(key, agent_id, patient_id=pid)
    if not state:
        return json.dumps({"key": key, "agent_id": agent_id, "patient_id": pid, "value": None})
    return json.dumps(
        {
            "id": state.id,
            "agent_id": state.agent_id,
            "key": state.key,
            "value": state.value,
            "patient_id": state.patient_id,
            "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        }
    )


async def list_agent_states(
    ctx: Context,
    agent_id: str = "oncoteam",
    limit: int = 100,
    patient_slug: str | None = None,
) -> str:
    """List all persistent state keys for an agent, scoped to a patient.

    Args:
        agent_id: Agent identifier (default: oncoteam).
        limit: Maximum number of states to return (default 100, max 500).
        patient_slug: Optional — explicit patient slug (#429).
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    states = await db.list_agent_states(agent_id, patient_id=pid)
    capped = min(max(limit, 1), 500)
    return json.dumps(
        [
            {
                "key": s.key,
                "value": s.value,
                "patient_id": s.patient_id,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in states[:capped]
        ]
    )


def register(mcp):
    mcp.tool()(set_agent_state)
    mcp.tool()(get_agent_state)
    mcp.tool()(list_agent_states)
