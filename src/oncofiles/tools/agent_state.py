"""Agent state persistence tools."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import AgentState
from oncofiles.tools._helpers import _get_db


async def set_agent_state(
    ctx: Context,
    key: str,
    value: str,
    agent_id: str = "oncoteam",
) -> str:
    """Set a persistent key-value pair for an agent.

    Upserts: creates the key if new, updates if it already exists.

    Args:
        key: State key name (e.g. "last_briefing_date", "treatment_protocol").
        value: JSON string value to store.
        agent_id: Agent identifier (default: oncoteam).
    """
    db = _get_db(ctx)
    state = AgentState(agent_id=agent_id, key=key, value=value)
    saved = await db.set_agent_state(state)
    return json.dumps(
        {
            "id": saved.id,
            "agent_id": saved.agent_id,
            "key": saved.key,
            "value": saved.value,
            "updated_at": saved.updated_at.isoformat() if saved.updated_at else None,
        }
    )


async def get_agent_state(
    ctx: Context,
    key: str,
    agent_id: str = "oncoteam",
) -> str:
    """Get a persistent state value by key.

    Returns {value: null} if the key does not exist.

    Args:
        key: State key name.
        agent_id: Agent identifier (default: oncoteam).
    """
    db = _get_db(ctx)
    state = await db.get_agent_state(key, agent_id)
    if not state:
        return json.dumps({"key": key, "agent_id": agent_id, "value": None})
    return json.dumps(
        {
            "id": state.id,
            "agent_id": state.agent_id,
            "key": state.key,
            "value": state.value,
            "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        }
    )


async def list_agent_states(
    ctx: Context,
    agent_id: str = "oncoteam",
    limit: int = 100,
) -> str:
    """List all persistent state keys for an agent.

    Args:
        agent_id: Agent identifier (default: oncoteam).
        limit: Maximum number of states to return (default 100, max 500).
    """
    db = _get_db(ctx)
    states = await db.list_agent_states(agent_id)
    capped = min(max(limit, 1), 500)
    return json.dumps(
        [
            {
                "key": s.key,
                "value": s.value,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in states[:capped]
        ]
    )


def register(mcp):
    mcp.tool()(set_agent_state)
    mcp.tool()(get_agent_state)
    mcp.tool()(list_agent_states)
