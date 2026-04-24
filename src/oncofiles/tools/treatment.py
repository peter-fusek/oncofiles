"""Treatment event tools."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import TreatmentEvent, TreatmentEventQuery
from oncofiles.tools._helpers import (
    _clamp_limit,
    _get_db,
    _parse_date,
    _resolve_patient_id,
)


async def _get_event_owned(db, event_id: int, pid: str) -> TreatmentEvent | None:
    """Fetch a treatment event by ID with patient ownership check.

    Returns None if the event doesn't exist or belongs to a different patient.
    Prevents cross-patient data leaks via enumerable integer IDs (#429).
    """
    if not await db.check_treatment_event_ownership(event_id, pid):
        return None
    return await db.get_treatment_event(event_id)


async def add_treatment_event(
    ctx: Context,
    event_date: str,
    event_type: str,
    title: str,
    notes: str = "",
    metadata: str = "{}",
    patient_slug: str | None = None,
) -> str:
    """Record a treatment milestone (chemo cycle, surgery, scan result, etc.).

    Args:
        event_date: Date of the event (YYYY-MM-DD).
        event_type: Type of event (e.g. chemo, surgery, scan, consult, side_effect).
        title: Short title for the event.
        notes: Optional longer description or notes.
        metadata: Optional JSON string with extra structured data.
        patient_slug: Optional — explicit patient slug (#429).
    """
    try:
        parsed_event_date = _parse_date(event_date)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    event = TreatmentEvent(
        event_date=parsed_event_date,
        event_type=event_type,
        title=title,
        notes=notes,
        metadata=metadata,
    )
    saved = await db.insert_treatment_event(event, patient_id=pid)
    return json.dumps(
        {
            "id": saved.id,
            "event_date": saved.event_date.isoformat(),
            "event_type": saved.event_type,
            "title": saved.title,
        }
    )


async def list_treatment_events(
    ctx: Context,
    event_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    patient_slug: str | None = None,
) -> str:
    """List treatment events, optionally filtered by type and date range.

    Returns events in reverse chronological order.

    Args:
        event_type: Filter by event type (e.g. chemo, surgery).
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        limit: Maximum results to return.
        patient_slug: Optional — explicit patient slug (#429).
    """
    try:
        db = _get_db(ctx)
        pid = await _resolve_patient_id(patient_slug, ctx)
        query = TreatmentEventQuery(
            event_type=event_type,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            limit=_clamp_limit(limit),
        )
        events = await db.list_treatment_events(query, patient_id=pid)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    items = [
        {
            "id": e.id,
            "event_date": e.event_date.isoformat(),
            "event_type": e.event_type,
            "title": e.title,
            "notes": e.notes,
            "metadata": e.metadata,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]
    return json.dumps({"events": items, "total": len(items)})


async def get_treatment_event(ctx: Context, event_id: int, patient_slug: str | None = None) -> str:
    """Get full details of a treatment event by ID.

    Args:
        event_id: The treatment event ID.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    event = await _get_event_owned(db, event_id, pid)
    if not event:
        return json.dumps({"error": f"Treatment event not found: {event_id}"})
    return json.dumps(
        {
            "id": event.id,
            "event_date": event.event_date.isoformat(),
            "event_type": event.event_type,
            "title": event.title,
            "notes": event.notes,
            "metadata": event.metadata,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }
    )


async def delete_treatment_event(
    ctx: Context, event_id: int, patient_slug: str | None = None
) -> str:
    """Delete a treatment event by ID. Use for removing contaminated/test data.

    Args:
        event_id: The treatment event ID to delete.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    # Cross-patient block: confirm ownership before deletion (#429).
    if not await db.check_treatment_event_ownership(event_id, pid):
        return json.dumps({"error": f"Treatment event {event_id} not found"})
    deleted = await db.delete_treatment_event(event_id)
    if not deleted:
        return json.dumps({"error": f"Treatment event {event_id} not found"})
    return json.dumps({"deleted": True, "event_id": event_id})


async def update_treatment_event(
    ctx: Context,
    event_id: int,
    title: str | None = None,
    notes: str | None = None,
    metadata: str | None = None,
    patient_slug: str | None = None,
) -> str:
    """Update a treatment event's title, notes, or metadata.

    Args:
        event_id: The treatment event ID to update.
        title: New title (optional).
        notes: New notes (optional).
        metadata: New metadata JSON string (optional).
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    # Cross-patient block: confirm ownership before update (#429).
    if not await db.check_treatment_event_ownership(event_id, pid):
        return json.dumps({"error": f"Treatment event {event_id} not found"})
    updated = await db.update_treatment_event(event_id, title=title, notes=notes, metadata=metadata)
    if not updated:
        return json.dumps({"error": f"Treatment event {event_id} not found"})
    return json.dumps(
        {
            "id": updated.id,
            "event_date": updated.event_date.isoformat(),
            "event_type": updated.event_type,
            "title": updated.title,
        }
    )


def register(mcp):
    mcp.tool()(add_treatment_event)
    mcp.tool()(list_treatment_events)
    mcp.tool()(get_treatment_event)
    mcp.tool()(delete_treatment_event)
    mcp.tool()(update_treatment_event)
