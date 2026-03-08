"""Research entry tools."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import ResearchEntry, ResearchQuery
from oncofiles.tools._helpers import _get_db


async def add_research_entry(
    ctx: Context,
    source: str,
    external_id: str,
    title: str,
    summary: str = "",
    tags: str = "[]",
    raw_data: str = "",
) -> str:
    """Save a research article or clinical trial found by an agent.

    Deduplicates by source+external_id — if a duplicate is found, returns
    the existing entry without error.

    Args:
        source: Source name (e.g. pubmed, clinicaltrials).
        external_id: External identifier (e.g. PMID, NCT number).
        title: Article or trial title.
        summary: Brief summary or abstract excerpt.
        tags: JSON array of tags (e.g. '["FOLFOX","mCRC"]').
        raw_data: Full raw data (abstract, JSON, etc.) for reference.
    """
    db = _get_db(ctx)
    entry = ResearchEntry(
        source=source,
        external_id=external_id,
        title=title,
        summary=summary,
        tags=tags,
        raw_data=raw_data,
    )
    saved = await db.insert_research_entry(entry)
    return json.dumps(
        {
            "id": saved.id,
            "source": saved.source,
            "external_id": saved.external_id,
            "title": saved.title,
        }
    )


async def search_research(
    ctx: Context,
    text: str | None = None,
    source: str | None = None,
    limit: int = 20,
) -> str:
    """Search saved research entries by text and/or source.

    Args:
        text: Search in title, summary, and tags.
        source: Filter by source (e.g. pubmed, clinicaltrials).
        limit: Maximum results to return.
    """
    db = _get_db(ctx)
    query = ResearchQuery(text=text, source=source, limit=limit)
    entries = await db.search_research_entries(query)
    items = [
        {
            "id": e.id,
            "source": e.source,
            "external_id": e.external_id,
            "title": e.title,
            "summary": e.summary[:500] + ("..." if len(e.summary) > 500 else ""),
            "tags": e.tags,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return json.dumps({"entries": items, "total": len(items)})


async def list_research_entries(
    ctx: Context,
    source: str | None = None,
    limit: int = 50,
) -> str:
    """List saved research entries, optionally filtered by source.

    Args:
        source: Filter by source (e.g. pubmed, clinicaltrials).
        limit: Maximum results to return.
    """
    db = _get_db(ctx)
    entries = await db.list_research_entries(source=source, limit=limit)
    items = [
        {
            "id": e.id,
            "source": e.source,
            "external_id": e.external_id,
            "title": e.title,
            "summary": e.summary[:200] + ("..." if len(e.summary) > 200 else ""),
            "tags": e.tags,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return json.dumps({"entries": items, "total": len(items)})


def register(mcp):
    mcp.tool()(add_research_entry)
    mcp.tool()(search_research)
    mcp.tool()(list_research_entries)
