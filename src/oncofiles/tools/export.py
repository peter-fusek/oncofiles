"""Document export package tool."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles import facts_timeline, patient_context
from oncofiles.config import DOC_QUERY_LIMIT
from oncofiles.tools._helpers import _gdrive_url, _get_db, _resolve_patient_id


async def export_document_package(
    ctx: Context,
    include_metadata: bool = True,
    include_timeline: bool = True,
    patient_slug: str | None = None,
) -> str:
    """Export a structured document package for consultations or second opinions.

    Assembles all documents grouped by category with metadata, a timeline and
    structured metadata. Returns JSON that an MCP client can render as PDF,
    email, or share link.

    ``treatment_timeline`` (oldest first) comes from the clinical facts model
    (the ``timeline`` tool's rows without documents: visits, diagnoses,
    treatments, procedures, medications, planned dates, abnormal values), each
    with its source document, page and verification. Only for a patient with
    no facts-model rows it lists the legacy treatment events instead;
    ``timeline_source`` says which (``facts`` | ``legacy``).

    Args:
        include_metadata: Include AI summaries and structured metadata (default True).
        include_timeline: Include the timeline (default True).
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)

    # Get all documents grouped by category
    docs = await db.list_documents(limit=DOC_QUERY_LIMIT, patient_id=pid)

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

    # The slug-resolved patient, never the request/default patient (#733):
    # with admin slug routing the request pid can be a different patient.
    await patient_context.ensure_loaded(db.db, pid)
    result: dict = {
        "patient": patient_context.get_context(pid),
        "total_documents": len(docs),
        "documents_by_category": by_category,
    }

    if include_timeline:
        if await facts_timeline.has_facts(db, pid):
            # facts model (#928): the timeline_v rows, oldest first like the
            # legacy list, in its shape plus provenance
            rows = await facts_timeline.fact_rows(db, pid)
            result["timeline_source"] = "facts"
            result["treatment_timeline"] = [
                {
                    "id": r["entity_id"],
                    "entity": r["entity"],
                    "event_date": str(r["at"] or "")[:10],
                    "event_type": r["kind"],
                    "title": r["title"] or "",
                    "notes": facts_timeline.summary_of(r),
                    "source_document_id": r["source_document_id"],
                    "page_number": r["page_number"],
                    "date_source": r["date_source"],
                    "verification": r["verification"],
                    "method": r["method"],
                    "verification_label": r["verification_label"],
                }
                for r in reversed(rows)
            ]
        else:
            # legacy fallback: only for a patient with no facts-model rows
            events = await db.get_treatment_events_timeline(patient_id=pid)
            result["timeline_source"] = "legacy"
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
