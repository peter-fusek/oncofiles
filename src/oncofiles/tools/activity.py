"""Activity log tools."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import ActivityLogEntry, ActivityLogQuery
from oncofiles.tools._helpers import _clamp_limit, _get_db, _parse_date


async def add_activity_log(
    ctx: Context,
    session_id: str,
    agent_id: str,
    tool_name: str,
    input_summary: str = "",
    output_summary: str = "",
    duration_ms: int | None = None,
    status: str = "ok",
    error_message: str | None = None,
    tags: str = "[]",
) -> str:
    """Log an agent tool call to the activity audit trail (append-only).

    Args:
        session_id: Session identifier.
        agent_id: Agent that made the call (e.g. oncoteam).
        tool_name: Name of the tool that was called.
        input_summary: Brief summary of the input parameters.
        output_summary: Brief summary of the output.
        duration_ms: How long the call took in milliseconds.
        status: Result status (ok, error, timeout).
        error_message: Error details if status is not ok.
        tags: JSON array of tags (e.g. '["research","pubmed"]').
    """
    db = _get_db(ctx)
    entry = ActivityLogEntry(
        session_id=session_id,
        agent_id=agent_id,
        tool_name=tool_name,
        input_summary=input_summary,
        output_summary=output_summary,
        duration_ms=duration_ms,
        status=status,
        error_message=error_message,
        tags=tags,
    )
    saved = await db.insert_activity_log(entry)
    return json.dumps({"id": saved.id, "status": saved.status})


async def search_activity_log(
    ctx: Context,
    session_id: str | None = None,
    agent_id: str | None = None,
    tool_name: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    text: str | None = None,
    limit: int = 50,
) -> str:
    """Search the activity log with filters.

    Args:
        session_id: Filter by session.
        agent_id: Filter by agent.
        tool_name: Filter by tool name.
        status: Filter by status (ok, error, timeout).
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        text: Search in input/output summaries.
        limit: Maximum results to return.
    """
    try:
        db = _get_db(ctx)
        query = ActivityLogQuery(
            session_id=session_id,
            agent_id=agent_id,
            tool_name=tool_name,
            status=status,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            text=text,
            limit=_clamp_limit(limit),
        )
        entries = await db.search_activity_log(query)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    items = [
        {
            "id": e.id,
            "session_id": e.session_id,
            "agent_id": e.agent_id,
            "tool_name": e.tool_name,
            "input_summary": e.input_summary,
            "output_summary": e.output_summary,
            "status": e.status,
            "duration_ms": e.duration_ms,
            "error_message": e.error_message,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return json.dumps({"entries": items, "total": len(items)})


async def get_activity_stats(
    ctx: Context,
    session_id: str | None = None,
    agent_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    """Get aggregated activity statistics by tool and status.

    Args:
        session_id: Filter by session.
        agent_id: Filter by agent.
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
    """
    try:
        db = _get_db(ctx)
        stats = await db.get_activity_stats(
            session_id=session_id,
            agent_id=agent_id,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
        )
    except ValueError as e:
        return json.dumps({"error": str(e)})
    total_calls = sum(s["count"] for s in stats)
    return json.dumps({"stats": stats, "total_calls": total_calls})


def register(mcp):
    mcp.tool()(add_activity_log)
    mcp.tool()(search_activity_log)
    mcp.tool()(get_activity_stats)
