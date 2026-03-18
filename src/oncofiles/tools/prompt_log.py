"""MCP tools for prompt log observability."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import PromptLogQuery
from oncofiles.tools._helpers import _get_db


async def get_prompt_log_entry(
    ctx: Context,
    entry_id: int,
) -> str:
    """Get a single prompt log entry with full prompts and raw response.

    Returns the complete AI call record including system prompt, user prompt,
    raw AI response, token counts, and timing. Use search_prompt_log to find entries.

    Args:
        entry_id: The prompt log entry ID.
    """
    db = _get_db(ctx)
    entry = await db.get_prompt_log(entry_id)
    if not entry:
        return json.dumps({"error": f"Prompt log entry not found: {entry_id}"})

    return json.dumps(
        {
            "id": entry.id,
            "call_type": entry.call_type.value,
            "document_id": entry.document_id,
            "model": entry.model,
            "system_prompt": entry.system_prompt,
            "user_prompt": entry.user_prompt,
            "raw_response": entry.raw_response,
            "input_tokens": entry.input_tokens,
            "output_tokens": entry.output_tokens,
            "duration_ms": entry.duration_ms,
            "result_summary": entry.result_summary,
            "status": entry.status,
            "error_message": entry.error_message,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
    )


async def search_prompt_log(
    ctx: Context,
    call_type: str | None = None,
    document_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    text: str | None = None,
    limit: int = 50,
) -> str:
    """Search prompt logs — all AI calls made during document processing.

    Returns a list of prompt log entries (without full prompts for brevity).
    Use get_prompt_log_entry to see full prompts and responses for a specific entry.

    Args:
        call_type: Filter by type: 'ocr', 'summary_tags', 'structured_metadata',
                   'filename_description'.
        document_id: Filter by document ID.
        status: Filter by status ('ok' or 'error').
        date_from: Filter from date (YYYY-MM-DD).
        date_to: Filter to date (YYYY-MM-DD).
        text: Search in prompts and responses.
        limit: Max results (1-200, default 50).
    """
    from oncofiles.tools._helpers import _parse_date

    db = _get_db(ctx)
    query = PromptLogQuery(
        call_type=call_type,
        document_id=document_id,
        status=status,
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
        text=text,
        limit=min(max(1, limit), 200),
    )
    entries = await db.search_prompt_log(query)

    # Return compact list (no full prompts — use get_prompt_log_entry for those)
    items = [
        {
            "id": e.id,
            "call_type": e.call_type.value,
            "document_id": e.document_id,
            "model": e.model,
            "input_tokens": e.input_tokens,
            "output_tokens": e.output_tokens,
            "duration_ms": e.duration_ms,
            "result_summary": e.result_summary,
            "status": e.status,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]

    return json.dumps({"matched": len(items), "entries": items})


def register(mcp):
    mcp.tool()(get_prompt_log_entry)
    mcp.tool()(search_prompt_log)
