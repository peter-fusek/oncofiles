"""Session diary tools (#721 follow-up, maintainer decision 2026-11-11).

The ``conversation_entries`` table is a **patient-scoped session diary**: a
place where Claude.ai, Claude Code, ChatGPT or another MCP client writes a
short note about a session with or about one patient (what was discussed,
what was decided, open questions). It used to be Oncoteam's narrative
archive; Oncoteam is retired and the diary now belongs to the chat clients.

Rules (privacy + infra review):

* **Scope.** Every tool resolves the patient through ``_resolve_patient_id``
  (ACL gate #497). An admin caller must name the patient with
  ``patient_slug``; there is no global diary and no implicit admin scope.
  A foreign or unknown entry id answers ``not found`` — never "wrong patient".
* **Local only.** Diary text is never sent to a cloud model, never exported
  to Drive in the background, and never a source of clinical facts: no
  extractor or indexer reads ``conversation_entries`` (lock test
  ``tests/test_session_diary.py::test_no_extractor_or_indexer_reads_the_diary``).
* **Retention.** Soft delete + restore only (#723); the table is never
  hard-deleted (lock in ``tests/test_soft_delete_723.py``).
* **Limits.** Per-entry size cap, per-patient daily cap, participant from a
  fixed list, control characters stripped. Every write, delete and restore
  leaves an ``activity_log`` row with ids only (never title or content), and
  the audit middleware records only non-content arguments of these tools.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from fastmcp import Context

from oncofiles.models import ActivityLogEntry, ConversationEntry, ConversationQuery, EntryType
from oncofiles.tools._helpers import (
    _caller_email,
    _get_db,
    _is_admin_caller,
    _parse_date,
    _resolve_patient_id,
)

logger = logging.getLogger(__name__)

# ── Diary limits ─────────────────────────────────────────────────────────

DIARY_TABLE = "conversation_entries"
# Who may be named as the author of a new entry. Older archive rows may carry
# other values (e.g. "oncoteam"); those stay readable and filterable.
DIARY_PARTICIPANTS: tuple[str, ...] = ("claude.ai", "claude-code", "chatgpt", "gemini", "other")
DIARY_ENTRY_TYPES: tuple[str, ...] = tuple(t.value for t in EntryType)
DIARY_MAX_CONTENT_BYTES = 20_000  # UTF-8 bytes of the body
DIARY_MAX_TITLE_CHARS = 200
DIARY_MAX_TAGS = 20
DIARY_MAX_TAG_CHARS = 50
DIARY_MAX_DOCUMENT_IDS = 50
DIARY_MAX_ENTRIES_PER_DAY = 50  # live entries per patient per UTC day (deleted ones count)
DIARY_SEARCH_MAX_LIMIT = 200
DIARY_SEARCH_MAX_OFFSET = 10_000
DIARY_PREVIEW_CHARS = 500

# Tool names of the diary (write, read, trash). The audit middleware logs only
# DIARY_AUDIT_SAFE_ARGS for these, never title/content/text/tags.
DIARY_WRITE_TOOLS = frozenset({"log_conversation", "diary_add"})
DIARY_TRASH_TOOLS = frozenset({"delete_conversation", "restore_conversation"})
DIARY_READ_TOOLS = frozenset({"search_conversations", "get_conversation", "get_conversation_stats"})
DIARY_TOOLS = DIARY_WRITE_TOOLS | DIARY_TRASH_TOOLS | DIARY_READ_TOOLS
DIARY_AUDIT_SAFE_ARGS = frozenset(
    {
        "patient_slug",
        "entry_id",
        "entry_date",
        "entry_type",
        "participant",
        "document_ids",
        "date_from",
        "date_to",
        "limit",
        "offset",
        "top_tags_limit",
    }
)

# C0/C1 controls except TAB and LF, DEL, and the bidi override/isolate marks
# that can make text render differently from what is stored.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def clean_diary_text(value: str | None) -> str:
    """Normalise line endings and strip control characters (keeps TAB/LF)."""
    if not value:
        return ""
    return _CONTROL_RE.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))


def _error(message: str, code: str = "invalid") -> str:
    return json.dumps({"error": message, "code": code})


def _not_found(entry_id: int) -> str:
    return _error(f"Diary entry not found: {entry_id}", "not_found")


async def _diary_patient(ctx: Context, patient_slug: str | None) -> str:
    """Resolve the diary's patient. Admins must name one (no global diary)."""
    if _is_admin_caller() and not patient_slug:
        raise ValueError(
            "Admin callers must pass patient_slug=<slug>: the session diary is "
            "per patient and has no implicit admin scope."
        )
    return await _resolve_patient_id(patient_slug, ctx)


async def _audit(db, *, action: str, entry_id: int, patient_id: str, actor: str) -> None:
    """activity_log row for a diary write. Ids only — never title, content or tags."""
    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=patient_id,
                session_id="session-diary",
                agent_id=actor or "unknown",
                tool_name=f"diary_{action}",
                input_summary=f"{DIARY_TABLE} id={entry_id}",
                output_summary=action,
                tags='["diary"]',
            )
        )
    except Exception:
        logger.exception("diary audit row failed: %s id=%s", action, entry_id)


def _parse_tags(tags: str | None) -> list[str] | None:
    """Comma-separated tags → cleaned, de-duplicated list (order kept)."""
    if not tags:
        return None
    out: list[str] = []
    for raw in tags.split(","):
        tag = clean_diary_text(raw).strip()
        if not tag:
            continue
        if len(tag) > DIARY_MAX_TAG_CHARS:
            raise ValueError(f"Tag longer than {DIARY_MAX_TAG_CHARS} characters.")
        if tag not in out:
            out.append(tag)
    if len(out) > DIARY_MAX_TAGS:
        raise ValueError(f"At most {DIARY_MAX_TAGS} tags per entry.")
    return out or None


def _parse_document_ids(document_ids: str | None) -> list[int] | None:
    if not document_ids:
        return None
    try:
        ids = [int(d.strip()) for d in document_ids.split(",") if d.strip()]
    except ValueError as exc:
        raise ValueError(
            f"Invalid document_ids: {document_ids!r}. Expected comma-separated integers."
        ) from exc
    ids = list(dict.fromkeys(ids))
    if len(ids) > DIARY_MAX_DOCUMENT_IDS:
        raise ValueError(f"At most {DIARY_MAX_DOCUMENT_IDS} document_ids per entry.")
    return ids or None


async def _entries_today(db, patient_id: str) -> int:
    """Live entries written for this patient since 00:00 UTC (soft-deleted count too)."""
    async with db.db.execute(
        f"SELECT COUNT(*) AS c FROM {DIARY_TABLE} "
        "WHERE patient_id = ? AND source = 'live' AND created_at >= date('now')",
        (patient_id,),
    ) as cur:
        row = await cur.fetchone()
    return int(dict(row).get("c", 0)) if row else 0


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


# ── Write ────────────────────────────────────────────────────────────────


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
    """Add an entry to the patient's session diary (same as diary_add).

    The session diary is a per-patient notebook for chat sessions in
    Claude.ai, Claude Code, ChatGPT or another MCP client: what was
    discussed, decided or left open. It is stored on the Oncofiles server
    only, is never sent to a cloud model, and is never used as a source of
    clinical facts (lab values, diagnoses) — store those as documents.

    Limits: body up to 20 kB, title up to 200 characters, up to 20 tags,
    up to 50 entries per patient per day. Control characters are removed.

    Args:
        title: Short title for the entry (max 200 characters).
        content: Markdown body (max 20 kB).
        entry_date: Date the entry is about (YYYY-MM-DD). Defaults to today.
        entry_type: summary, decision, progress, question or note.
        tags: Comma-separated tags (e.g. "cycle-3,questions").
        document_ids: Comma-separated ids of this patient's documents the
            entry refers to (e.g. "3,15"). Each must belong to the patient.
        participant: Which client wrote it: claude.ai, claude-code, chatgpt,
            gemini or other.
        patient_slug: The patient. Required for admin callers; otherwise
            defaults to the patient your connection is bound to.
    """
    db = _get_db(ctx)
    try:
        pid = await _diary_patient(ctx, patient_slug)
        parsed_date = _parse_date(entry_date) or date.today()
        parsed_tags = _parse_tags(tags)
        parsed_doc_ids = _parse_document_ids(document_ids)
    except ValueError as e:
        return _error(str(e))

    if participant not in DIARY_PARTICIPANTS:
        return _error(
            f"Invalid participant {participant!r}. Use one of: {', '.join(DIARY_PARTICIPANTS)}."
        )
    if entry_type not in DIARY_ENTRY_TYPES:
        return _error(
            f"Invalid entry_type {entry_type!r}. Use one of: {', '.join(DIARY_ENTRY_TYPES)}."
        )

    clean_title = clean_diary_text(title).strip()
    clean_content = clean_diary_text(content).strip()
    if not clean_title or not clean_content:
        return _error("title and content must not be empty.")
    if len(clean_title) > DIARY_MAX_TITLE_CHARS:
        return _error(f"title is longer than {DIARY_MAX_TITLE_CHARS} characters.", "too_large")
    size = len(clean_content.encode("utf-8"))
    if size > DIARY_MAX_CONTENT_BYTES:
        return _error(
            f"content is {size} bytes; the limit is {DIARY_MAX_CONTENT_BYTES} bytes per entry. "
            "Split it into several entries or summarise it.",
            "too_large",
        )

    if parsed_doc_ids:
        owned = await db.get_documents_by_ids(set(parsed_doc_ids), patient_id=pid)
        missing = [
            i for i in parsed_doc_ids if i not in owned or getattr(owned[i], "deleted_at", None)
        ]
        if missing:
            # Same answer for "another patient's" and "does not exist" (#499).
            return _error(f"Document(s) not found for this patient: {missing}", "not_found")

    if await _entries_today(db, pid) >= DIARY_MAX_ENTRIES_PER_DAY:
        return _error(
            f"Daily limit reached: at most {DIARY_MAX_ENTRIES_PER_DAY} diary entries per "
            "patient per day. Summarise into fewer entries or continue tomorrow.",
            "rate_limited",
        )

    entry = ConversationEntry(
        entry_date=parsed_date,
        entry_type=entry_type,
        title=clean_title,
        content=clean_content,
        participant=participant,
        session_type="technical" if participant == "claude-code" else "patient",
        session_id=getattr(ctx, "session_id", None),
        tags=parsed_tags,
        document_ids=parsed_doc_ids,
        source="live",
    )
    entry = await db.insert_conversation_entry(entry, patient_id=pid)
    await _audit(db, action="add", entry_id=entry.id, patient_id=pid, actor=participant)
    return json.dumps(
        {
            "id": entry.id,
            "entry_date": entry.entry_date.isoformat(),
            "entry_type": entry.entry_type,
            "title": entry.title,
            "tags": entry.tags,
            "document_ids": entry.document_ids,
            "participant": entry.participant,
        }
    )


async def diary_add(
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
    """Add an entry to the patient's session diary (alias of log_conversation).

    Use at the end of a chat session (Claude.ai, Claude Code, ChatGPT, ...)
    to note what was discussed, decided or left open for this patient. Kept
    on the Oncofiles server only; never sent to a cloud model and never used
    as a source of clinical facts. Limits: body 20 kB, title 200 characters,
    20 tags, 50 entries per patient per day.

    Args:
        title: Short title for the entry (max 200 characters).
        content: Markdown body (max 20 kB).
        entry_date: Date the entry is about (YYYY-MM-DD). Defaults to today.
        entry_type: summary, decision, progress, question or note.
        tags: Comma-separated tags.
        document_ids: Comma-separated ids of this patient's documents.
        participant: claude.ai, claude-code, chatgpt, gemini or other.
        patient_slug: The patient. Required for admin callers.
    """
    return await log_conversation(
        ctx,
        title=title,
        content=content,
        entry_date=entry_date,
        entry_type=entry_type,
        tags=tags,
        document_ids=document_ids,
        participant=participant,
        patient_slug=patient_slug,
    )


async def delete_conversation(
    ctx: Context,
    entry_id: int,
    patient_slug: str | None = None,
) -> str:
    """Move a session-diary entry to the trash (soft delete).

    The entry disappears from search, get, stats and the journey timeline
    but is kept and can be brought back with restore_conversation. Diary
    entries are never hard-deleted.

    Args:
        entry_id: The diary entry id.
        patient_slug: The patient. Required for admin callers.
    """
    db = _get_db(ctx)
    try:
        pid = await _diary_patient(ctx, patient_slug)
    except ValueError as e:
        return _error(str(e))
    actor = _caller_email() or "mcp"
    if not await db.delete_conversation_entry(entry_id, patient_id=pid, deleted_by=actor):
        return _not_found(entry_id)
    return json.dumps(
        {
            "deleted": True,
            "entry_id": entry_id,
            "message": "Moved to trash. Bring it back with restore_conversation.",
        }
    )


async def restore_conversation(
    ctx: Context,
    entry_id: int,
    patient_slug: str | None = None,
) -> str:
    """Restore a session-diary entry from the trash (undo delete_conversation).

    Args:
        entry_id: The diary entry id.
        patient_slug: The patient. Required for admin callers.
    """
    db = _get_db(ctx)
    try:
        pid = await _diary_patient(ctx, patient_slug)
    except ValueError as e:
        return _error(str(e))
    actor = _caller_email() or "mcp"
    restored = await db.restore_conversation_entry(entry_id, patient_id=pid, restored_by=actor)
    if restored is None:
        return _error(f"Diary entry {entry_id} is not in the trash.", "not_found")
    return json.dumps(
        {
            "restored": True,
            "entry_id": entry_id,
            "entry_date": restored.entry_date.isoformat() if restored.entry_date else None,
            "title": restored.title,
        }
    )


# ── Read ─────────────────────────────────────────────────────────────────


async def search_conversations(
    ctx: Context,
    text: str | None = None,
    entry_type: str | None = None,
    participant: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    tags: str | None = None,
    limit: int = 50,
    offset: int = 0,
    patient_slug: str | None = None,
) -> str:
    """Search the patient's session diary by text, type, author, date or tags.

    Returns entries with content cut to 500 characters, newest first. Use
    get_conversation for the full text of one entry. Page with offset;
    ``has_more`` says whether another page exists. Read-only.

    Args:
        text: Full-text search (words; punctuation is ignored).
        entry_type: summary, decision, progress, question or note.
        participant: claude.ai, claude-code, chatgpt, gemini, other (older
            archive entries may also carry oncoteam).
        date_from: From this entry date (YYYY-MM-DD).
        date_to: To this entry date (YYYY-MM-DD).
        tags: Comma-separated tags; an entry must carry all of them (exact match).
        limit: Page size, 1-200 (default 50).
        offset: Entries to skip, 0-10000.
        patient_slug: The patient. Required for admin callers.
    """
    from oncofiles.memory import query_slot, update_peak_rss

    db = _get_db(ctx)
    try:
        pid = await _diary_patient(ctx, patient_slug)
        parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        page = max(1, min(int(limit), DIARY_SEARCH_MAX_LIMIT))
        skip = max(0, min(int(offset), DIARY_SEARCH_MAX_OFFSET))
        query = ConversationQuery(
            text=text,
            entry_type=entry_type,
            participant=participant,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            tags=parsed_tags,
            # one extra row tells whether another page exists
            limit=page + 1,
            offset=skip,
        )
    except (ValueError, TypeError) as e:
        return _error(str(e))
    async with query_slot("search_conversations"):
        entries = await db.search_conversation_entries(query, patient_id=pid)
    update_peak_rss()
    has_more = len(entries) > page
    entries = entries[:page]
    items = [
        {
            "id": e.id,
            "entry_date": e.entry_date.isoformat() if e.entry_date else None,
            "entry_type": e.entry_type,
            "title": e.title,
            "content": e.content[:DIARY_PREVIEW_CHARS]
            + ("..." if len(e.content) > DIARY_PREVIEW_CHARS else ""),
            "participant": e.participant,
            "tags": e.tags,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return json.dumps(
        {
            "entries": items,
            "total": len(items),
            "offset": skip,
            "limit": page,
            "has_more": has_more,
        }
    )


async def get_conversation(ctx: Context, entry_id: int, patient_slug: str | None = None) -> str:
    """Get the full text of one session-diary entry by id. Read-only.

    Args:
        entry_id: The diary entry id.
        patient_slug: The patient. Required for admin callers.
    """
    db = _get_db(ctx)
    try:
        pid = await _diary_patient(ctx, patient_slug)
    except ValueError as e:
        return _error(str(e))
    # The SQL filter on patient_id is the isolation boundary (#499): a foreign
    # or deleted id is simply "not found".
    entry = await db.get_conversation_entry(entry_id, patient_id=pid)
    if not entry:
        return _not_found(entry_id)
    return json.dumps(
        {
            "id": entry.id,
            "entry_date": entry.entry_date.isoformat() if entry.entry_date else None,
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
            conds = ["patient_id = ?", "deleted_at IS NULL"]
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


async def get_conversation_stats(
    ctx: Context,
    date_from: str | None = None,
    date_to: str | None = None,
    top_tags_limit: int = 20,
    patient_slug: str | None = None,
) -> str:
    """Session-diary stats for one patient: counts, topics, monthly breakdown. Read-only.

    Shows how much of the patient's journey is noted in the session diary
    and where the gaps are (#462, #455). Entries in the trash are not counted.

    Returns JSON with:
      - total: total live diary entries (respects date filters)
      - by_entry_type: {type: count}
      - by_participant: {participant: count}
      - by_month: ordered [{month: YYYY-MM, count}]
      - top_tags: [{tag, count}] capped at top_tags_limit
      - date_range: {first, last} as YYYY-MM-DD or None
      - recency: {last_7_days, last_30_days}

    Args:
        date_from: Only consider entries with entry_date >= this (YYYY-MM-DD).
        date_to: Only consider entries with entry_date <= this (YYYY-MM-DD).
        top_tags_limit: Max number of tags returned in top_tags (default 20, max 100).
        patient_slug: The patient. Required for admin callers.
    """
    try:
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
        top_tags_limit = max(1, min(int(top_tags_limit or 20), 100))
    except (ValueError, TypeError) as e:
        return _error(str(e))

    db = _get_db(ctx)
    try:
        pid = await _diary_patient(ctx, patient_slug)
    except ValueError as e:
        return _error(str(e))

    conds = ["patient_id = ?", "deleted_at IS NULL"]
    params: list[str] = [pid]
    if parsed_from:
        conds.append("entry_date >= ?")
        params.append(parsed_from.isoformat())
    if parsed_to:
        conds.append("entry_date <= ?")
        params.append(parsed_to.isoformat())
    where = " AND ".join(conds)

    async def _scalar_count(extra_sql: str = "") -> int:
        sql = f"SELECT COUNT(*) AS c FROM conversation_entries WHERE {where} {extra_sql}"
        async with db.db.execute(sql, tuple(params)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return 0
        try:
            return int(row["c"])
        except Exception:
            return int(dict(row).get("c", 0))

    total = await _scalar_count()

    # ── by_entry_type / by_participant / by_month ─────────────────────────
    async def _group_counts(field_sql: str) -> dict[str, int]:
        sql = (
            f"SELECT {field_sql} AS k, COUNT(*) AS c FROM conversation_entries "
            f"WHERE {where} GROUP BY k ORDER BY c DESC"
        )
        out: dict[str, int] = {}
        async with db.db.execute(sql, tuple(params)) as cursor:
            for row in await cursor.fetchall():
                d = dict(row)
                key = d.get("k") or "(unset)"
                out[str(key)] = int(d.get("c", 0))
        return out

    by_entry_type = await _group_counts("entry_type")
    by_participant = await _group_counts("participant")
    by_month_raw = await _group_counts("substr(entry_date, 1, 7)")
    by_month = [{"month": m, "count": by_month_raw[m]} for m in sorted(by_month_raw.keys())]

    # ── date_range ────────────────────────────────────────────────────────
    async with db.db.execute(
        f"SELECT MIN(entry_date) AS mn, MAX(entry_date) AS mx FROM conversation_entries "
        f"WHERE {where}",
        tuple(params),
    ) as cursor:
        row = await cursor.fetchone()
    range_row = dict(row) if row else {}
    date_range = {"first": range_row.get("mn"), "last": range_row.get("mx")}

    # ── top_tags via json_each over tags JSON blob ────────────────────────
    # conversation_entries.tags is a TEXT column with JSON array contents.
    # json_each works on SQLite/Turso (see migration 045 pattern). If a row's
    # tags column is NULL or empty, json_each returns no rows for it.
    # A malformed legacy tags value must not fail the whole aggregation, so
    # only rows holding a valid JSON array reach json_each (the CASE is lazy).
    tags_sql = (
        f"SELECT j.value AS tag, COUNT(*) AS c "
        f"FROM conversation_entries, json_each("
        f"  CASE WHEN json_valid(conversation_entries.tags) "
        f"  THEN conversation_entries.tags ELSE '[]' END) AS j "
        f"WHERE {where} AND conversation_entries.tags IS NOT NULL "
        f"  AND conversation_entries.tags != '' "
        f"GROUP BY j.value ORDER BY c DESC LIMIT ?"
    )
    tag_params = list(params) + [top_tags_limit]
    top_tags: list[dict] = []
    try:
        async with db.db.execute(tags_sql, tuple(tag_params)) as cursor:
            for row in await cursor.fetchall():
                d = dict(row)
                tag_val = d.get("tag")
                if tag_val is None:
                    continue
                top_tags.append({"tag": str(tag_val), "count": int(d.get("c", 0))})
    except Exception:
        logger.debug("get_conversation_stats: json_each tag aggregation failed", exc_info=True)

    # ── recency: last 7 / 30 days (relative to today, ignores date filters) ─
    recency_conds = ["patient_id = ?", "deleted_at IS NULL"]
    recency_params: list[str] = [pid]
    # Use today-anchored SQL expressions so the count is "rolling" regardless
    # of the date_from/date_to bounds above.
    last_7_days = 0
    last_30_days = 0
    try:
        async with db.db.execute(
            "SELECT "
            "  SUM(CASE WHEN entry_date >= date('now', '-7 days') THEN 1 ELSE 0 END) AS d7, "
            "  SUM(CASE WHEN entry_date >= date('now', '-30 days') THEN 1 ELSE 0 END) AS d30 "
            "FROM conversation_entries WHERE " + " AND ".join(recency_conds),
            tuple(recency_params),
        ) as cursor:
            row = await cursor.fetchone()
        d = dict(row) if row else {}
        last_7_days = int(d.get("d7") or 0)
        last_30_days = int(d.get("d30") or 0)
    except Exception:
        logger.debug("get_conversation_stats: recency aggregation failed", exc_info=True)

    return json.dumps(
        {
            "patient_id": pid,
            "total": total,
            "by_entry_type": by_entry_type,
            "by_participant": by_participant,
            "by_month": by_month,
            "top_tags": top_tags,
            "date_range": date_range,
            "recency": {"last_7_days": last_7_days, "last_30_days": last_30_days},
        },
        default=str,
    )


# MCP tool annotations. The diary is local-only (openWorldHint False). Writes
# and trash moves are never read-only; a soft delete is reversible but hides
# the entry, so clients should confirm it (destructiveHint True).
DIARY_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
DIARY_WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
DIARY_DELETE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}
DIARY_RESTORE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def register(mcp):
    mcp.tool(annotations=DIARY_WRITE)(log_conversation)
    mcp.tool(annotations=DIARY_WRITE)(diary_add)
    mcp.tool(annotations=DIARY_READ_ONLY)(search_conversations)
    mcp.tool(annotations=DIARY_READ_ONLY)(get_conversation)
    mcp.tool(annotations=DIARY_READ_ONLY)(get_conversation_stats)
    mcp.tool(annotations=DIARY_DELETE)(delete_conversation)
    mcp.tool(annotations=DIARY_RESTORE)(restore_conversation)
    mcp.tool()(get_journey_timeline)
