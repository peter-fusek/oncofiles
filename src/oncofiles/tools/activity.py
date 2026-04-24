"""Activity log tools.

Patient-scoped by default (#484 sweep follow-up): every read-side tool here
filters by the caller's resolved patient_id so a patient-token caller cannot
enumerate cross-patient activity. Admin callers (static MCP_BEARER_TOKEN or
an OAuth email in DASHBOARD_ADMIN_EMAILS) see system-wide results.

Prior behavior — the `activity_log.patient_id` column existed (migration
029) but neither the tool nor the DB layer ever used it in WHERE, so any
authenticated caller could read every patient's tool-call history.
"""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import ActivityLogEntry, ActivityLogQuery
from oncofiles.tools._helpers import (
    _clamp_limit,
    _get_db,
    _is_admin_caller,
    _parse_date,
    _resolve_patient_id,
)


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
    patient_slug: str | None = None,
) -> str:
    """Log an agent tool call to the activity audit trail (append-only).

    The row's `patient_id` is set from the caller's resolved patient so
    read-side filtering actually scopes correctly (#484 follow-up). Admin
    callers without a bound patient write with empty patient_id.

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
        patient_slug: Optional explicit patient; defaults to caller's bound patient.
    """
    db = _get_db(ctx)
    caller_pid = await _resolve_patient_id(patient_slug, ctx, required=False)
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
        patient_id=caller_pid,
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
    patient_slug: str | None = None,
) -> str:
    """Search the activity log with filters, scoped to the caller's patient.

    Admin callers (static bearer or DASHBOARD_ADMIN_EMAILS) see system-wide
    by default. Patient-scoped callers see only their patient's rows —
    passing a different `patient_slug` resolves to that slug's pid and is
    still filtered by ownership downstream (an OAuth caller with email
    matching the target patient's caregiver_email can pass an explicit slug).

    Args:
        session_id: Filter by session.
        agent_id: Filter by agent.
        tool_name: Filter by tool name.
        status: Filter by status (ok, error, timeout).
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        text: Search in input/output summaries.
        limit: Maximum results to return.
        patient_slug: Optional explicit patient (non-admin callers may pass
            only their own slug; admin callers may pass any).
    """
    try:
        db = _get_db(ctx)
        caller_pid = await _resolve_patient_id(patient_slug, ctx, required=False)
        # Admin with no patient_slug → system-wide view (pid empty); admin
        # with patient_slug → scoped to that patient. Non-admin always
        # scoped to caller's own pid.
        scope_pid = caller_pid if (caller_pid or not _is_admin_caller()) else ""
        query = ActivityLogQuery(
            patient_id=scope_pid,
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
    patient_slug: str | None = None,
) -> str:
    """Get aggregated activity statistics by tool and status, patient-scoped.

    Admin callers see system-wide; non-admin scoped to their patient.
    Prior behaviour leaked cross-patient aggregate counts.

    Args:
        session_id: Filter by session.
        agent_id: Filter by agent.
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        patient_slug: Optional explicit patient.
    """
    try:
        db = _get_db(ctx)
        caller_pid = await _resolve_patient_id(patient_slug, ctx, required=False)
        scope_pid = caller_pid if (caller_pid or not _is_admin_caller()) else ""
        stats = await db.get_activity_stats(
            session_id=session_id,
            agent_id=agent_id,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            patient_id=scope_pid,
        )
    except ValueError as e:
        return json.dumps({"error": str(e)})
    total_calls = sum(s["count"] for s in stats)
    return json.dumps({"stats": stats, "total_calls": total_calls})


def register(mcp):
    mcp.tool()(add_activity_log)
    mcp.tool()(search_activity_log)
    mcp.tool()(get_activity_stats)
