"""MCP tools for prompt log observability."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import PromptLogQuery
from oncofiles.tools._helpers import _get_db, _resolve_patient_id


async def get_prompt_log_entry(
    ctx: Context,
    entry_id: int,
    patient_slug: str | None = None,
) -> str:
    """Get a single prompt log entry with full prompts and raw response.

    Returns the complete AI call record including system prompt, user prompt,
    raw AI response, token counts, and timing. Use search_prompt_log to find entries.

    Args:
        entry_id: The prompt log entry ID.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    entry = await db.get_prompt_log(entry_id, patient_id=pid)
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
    patient_slug: str | None = None,
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
        patient_slug: Optional — explicit patient slug (#429).
    """
    from oncofiles.tools._helpers import _parse_date

    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    query = PromptLogQuery(
        call_type=call_type,
        document_id=document_id,
        status=status,
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
        text=text,
        limit=min(max(1, limit), 200),
    )
    entries = await db.search_prompt_log(query, patient_id=pid)

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


async def backfill_orphan_prompt_logs(
    ctx: Context,
    batch_size: int = 500,
    dry_run: bool = True,
) -> str:
    """Backfill prompt_log rows that were stored with patient_id='' (#476 orphans).

    **Admin-only** (#484 sweep follow-up): operates on prompt_log rows
    system-wide (no patient_slug parameter) and rewrites the `patient_id`
    column — giving a patient-token holder write access to other patients'
    attribution. Restricted to static MCP_BEARER_TOKEN callers or OAuth
    callers whose email is in DASHBOARD_ADMIN_EMAILS.

    Processes one batch at a time so long-running UPDATE transactions don't
    corrupt the Turso embedded replica (migration 062 crashed prod trying to
    do 19,707 UPDATEs in one transaction — this tool does chunked work).

    Recovery strategy per row:
      1. If document_id is set AND documents.id exists → set patient_id to
         documents.patient_id (recovers attribution ~80% of the time).
      2. Otherwise → set patient_id to the sentinel '__system_no_patient__'
         so cross-patient WHERE-clause queries can never match it.

    Call repeatedly until `remaining` hits 0. Each call processes at most
    `batch_size` rows.

    Args:
        batch_size: Max rows per call. Default 500 — safely below the Turso
            transaction size that corrupted the replica in migration 062.
        dry_run: If True (default), report what would change without writing.

    Returns JSON with:
        - scanned: rows examined this batch
        - recovered_via_document: attributed to real patient via JOIN
        - sentinel_assigned: set to '__system_no_patient__'
        - unrecoverable_document_missing: document_id set but document deleted
        - remaining: orphan rows still to process after this batch
        - dry_run: whether changes were applied
    """
    from oncofiles.tools._helpers import _require_admin_or_raise

    try:
        _require_admin_or_raise("backfill_orphan_prompt_logs")
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    db = _get_db(ctx)

    # Count remaining orphans before the batch
    async with db.db.execute("SELECT COUNT(*) AS cnt FROM prompt_log WHERE patient_id = ''") as cur:
        row = await cur.fetchone()
        total_before = dict(row)["cnt"] if row else 0

    if total_before == 0:
        return json.dumps(
            {
                "scanned": 0,
                "recovered_via_document": 0,
                "sentinel_assigned": 0,
                "unrecoverable_document_missing": 0,
                "remaining": 0,
                "dry_run": dry_run,
                "status": "complete",
            }
        )

    # Pull the batch
    batch_size = max(1, min(batch_size, 2000))
    async with db.db.execute(
        """
        SELECT pl.id AS pl_id, pl.document_id, d.patient_id AS doc_patient_id
        FROM prompt_log pl
        LEFT JOIN documents d ON d.id = pl.document_id
        WHERE pl.patient_id = ''
        LIMIT ?
        """,
        (batch_size,),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    recovered: list[int] = []
    sentinel: list[int] = []
    missing_doc: list[int] = []
    SENTINEL = "__system_no_patient__"  # noqa: N806

    for r in rows:
        pl_id = r["pl_id"]
        doc_pid = r.get("doc_patient_id")
        if doc_pid:
            recovered.append((pl_id, doc_pid))
        elif r.get("document_id") is not None:
            missing_doc.append(pl_id)
        else:
            sentinel.append(pl_id)

    if not dry_run:
        # Apply recoveries
        for pl_id, doc_pid in recovered:
            await db.db.execute(
                "UPDATE prompt_log SET patient_id = ? WHERE id = ?",
                (doc_pid, pl_id),
            )
        # Apply sentinel to both unrecoverable-doc and no-doc
        if missing_doc or sentinel:
            all_sentinel_ids = [pl_id for pl_id in missing_doc] + sentinel
            placeholders = ",".join("?" for _ in all_sentinel_ids)
            await db.db.execute(
                f"UPDATE prompt_log SET patient_id = ? WHERE id IN ({placeholders})",
                [SENTINEL, *all_sentinel_ids],
            )
        await db.db.commit()

    remaining_after = total_before - len(rows) if not dry_run else total_before

    return json.dumps(
        {
            "scanned": len(rows),
            "recovered_via_document": len(recovered),
            "sentinel_assigned": len(sentinel),
            "unrecoverable_document_missing": len(missing_doc),
            "remaining": remaining_after,
            "dry_run": dry_run,
            "status": "partial" if remaining_after > 0 else "complete",
            "_next_call": (
                f"Call again with dry_run={dry_run} to process next {batch_size} rows"
                if remaining_after > 0
                else "All orphans processed"
            ),
        }
    )


def register(mcp):
    mcp.tool()(get_prompt_log_entry)
    mcp.tool()(search_prompt_log)
    mcp.tool()(backfill_orphan_prompt_logs)
