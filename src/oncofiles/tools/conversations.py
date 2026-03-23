"""Conversation archive tools."""

from __future__ import annotations

import json
from datetime import date

from fastmcp import Context

from oncofiles.models import ConversationEntry, ConversationQuery
from oncofiles.tools._helpers import _clamp_limit, _get_db, _parse_date


async def log_conversation(
    ctx: Context,
    title: str,
    content: str,
    entry_date: str | None = None,
    entry_type: str = "note",
    tags: str | None = None,
    document_ids: str | None = None,
    participant: str = "claude.ai",
) -> str:
    """Save a diary entry to the conversation archive.

    Use this to log summaries, decisions, progress notes, questions,
    or any narrative content from conversations about the oncology journey.

    Args:
        title: Short title for the entry.
        content: Markdown body with the full entry text.
        entry_date: Date the entry is about (YYYY-MM-DD). Defaults to today.
        entry_type: Type of entry: summary, decision, progress, question, note.
        tags: Comma-separated tags (e.g. "chemo,FOLFOX,cycle-3").
        document_ids: Comma-separated document IDs referenced (e.g. "3,15").
        participant: Who created this: claude.ai, claude-code, oncoteam.
    """
    try:
        parsed_date = _parse_date(entry_date) or date.today()
    except ValueError as e:
        return json.dumps({"error": str(e)})

    db = _get_db(ctx)

    parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    try:
        parsed_doc_ids = (
            [int(d.strip()) for d in document_ids.split(",") if d.strip()] if document_ids else None
        )
    except ValueError:
        return json.dumps(
            {"error": f"Invalid document_ids: {document_ids!r}. Expected comma-separated integers."}
        )

    # Try to capture session_id from context
    session_id = getattr(ctx, "session_id", None)

    entry = ConversationEntry(
        entry_date=parsed_date,
        entry_type=entry_type,
        title=title,
        content=content,
        participant=participant,
        session_id=session_id,
        tags=parsed_tags,
        document_ids=parsed_doc_ids,
        source="live",
    )
    entry = await db.insert_conversation_entry(entry)
    return json.dumps(
        {
            "id": entry.id,
            "entry_date": entry.entry_date.isoformat(),
            "entry_type": entry.entry_type,
            "title": entry.title,
            "tags": entry.tags,
        }
    )


async def search_conversations(
    ctx: Context,
    text: str | None = None,
    entry_type: str | None = None,
    participant: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    tags: str | None = None,
    limit: int = 50,
) -> str:
    """Search the conversation archive by text, type, date, or tags.

    Returns entries with truncated content (500 chars). Use get_conversation
    for full text of a specific entry.

    Args:
        text: Full-text search query.
        entry_type: Filter by type: summary, decision, progress, question, note.
        participant: Filter by participant: claude.ai, claude-code, oncoteam.
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        tags: Comma-separated tags to filter by (all must match).
        limit: Maximum results to return.
    """
    from oncofiles.memory import acquire_query_slot, release_query_slot

    try:
        db = _get_db(ctx)
        parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        query = ConversationQuery(
            text=text,
            entry_type=entry_type,
            participant=participant,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            tags=parsed_tags,
            limit=_clamp_limit(limit),
        )
        await acquire_query_slot("search_conversations")
        try:
            entries = await db.search_conversation_entries(query)
        finally:
            release_query_slot()
    except ValueError as e:
        return json.dumps({"error": str(e)})
    items = [
        {
            "id": e.id,
            "entry_date": e.entry_date.isoformat(),
            "entry_type": e.entry_type,
            "title": e.title,
            "content": e.content[:500] + ("..." if len(e.content) > 500 else ""),
            "participant": e.participant,
            "tags": e.tags,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return json.dumps({"entries": items, "total": len(items)})


async def get_conversation(ctx: Context, entry_id: int) -> str:
    """Get the full content of a single conversation entry by ID.

    Args:
        entry_id: The conversation entry ID.
    """
    db = _get_db(ctx)
    entry = await db.get_conversation_entry(entry_id)
    if not entry:
        return json.dumps({"error": f"Conversation entry not found: {entry_id}"})
    return json.dumps(
        {
            "id": entry.id,
            "entry_date": entry.entry_date.isoformat(),
            "entry_type": entry.entry_type,
            "title": entry.title,
            "content": entry.content,
            "participant": entry.participant,
            "session_id": entry.session_id,
            "tags": entry.tags,
            "document_ids": entry.document_ids,
            "source": entry.source,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
    )


async def get_journey_timeline(
    ctx: Context,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200,
) -> str:
    """Get a unified chronological timeline merging documents and conversation entries.

    This is the complete view of the oncology journey — documents and diary entries
    interleaved by date. Useful for book writing, doctor sharing, or reviewing history.

    Args:
        date_from: Start date (YYYY-MM-DD).
        date_to: End date (YYYY-MM-DD).
        limit: Maximum items per type (default 200).
    """
    try:
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    db = _get_db(ctx)

    # Fetch documents
    doc_conditions: list[str] = []
    doc_params: list[str | int] = []
    if parsed_from:
        doc_conditions.append("document_date >= ?")
        doc_params.append(parsed_from.isoformat())
    if parsed_to:
        doc_conditions.append("document_date <= ?")
        doc_params.append(parsed_to.isoformat())
    doc_conditions.append("deleted_at IS NULL")
    doc_where = " AND ".join(doc_conditions)
    async with db.db.execute(
        f"SELECT * FROM documents WHERE {doc_where} ORDER BY document_date ASC LIMIT ?",
        (*doc_params, limit),
    ) as cursor:
        doc_rows = await cursor.fetchall()

    # Fetch conversation entries
    entries = await db.get_conversation_timeline(
        date_from=parsed_from, date_to=parsed_to, limit=limit
    )

    # Merge into unified timeline
    timeline: list[dict] = []
    for row in doc_rows:
        timeline.append(
            {
                "date": row["document_date"] or "",
                "type": "document",
                "subtype": row["category"],
                "title": row["filename"],
                "detail": f"{row['institution'] or 'unknown'} | {row['category']}",
                "id": row["id"],
            }
        )
    for e in entries:
        timeline.append(
            {
                "date": e.entry_date.isoformat(),
                "type": "conversation",
                "subtype": e.entry_type,
                "title": e.title,
                "detail": e.content[:200] + ("..." if len(e.content) > 200 else ""),
                "id": e.id,
            }
        )

    # Sort chronologically
    timeline.sort(key=lambda x: x["date"])
    return json.dumps(timeline)


def register(mcp):
    mcp.tool()(log_conversation)
    mcp.tool()(search_conversations)
    mcp.tool()(get_conversation)
    mcp.tool()(get_journey_timeline)
