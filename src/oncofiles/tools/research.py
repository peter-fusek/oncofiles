"""Research entry tools.

DEPRECATED (#721, Oncoteam sunset): ``add_research_entry`` refuses new writes;
the read tools stay as an archive until 2027-01-10. See ``_sunset.py``.
"""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import ResearchQuery
from oncofiles.tools._helpers import _get_db, _research_source_url, _resolve_patient_id
from oncofiles.tools._sunset import write_refused


async def add_research_entry(
    ctx: Context,
    source: str,
    external_id: str,
    title: str,
    summary: str = "",
    tags: str = "[]",
    raw_data: str = "",
    patient_slug: str | None = None,
) -> str:
    """[DEPRECATED — Oncoteam sunset (#721); removed after 2027-01-10] Refuses new writes.

    Research entries were written by Oncoteam's literature and trial agents.
    Oncoteam is retired, so this tool stores nothing and returns a
    ``deprecated`` error. Existing entries stay readable via search_research
    and list_research_entries.

    Args:
        source: Source name (ignored).
        external_id: External identifier (ignored).
        title: Title (ignored).
        summary: Summary (ignored).
        tags: Tags (ignored).
        raw_data: Raw data (ignored).
        patient_slug: Optional patient slug (ignored).
    """
    return write_refused("add_research_entry")


async def search_research(
    ctx: Context,
    text: str | None = None,
    source: str | None = None,
    limit: int = 20,
    patient_slug: str | None = None,
) -> str:
    """[DEPRECATED — Oncoteam sunset (#721); removed after 2027-01-10]
    Search saved research entries by text and/or source.

    Args:
        text: Search in title, summary, and tags.
        source: Filter by source (e.g. pubmed, clinicaltrials).
        limit: Maximum results to return.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    query = ResearchQuery(text=text, source=source, limit=limit)
    entries = await db.search_research_entries(query, patient_id=pid)
    items = [
        {
            "id": e.id,
            "source": e.source,
            "external_id": e.external_id,
            "title": e.title,
            "summary": e.summary[:500] + ("..." if len(e.summary) > 500 else ""),
            "tags": e.tags,
            "url": _research_source_url(e.source, e.external_id),
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return json.dumps({"entries": items, "total": len(items)})


async def list_research_entries(
    ctx: Context,
    source: str | None = None,
    limit: int = 50,
    patient_slug: str | None = None,
) -> str:
    """[DEPRECATED — Oncoteam sunset (#721); removed after 2027-01-10]
    List saved research entries, optionally filtered by source.

    Args:
        source: Filter by source (e.g. pubmed, clinicaltrials).
        limit: Maximum results to return.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    entries = await db.list_research_entries(source=source, limit=limit, patient_id=pid)
    items = [
        {
            "id": e.id,
            "source": e.source,
            "external_id": e.external_id,
            "title": e.title,
            "summary": e.summary[:200] + ("..." if len(e.summary) > 200 else ""),
            "tags": e.tags,
            "url": _research_source_url(e.source, e.external_id),
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return json.dumps({"entries": items, "total": len(items)})


def register(mcp):
    mcp.tool()(add_research_entry)
    mcp.tool()(search_research)
    mcp.tool()(list_research_entries)
