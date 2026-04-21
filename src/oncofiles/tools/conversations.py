"""Conversation archive tools."""

from __future__ import annotations

import json
import logging
from datetime import date

from fastmcp import Context

from oncofiles.models import ConversationEntry, ConversationQuery
from oncofiles.tools._helpers import _clamp_limit, _get_db, _parse_date, _resolve_patient_id

logger = logging.getLogger(__name__)


def _row_get(row, key: str, default=""):
    """Get a field from a DB row (works with both dict and sqlite3.Row)."""
    try:
        val = row[key]
        return val if val is not None else default
    except (KeyError, IndexError):
        return default


def _safe_json_list(raw: str | None) -> list:
    """Parse a JSON string as a list, returning [] on any error."""
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


def _safe_json_field(raw: str | None, field: str) -> str:
    """Extract a string field from a JSON blob, returning "" on any error."""
    if not raw:
        return ""
    try:
        return json.loads(raw).get(field, "")
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return ""


async def log_conversation(
    ctx: Context,
    title: str,
    content: str,
    entry_date: str | None = None,
    entry_type: str = "note",
    tags: str | None = None,
    document_ids: str | None = None,
    participant: str = "claude.ai",
    patient_slug: str | None = None,
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
        patient_slug: Optional — explicit patient slug (#429).
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

    # Auto-classify session type based on participant
    session_type = "technical" if participant == "claude-code" else "patient"

    entry = ConversationEntry(
        entry_date=parsed_date,
        entry_type=entry_type,
        title=title,
        content=content,
        participant=participant,
        session_type=session_type,
        session_id=session_id,
        tags=parsed_tags,
        document_ids=parsed_doc_ids,
        source="live",
    )
    pid = await _resolve_patient_id(patient_slug, ctx)
    entry = await db.insert_conversation_entry(entry, patient_id=pid)
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
    patient_slug: str | None = None,
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
        patient_slug: Optional — explicit patient slug (#429).
    """
    from oncofiles.memory import query_slot

    try:
        db = _get_db(ctx)
        pid = await _resolve_patient_id(patient_slug, ctx)
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
        async with query_slot("search_conversations"):
            entries = await db.search_conversation_entries(query, patient_id=pid)
        from oncofiles.memory import update_peak_rss

        update_peak_rss()
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


async def get_conversation(ctx: Context, entry_id: int, patient_slug: str | None = None) -> str:
    """Get the full content of a single conversation entry by ID.

    Args:
        entry_id: The conversation entry ID.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    # Cross-patient block: confirm the entry belongs to the resolved patient.
    if not await db.check_conversation_entry_ownership(entry_id, pid):
        return json.dumps({"error": f"Conversation entry not found: {entry_id}"})
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
    offset: int = 0,
    source_tables: str | None = None,
    search: str | None = None,
    sort: str = "desc",
    patient_slug: str | None = None,
) -> str:
    """Get a unified chronological timeline of the complete patient journey.

    Merges documents, treatment events, conversations, lab values, and research
    entries into a single date-sorted stream. Each item includes a source_table
    field for namespaced IDs (e.g. doc:42, te:7).

    Args:
        date_from: Start date (YYYY-MM-DD).
        date_to: End date (YYYY-MM-DD).
        limit: Maximum total items returned (default 200, max 500).
        offset: Skip first N items for pagination.
        source_tables: Comma-separated list to include (e.g. "documents,treatment_events").
                       Default: all tables.
        search: Substring filter across title + summary + tags.
        sort: "desc" (newest first, default) or "asc" (oldest first).
        patient_slug: Optional — explicit patient slug (#429).
    """
    try:
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    db = _get_db(ctx)
    patient_id = await _resolve_patient_id(patient_slug, ctx)
    limit = min(limit, 500)

    # Which tables to include
    all_tables = {
        "documents",
        "treatment_events",
        "conversations",
        "lab_values",
        "research_entries",
    }
    if source_tables:
        include = {t.strip() for t in source_tables.split(",") if t.strip() in all_tables}
    else:
        include = all_tables

    timeline: list[dict] = []

    # ── Documents ──────────────────────────────────────────────────────────
    if "documents" in include:
        conds = ["deleted_at IS NULL", "patient_id = ?"]
        params: list[str | int] = [patient_id]
        if parsed_from:
            conds.append("document_date >= ?")
            params.append(parsed_from.isoformat())
        if parsed_to:
            conds.append("document_date <= ?")
            params.append(parsed_to.isoformat())
        where = " AND ".join(conds)
        sql = f"SELECT * FROM documents WHERE {where} ORDER BY document_date DESC LIMIT 500"
        async with db.db.execute(sql, tuple(params)) as cursor:
            for row in await cursor.fetchall():
                gdrive_url = ""
                if _row_get(row, "gdrive_id"):
                    gdrive_url = f"https://drive.google.com/file/d/{row['gdrive_id']}/view"
                timeline.append(
                    {
                        "id": row["id"],
                        "source_table": "documents",
                        "date": row["document_date"] or _row_get(row, "created_at", "")[:10] or "",
                        "event_type": "document",
                        "title": row["filename"],
                        "summary": (_row_get(row, "ai_summary") or "")[:200],
                        "category": row["category"],
                        "document_id": row["id"],
                        "gdrive_url": gdrive_url,
                        "tags": [],
                        "institution": _row_get(row, "institution") or "",
                    }
                )

    # ── Treatment events ──────────────────────────────────────────────────
    if "treatment_events" in include:
        try:
            conds = ["patient_id = ?"]
            params = [patient_id]
            if parsed_from:
                conds.append("event_date >= ?")
                params.append(parsed_from.isoformat())
            if parsed_to:
                conds.append("event_date <= ?")
                params.append(parsed_to.isoformat())
            where = " AND ".join(conds)
            sql = f"SELECT * FROM treatment_events WHERE {where} ORDER BY event_date DESC LIMIT 500"
            async with db.db.execute(sql, tuple(params)) as cursor:
                for row in await cursor.fetchall():
                    timeline.append(
                        {
                            "id": row["id"],
                            "source_table": "treatment_events",
                            "date": row["event_date"] or "",
                            "event_type": row["event_type"],
                            "title": row["title"],
                            "summary": (_row_get(row, "notes") or "")[:200],
                            "category": row["event_type"],
                            "document_id": None,
                            "gdrive_url": "",
                            "tags": [],
                        }
                    )
        except Exception:
            logger.debug("treatment_events query failed — table may not exist")

    # ── Conversations ─────────────────────────────────────────────────────
    if "conversations" in include:
        entries = await db.get_conversation_timeline(
            date_from=parsed_from,
            date_to=parsed_to,
            limit=500,
            patient_id=patient_id,
        )
        for e in entries:
            tags = _safe_json_list(e.tags)
            timeline.append(
                {
                    "id": e.id,
                    "source_table": "conversations",
                    "date": e.entry_date.isoformat(),
                    "event_type": e.entry_type,
                    "title": e.title,
                    "summary": e.content[:200] + ("..." if len(e.content) > 200 else ""),
                    "category": e.entry_type,
                    "document_id": None,
                    "gdrive_url": "",
                    "tags": tags,
                }
            )

    # ── Lab values (grouped by date+document) ─────────────────────────────
    if "lab_values" in include:
        try:
            conds = ["patient_id = ?"]
            params = [patient_id]
            if parsed_from:
                conds.append("lab_date >= ?")
                params.append(parsed_from.isoformat())
            if parsed_to:
                conds.append("lab_date <= ?")
                params.append(parsed_to.isoformat())
            where = " AND ".join(conds)
            sql = f"""SELECT lab_date, document_id, COUNT(*) as cnt,
                       GROUP_CONCAT(parameter, ', ') as params
                FROM lab_values WHERE {where}
                GROUP BY lab_date, document_id
                ORDER BY lab_date DESC LIMIT 500"""
            async with db.db.execute(sql, tuple(params)) as cursor:
                for row in await cursor.fetchall():
                    param_list = (_row_get(row, "params") or "")[:100]
                    timeline.append(
                        {
                            "id": row["document_id"] or 0,
                            "source_table": "lab_values",
                            "date": row["lab_date"] or "",
                            "event_type": "lab_result",
                            "title": f"Lab results ({row['cnt']} values)",
                            "summary": param_list,
                            "category": "labs",
                            "document_id": row["document_id"],
                            "gdrive_url": "",
                            "tags": [],
                        }
                    )
        except Exception:
            logger.debug("lab_values query failed — table may not exist")

    # ── Research entries ───────────────────────────────────────────────────
    if "research_entries" in include:
        try:
            conds = ["patient_id = ?"]
            params = [patient_id]
            if parsed_from:
                conds.append("date(created_at) >= ?")
                params.append(parsed_from.isoformat())
            if parsed_to:
                conds.append("date(created_at) <= ?")
                params.append(parsed_to.isoformat())
            where = " AND ".join(conds)
            sql = f"SELECT * FROM research_entries WHERE {where} ORDER BY created_at DESC LIMIT 500"
            async with db.db.execute(sql, tuple(params)) as cursor:
                for row in await cursor.fetchall():
                    tags = _safe_json_list(_row_get(row, "tags"))
                    url = _safe_json_field(_row_get(row, "raw_data"), "url")
                    timeline.append(
                        {
                            "id": row["id"],
                            "source_table": "research_entries",
                            "date": (_row_get(row, "created_at") or "")[:10],
                            "event_type": "research",
                            "title": row["title"],
                            "summary": (_row_get(row, "summary") or "")[:200],
                            "category": _row_get(row, "source", "research"),
                            "document_id": None,
                            "gdrive_url": url,
                            "tags": tags,
                        }
                    )
        except Exception:
            logger.debug("research_entries query failed — table may not exist")

    # ── Search filter ─────────────────────────────────────────────────────
    if search:
        q = search.lower()
        timeline = [
            item
            for item in timeline
            if q in item["title"].lower()
            or q in item.get("summary", "").lower()
            or q in str(item.get("tags", [])).lower()
        ]

    # ── Sort ──────────────────────────────────────────────────────────────
    timeline.sort(key=lambda x: x["date"], reverse=(sort != "asc"))

    # ── Pagination ────────────────────────────────────────────────────────
    total = len(timeline)
    timeline = timeline[offset : offset + limit]

    return json.dumps({"items": timeline, "total": total, "offset": offset, "limit": limit})


def register(mcp):
    mcp.tool()(log_conversation)
    mcp.tool()(search_conversations)
    mcp.tool()(get_conversation)
    mcp.tool()(get_journey_timeline)
