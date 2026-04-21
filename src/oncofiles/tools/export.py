"""Document export package tool."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.patient_context import get_context as _get_patient_context
from oncofiles.tools._helpers import _gdrive_url, _get_db, _resolve_patient_id


async def export_document_package(
    ctx: Context,
    include_metadata: bool = True,
    include_timeline: bool = True,
    patient_slug: str | None = None,
) -> str:
    """Export a structured document package for consultations or second opinions.

    Assembles all documents grouped by category with metadata, treatment
    events timeline, and structured metadata. Returns JSON that Oncoteam
    can render as PDF, email, or share link.

    Args:
        include_metadata: Include AI summaries and structured metadata (default True).
        include_timeline: Include treatment events timeline (default True).
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)

    # Get all documents grouped by category
    docs = await db.list_documents(limit=200, patient_id=pid)

    # Group by category
    by_category: dict[str, list[dict]] = {}
    for d in docs:
        cat = d.category.value
        if cat not in by_category:
            by_category[cat] = []
        entry = {
            "id": d.id,
            "file_id": d.file_id,
            "filename": d.filename,
            "document_date": d.document_date.isoformat() if d.document_date else None,
            "institution": d.institution,
            "description": d.description,
            "gdrive_url": _gdrive_url(d.gdrive_id),
        }
        if include_metadata:
            if d.ai_summary:
                entry["ai_summary"] = d.ai_summary
            if d.structured_metadata:
                entry["structured_metadata"] = json.loads(d.structured_metadata)
        by_category[cat].append(entry)

    result: dict = {
        "patient": _get_patient_context(),
        "total_documents": len(docs),
        "documents_by_category": by_category,
    }

    if include_timeline:
        events = await db.get_treatment_events_timeline(patient_id=pid)
        result["treatment_timeline"] = [
            {
                "id": e.id,
                "event_date": e.event_date.isoformat(),
                "event_type": e.event_type,
                "title": e.title,
                "notes": e.notes,
            }
            for e in events
        ]

    return json.dumps(result)


def register(mcp):
    mcp.tool()(export_document_package)
