"""MCP tools for clinical_records + notes (#450 Phase 1).

Session 2 ships four of the eleven tools from #450. Remaining seven (update,
soft-delete, audit-history view, search_notes, etc.) deferred to v5.12 so we
can validate the data model in production against real Oncoteam workloads
before growing the surface area.

All four tools accept ``patient_slug`` per the Option A pattern (#429) —
required in stateless-HTTP contexts (Claude.ai connector, ChatGPT) where
``select_patient()`` state does not persist across tool calls.
"""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import ClinicalRecord, ClinicalRecordNote, ClinicalRecordQuery
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


# Stitched together here so ClinicalRecordQuery stays imported for future tools
# (list_clinical_records ships in v5.12 — it'd be premature to ship it without
# the dashboard view that consumes it).
_ = ClinicalRecordQuery  # keep import used until v5.12 list tool lands


def register(mcp):
    mcp.tool()(add_clinical_record)
    mcp.tool()(get_clinical_record)
    mcp.tool()(add_clinical_record_note)
    mcp.tool()(list_clinical_record_notes)
