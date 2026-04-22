"""MCP tools for clinical_records + notes + analyses (#450 Phases 1 + 2).

Phase 1 (v5.11) shipped four read/write tools (add, get, add_note, list_notes).
Phase 2 (v5.13) adds seven more to close the CRUD loop + expose audit + give
Oncoteam a place to persist analytic outputs:

  - update_clinical_record     — partial update + audit row
  - delete_clinical_record     — soft-delete + audit row
  - restore_clinical_record    — restore from soft-delete + audit row
  - list_clinical_records      — paginated filter
  - get_record_audit           — full chronological audit history
  - add_clinical_analysis      — persist analytic output referencing N records
  - search_notes               — LIKE %q% on note_text (FTS5 deferred)

All tools accept ``patient_slug`` per the Option A pattern (#429) — required
in stateless-HTTP contexts (Claude.ai, ChatGPT) where ``select_patient()``
state does not persist across tool calls. All mutating tools validate
patient_id ownership before writing to prevent cross-patient ID spoofing.
"""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import (
    ClinicalAnalysis,
    ClinicalRecord,
    ClinicalRecordNote,
    ClinicalRecordQuery,
)
from oncofiles.tools._helpers import _get_db, _resolve_patient_id


async def add_clinical_record(
    ctx: Context,
    record_type: str,
    source: str,
    param: str | None = None,
    value_num: float | None = None,
    value_text: str | None = None,
    unit: str | None = None,
    status: str | None = None,
    occurred_at: str | None = None,
    source_document_id: int | None = None,
    ref_range_low: float | None = None,
    ref_range_high: float | None = None,
    metadata_json: str | None = None,
    session_id: str | None = None,
    caller_identity: str | None = None,
    created_by: str | None = None,
    reason: str | None = None,
    patient_slug: str | None = None,
) -> str:
    """Insert a canonical clinical fact (#450 Phase 1).

    Use this for any structured clinical data you want queryable + audited:
    labs (param='CEA', value_num=5.2), biomarkers (param='KRAS', value_text='G12S'),
    findings, medications, procedures, etc. Every insert generates a matching
    audit row so the caregiver can see who recorded what and when.

    Args:
        record_type: 'lab' | 'finding' | 'medication' | 'treatment_event' |
            'biomarker' | 'condition' | 'procedure' | 'imaging' | 'pathology' |
            'genetic_variant' | 'allergy' | 'vital'
        source: Provenance tag — 'manual', 'ai-extract', 'oncoteam',
            'mcp-claude', 'mcp-chatgpt', 'import-*'. Shapes the audit trail.
        param: Parameter name — 'CEA', 'KRAS', 'Oxaliplatin', 'BRCA1'.
        value_num: Numeric value (labs, doses, sizes).
        value_text: Text value ('wild-type', 'G12S', 'pT3N1M0').
        unit: Unit string — 'ng/mL', 'mg/m2', 'mm'.
        status: Clinical status — 'active' | 'resolved' | 'suspected' | 'confirmed' |
            'normal' | 'abnormal' | 'high' | 'low'.
        occurred_at: ISO date/datetime of the clinical event (YYYY-MM-DD).
        source_document_id: Id of the source PDF if this was extracted from one.
        ref_range_low / ref_range_high: Reference range bounds (for labs).
        metadata_json: JSON string for type-specific overflow fields.
        session_id: MCP/Claude.ai/ChatGPT conversation UUID for audit linking.
        caller_identity: Token hash or OAuth sub — shapes accountability.
        created_by: Email of the actor (or 'system' for automated paths).
        reason: Human explanation of why this record was added (goes in audit).
        patient_slug: Explicit patient slug — required in stateless-HTTP.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    record = ClinicalRecord(
        patient_id=pid,
        record_type=record_type,
        source_document_id=source_document_id,
        occurred_at=occurred_at,
        param=param,
        value_num=value_num,
        value_text=value_text,
        unit=unit,
        status=status,
        ref_range_low=ref_range_low,
        ref_range_high=ref_range_high,
        metadata_json=metadata_json,
        source=source,
        session_id=session_id,
        caller_identity=caller_identity,
        created_by=created_by,
    )
    stored = await db.insert_clinical_record(record, reason=reason)
    return json.dumps(
        {
            "status": "created",
            "id": stored.id,
            "patient_slug": patient_slug or "current",
            "record_type": stored.record_type,
            "param": stored.param,
            "created_at": stored.created_at,
        },
        ensure_ascii=False,
    )


async def get_clinical_record(
    ctx: Context,
    record_id: int,
    include_audit: bool = False,
    include_notes: bool = False,
    patient_slug: str | None = None,
) -> str:
    """Fetch a single clinical record, optionally with audit trail and notes.

    Args:
        record_id: The clinical record id.
        include_audit: If True, include full change history under key 'audit'.
        include_notes: If True, include active (non-deleted) notes under 'notes'.
        patient_slug: Optional — validates the record belongs to the targeted
            patient (defence-in-depth against cross-patient ID spoofing).
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    record = await db.get_clinical_record(record_id, include_deleted=True)
    if record is None:
        return json.dumps({"error": "not_found", "record_id": record_id})
    if record.patient_id != pid:
        return json.dumps({"error": "wrong_patient", "record_id": record_id})

    result: dict = {"record": record.model_dump()}

    if include_audit:
        audit = await db.list_clinical_record_audit(record_id)
        result["audit"] = [row.model_dump() for row in audit]

    if include_notes:
        notes = await db.list_clinical_record_notes(record_id=record_id)
        result["notes"] = [n.model_dump() for n in notes]

    return json.dumps(result, ensure_ascii=False, default=str)


async def add_clinical_record_note(
    ctx: Context,
    record_id: int,
    note_text: str,
    source: str,
    tags: list[str] | None = None,
    session_id: str | None = None,
    mcp_conversation_ref: str | None = None,
    caller_identity: str | None = None,
    created_by: str | None = None,
    patient_slug: str | None = None,
) -> str:
    """Attach a free-form note to a clinical record.

    The killer feature for #450 Phase 1: any caregiver, Oncoteam session, or
    Claude.ai/ChatGPT chat can annotate a specific record with a personal
    observation (\"Asked Dr. Kovac about this CEA, monitoring\"). Notes surface
    in the dashboard + future clinical-timeline view.

    Args:
        record_id: Target clinical record id.
        note_text: Free-form annotation.
        source: 'dashboard' | 'mcp-claude' | 'mcp-chatgpt' | 'oncoteam' | 'manual'.
        tags: List of tag strings — 'flagged', 'ask-oncologist', 'pre-cycle-3',
            'side-effect', etc. Stored as a JSON array for queryability.
        session_id: MCP conversation UUID — links back to the chat that wrote
            the note (future \"view original conversation\" UI).
        mcp_conversation_ref: Opaque reference if the LLM thread is recoverable.
        caller_identity: Token hash / OAuth sub — accountability.
        created_by: Email of the actor.
        patient_slug: Validates the target record belongs to the targeted
            patient before writing.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)

    target = await db.get_clinical_record(record_id, include_deleted=True)
    if target is None:
        return json.dumps({"error": "not_found", "record_id": record_id})
    if target.patient_id != pid:
        return json.dumps({"error": "wrong_patient", "record_id": record_id})

    note = ClinicalRecordNote(
        record_id=record_id,
        note_text=note_text,
        tags=json.dumps(tags, ensure_ascii=False) if tags else None,
        source=source,
        session_id=session_id,
        mcp_conversation_ref=mcp_conversation_ref,
        caller_identity=caller_identity,
        created_by=created_by,
    )
    stored = await db.insert_clinical_record_note(note)
    return json.dumps(
        {
            "status": "created",
            "id": stored.id,
            "record_id": stored.record_id,
            "created_at": stored.created_at,
            "tags": tags or [],
        },
        ensure_ascii=False,
    )


async def list_clinical_record_notes(
    ctx: Context,
    record_id: int | None = None,
    tags_any: list[str] | None = None,
    limit: int = 200,
    patient_slug: str | None = None,
) -> str:
    """List notes for a record or across all records for a patient.

    Args:
        record_id: If provided, only notes on that specific record. If omitted,
            all notes across the patient's records (via join).
        tags_any: Filter to notes whose tags JSON array contains at least one
            of the given tag strings.
        limit: Max notes to return (default 200).
        patient_slug: Resolves the patient for the cross-record path; also
            validates the patient_id when record_id is provided.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)

    if record_id is not None:
        target = await db.get_clinical_record(record_id, include_deleted=True)
        if target is None:
            return json.dumps({"error": "not_found", "record_id": record_id})
        if target.patient_id != pid:
            return json.dumps({"error": "wrong_patient", "record_id": record_id})
        notes = await db.list_clinical_record_notes(
            record_id=record_id, tags_any=tags_any, limit=limit
        )
    else:
        notes = await db.list_clinical_record_notes(patient_id=pid, tags_any=tags_any, limit=limit)
    return json.dumps(
        {
            "count": len(notes),
            "notes": [n.model_dump() for n in notes],
        },
        ensure_ascii=False,
        default=str,
    )


# ── Phase 2 tools (#450) ────────────────────────────────────────────────


async def update_clinical_record(
    ctx: Context,
    record_id: int,
    source: str,
    record_type: str | None = None,
    param: str | None = None,
    value_num: float | None = None,
    value_text: str | None = None,
    unit: str | None = None,
    status: str | None = None,
    occurred_at: str | None = None,
    source_document_id: int | None = None,
    ref_range_low: float | None = None,
    ref_range_high: float | None = None,
    metadata_json: str | None = None,
    session_id: str | None = None,
    caller_identity: str | None = None,
    changed_by: str | None = None,
    reason: str | None = None,
    patient_slug: str | None = None,
) -> str:
    """Partial update on a clinical record — only provided fields change.

    Every update writes an audit row with the before/after snapshots and the
    list of changed field names, so the caregiver or Oncoteam can later
    inspect who changed what and why. Fields not passed (left as None) are
    untouched; pass an explicit value to clear a field is not supported here
    — use the DB layer if you need that edge case.

    Args:
        record_id: Target clinical record id.
        source: Provenance tag for this update — 'manual', 'ai-extract',
            'oncoteam', 'mcp-claude', 'mcp-chatgpt'.
        record_type / param / value_num / value_text / unit / status /
        occurred_at / source_document_id / ref_range_low / ref_range_high /
        metadata_json: Fields to update. Pass only what should change.
        session_id: MCP conversation UUID — links audit row to the chat.
        caller_identity: Token hash / OAuth sub — accountability.
        changed_by: Email of the actor.
        reason: Human explanation of why this update was made (audit).
        patient_slug: Explicit patient slug per Option A (#429).
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)

    target = await db.get_clinical_record(record_id, include_deleted=True)
    if target is None:
        return json.dumps({"error": "not_found", "record_id": record_id})
    if target.patient_id != pid:
        return json.dumps({"error": "wrong_patient", "record_id": record_id})

    updates = {
        k: v
        for k, v in {
            "record_type": record_type,
            "param": param,
            "value_num": value_num,
            "value_text": value_text,
            "unit": unit,
            "status": status,
            "occurred_at": occurred_at,
            "source_document_id": source_document_id,
            "ref_range_low": ref_range_low,
            "ref_range_high": ref_range_high,
            "metadata_json": metadata_json,
        }.items()
        if v is not None
    }

    if not updates:
        return json.dumps(
            {"status": "no_change", "id": record_id, "reason": "no updatable fields provided"}
        )

    after = await db.update_clinical_record(
        record_id,
        updates,
        changed_by=changed_by,
        source=source,
        session_id=session_id,
        caller_identity=caller_identity,
        reason=reason,
    )
    return json.dumps(
        {
            "status": "updated",
            "id": record_id,
            "changed_fields": sorted(updates.keys()),
            "record": after.model_dump() if after else None,
        },
        ensure_ascii=False,
        default=str,
    )


async def delete_clinical_record(
    ctx: Context,
    record_id: int,
    source: str,
    session_id: str | None = None,
    caller_identity: str | None = None,
    deleted_by: str | None = None,
    reason: str | None = None,
    patient_slug: str | None = None,
) -> str:
    """Soft-delete a clinical record. Idempotent — already-deleted returns no_change.

    Original row is preserved with deleted_at/deleted_by set; the audit row
    captures the delete action. Recovery via restore_clinical_record within
    the 30-day retention window (per global deletion policy). Nothing is ever
    hard-deleted from this table.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)

    target = await db.get_clinical_record(record_id, include_deleted=True)
    if target is None:
        return json.dumps({"error": "not_found", "record_id": record_id})
    if target.patient_id != pid:
        return json.dumps({"error": "wrong_patient", "record_id": record_id})

    did = await db.delete_clinical_record(
        record_id,
        deleted_by=deleted_by,
        source=source,
        session_id=session_id,
        caller_identity=caller_identity,
        reason=reason,
    )
    if not did:
        return json.dumps({"status": "no_change", "id": record_id, "reason": "already_deleted"})
    return json.dumps({"status": "deleted", "id": record_id})


async def restore_clinical_record(
    ctx: Context,
    record_id: int,
    source: str,
    session_id: str | None = None,
    caller_identity: str | None = None,
    restored_by: str | None = None,
    reason: str | None = None,
    patient_slug: str | None = None,
) -> str:
    """Restore a soft-deleted clinical record. Emits a 'restore' audit row."""
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)

    target = await db.get_clinical_record(record_id, include_deleted=True)
    if target is None:
        return json.dumps({"error": "not_found", "record_id": record_id})
    if target.patient_id != pid:
        return json.dumps({"error": "wrong_patient", "record_id": record_id})
    if target.deleted_at is None:
        return json.dumps({"status": "no_change", "id": record_id, "reason": "not_deleted"})

    after = await db.restore_clinical_record(
        record_id,
        restored_by=restored_by,
        source=source,
        session_id=session_id,
        caller_identity=caller_identity,
        reason=reason,
    )
    return json.dumps(
        {"status": "restored", "id": record_id, "record": after.model_dump() if after else None},
        ensure_ascii=False,
        default=str,
    )


async def list_clinical_records(
    ctx: Context,
    record_type: str | None = None,
    param: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_deleted: bool = False,
    limit: int = 200,
    patient_slug: str | None = None,
) -> str:
    """List clinical records for the active patient, paginated + filterable.

    Args:
        record_type: Filter by type — 'lab', 'biomarker', 'finding', etc.
        param: Filter by parameter name (e.g. 'CEA', 'KRAS').
        since / until: Date bounds on occurred_at (ISO strings).
        include_deleted: If True, also returns soft-deleted rows.
        limit: Max records to return (default 200, max 2000).
        patient_slug: Explicit patient slug per Option A (#429).
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    query = ClinicalRecordQuery(
        record_type=record_type,
        param=param,
        since=since,
        until=until,
        include_deleted=include_deleted,
        limit=limit,
    )
    records = await db.list_clinical_records(query, patient_id=pid)
    return json.dumps(
        {
            "count": len(records),
            "records": [r.model_dump() for r in records],
        },
        ensure_ascii=False,
        default=str,
    )


async def get_record_audit(
    ctx: Context,
    record_id: int,
    limit: int = 200,
    patient_slug: str | None = None,
) -> str:
    """Full chronological audit history for a single clinical record.

    Returns every create / update / delete / restore event newest-first.
    Each entry has before_json + after_json snapshots + changed_fields list
    so the caller can render a git-style diff view.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)

    target = await db.get_clinical_record(record_id, include_deleted=True)
    if target is None:
        return json.dumps({"error": "not_found", "record_id": record_id})
    if target.patient_id != pid:
        return json.dumps({"error": "wrong_patient", "record_id": record_id})

    audit = await db.list_clinical_record_audit(record_id, limit=limit)
    return json.dumps(
        {
            "record_id": record_id,
            "count": len(audit),
            "audit": [a.model_dump() for a in audit],
        },
        ensure_ascii=False,
        default=str,
    )


async def add_clinical_analysis(
    ctx: Context,
    analysis_type: str,
    result_json: str,
    produced_by: str,
    record_ids: list[int] | None = None,
    result_summary: str | None = None,
    tags: list[str] | None = None,
    session_id: str | None = None,
    patient_slug: str | None = None,
) -> str:
    """Persist an analytic output computed over one or more clinical records.

    Oncoteam's primary write path for SII trends, lab deltas, biomarker
    safety checks, session summaries, trial-eligibility notes, etc. The
    ``record_ids`` list traces which facts fed this analysis — the caregiver
    (or a future dashboard timeline) can drill from the analysis back to
    the underlying facts.

    Args:
        analysis_type: Shape of the analysis — 'sii_trend' | 'ne_ly_ratio' |
            'lab_delta' | 'biomarker_safety_check' | 'trial_eligibility' |
            'session_note' | 'precycle_checklist' | 'custom'.
        result_json: JSON-serialised analytic payload (free-form per type).
        produced_by: 'oncoteam' | 'oncofiles-internal' | 'external-ai' | 'manual'.
        record_ids: Optional list of clinical_records.id that fed this analysis.
            Validated to belong to the targeted patient.
        result_summary: One-line human summary for list views.
        tags: JSON array of tag strings for later filtering.
        session_id: MCP conversation UUID for traceback.
        patient_slug: Explicit patient slug per Option A (#429).
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)

    # Defence-in-depth: every record_id must belong to this patient so an
    # external AI can't stitch together an analysis across patients.
    if record_ids:
        for rid in record_ids:
            rec = await db.get_clinical_record(rid, include_deleted=True)
            if rec is None:
                return json.dumps({"error": "record_not_found", "record_id": rid})
            if rec.patient_id != pid:
                return json.dumps({"error": "wrong_patient", "record_id": rid})

    analysis = ClinicalAnalysis(
        patient_id=pid,
        record_ids=json.dumps(record_ids) if record_ids else None,
        analysis_type=analysis_type,
        result_json=result_json,
        result_summary=result_summary,
        tags=json.dumps(tags, ensure_ascii=False) if tags else None,
        produced_by=produced_by,
        session_id=session_id,
    )
    stored = await db.insert_clinical_analysis(analysis)
    return json.dumps(
        {
            "status": "created",
            "id": stored.id,
            "analysis_type": stored.analysis_type,
            "produced_by": stored.produced_by,
            "created_at": stored.created_at,
            "record_ids": record_ids or [],
            "tags": tags or [],
        },
        ensure_ascii=False,
        default=str,
    )


async def search_notes(
    ctx: Context,
    query: str,
    limit: int = 100,
    patient_slug: str | None = None,
) -> str:
    """LIKE-based search over all active notes for the patient.

    Covers the common caregiver recall case: 'what did we write about CEA?'.
    Case-insensitive via SQLite's default LIKE. FTS5 upgrade deferred
    until per-patient note count exceeds ~500 (see #450).

    Args:
        query: Substring to match inside note_text.
        limit: Max notes to return (default 100).
        patient_slug: Explicit patient slug per Option A (#429).
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)

    if not query or not query.strip():
        return json.dumps({"error": "empty_query"})

    notes = await db.search_clinical_record_notes(patient_id=pid, query=query, limit=limit)
    return json.dumps(
        {
            "query": query,
            "count": len(notes),
            "notes": [n.model_dump() for n in notes],
        },
        ensure_ascii=False,
        default=str,
    )


def register(mcp):
    mcp.tool()(add_clinical_record)
    mcp.tool()(get_clinical_record)
    mcp.tool()(add_clinical_record_note)
    mcp.tool()(list_clinical_record_notes)
    # Phase 2 (#450, v5.13)
    mcp.tool()(update_clinical_record)
    mcp.tool()(delete_clinical_record)
    mcp.tool()(restore_clinical_record)
    mcp.tool()(list_clinical_records)
    mcp.tool()(get_record_audit)
    mcp.tool()(add_clinical_analysis)
    mcp.tool()(search_notes)
