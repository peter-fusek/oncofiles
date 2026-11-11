"""Human verification of facts (#636): verify_fact, correct_fact, unverify_fact.

A person reviewing a fact in Claude.ai (or any MCP client) confirms it, fixes
it, or takes a confirmation back. Facts a person verified or corrected are kept
by re-extraction (``replace_document_facts``) and count in statistics.

* The patient comes from ``_resolve_patient_id`` (token binding or an allowed
  ``patient_slug``); a fact of another patient is reported as not found.
* These tools write: no ``readOnlyHint``, ``destructiveHint`` false (a
  correction soft-deletes the old version, nothing is ever lost).
* Every call writes an ``activity_log`` row with ids and statuses only, never
  values, notes or reasons (those live in the patient-scoped audit/provenance).
* The acting person (``agent_user``) is the caller's OAuth-bound email, as the
  existing audit columns store it (``clinical_records.created_by``,
  ``curated_nightly.updated_by``); callers without an email are recorded by
  their kind of credential.
"""

from __future__ import annotations

import json
import logging
import time

from fastmcp import Context

from oncofiles.models import ActivityLogEntry
from oncofiles.tools._helpers import (
    _caller_email,
    _get_db,
    _is_admin_caller,
    _resolve_patient_id,
)

logger = logging.getLogger(__name__)

WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
_FACT_KEYS = (
    "id", "fhir_id", "version_id", "code", "param", "occurred_at", "value_num", "value_text",
    "unit", "value_canonical", "unit_canonical", "flag", "verification", "source_document_id",
    "page_number", "evidence",
)  # fmt: skip


def _agent_user() -> str:
    email = _caller_email().strip().lower()
    if email:
        return email
    try:
        from oncofiles.persistent_oauth import _verified_patient_id

        bound = _verified_patient_id.get()
    except Exception:
        bound = None
    if _is_admin_caller():
        return "mcp-bearer:operator"
    if bound:
        return "mcp-bearer:patient"
    return "local"


def _session_id(ctx: Context) -> str:
    try:
        return str(ctx.session_id or "") or "fact-review"
    except Exception:
        return "fact-review"


async def _log(ctx, db, *, pid, tool, inputs, outputs, status, started) -> None:
    """One activity_log row per call; ids and statuses only."""
    entry = ActivityLogEntry(
        patient_id=pid or "",
        session_id=_session_id(ctx),
        agent_id="mcp",
        tool_name=tool,
        input_summary=json.dumps(inputs, default=str),
        output_summary=json.dumps(outputs, default=str),
        duration_ms=int((time.monotonic() - started) * 1000),
        status=status,
        tags='["fact_review"]',
    )
    try:
        await db.insert_activity_log(entry)
    except Exception:  # the review itself is already committed and audited
        logger.exception("fact_review: activity_log write failed")


def _fact_view(row: dict | None) -> dict | None:
    return None if row is None else {k: row.get(k) for k in _FACT_KEYS}


async def _run(ctx, tool: str, fact_id, patient_slug, action) -> str:
    """Resolve the patient, run ``action(db, pid, agent)``, log the call."""
    started = time.monotonic()
    db = _get_db(ctx)
    inputs = {"fact_id": fact_id}
    try:
        pid = await _resolve_patient_id(patient_slug, ctx)
    except ValueError:
        await _log(ctx, db, pid="", tool=tool, inputs=inputs, outputs={}, status="denied",
                   started=started)  # fmt: skip
        raise
    try:
        result = await action(db, pid, _agent_user())
    except ValueError as exc:
        await _log(ctx, db, pid=pid, tool=tool, inputs=inputs, outputs={"error": "invalid"},
                   status="error", started=started)  # fmt: skip
        return json.dumps({"error": "invalid_input", "detail": str(exc), "fact_id": fact_id})
    status = result["status"]
    fact = result.get("fact")
    outputs = {
        "status": status,
        "fact_id": fact["id"] if fact else None,
        "old_fact_id": result["old"]["id"] if result.get("old") else None,
        "provenance_id": result.get("provenance_id"),
    }
    await _log(ctx, db, pid=pid, tool=tool, inputs=inputs, outputs=outputs,
               status="ok" if status != "not_found" else "not_found", started=started)  # fmt: skip
    if status == "not_found":
        return json.dumps({"error": "not_found", "fact_id": fact_id})
    out = {"status": status, "fact": _fact_view(fact)}
    if result.get("old"):
        out["previous_version"] = _fact_view(result["old"])
    if result.get("provenance_id") is not None:
        out["provenance_id"] = result["provenance_id"]
    return json.dumps(out, ensure_ascii=False, default=str)


async def verify_fact(
    ctx: Context,
    fact_id: int | str,
    patient_slug: str | None = None,
    note: str | None = None,
) -> str:
    """Confirm that a fact matches its source document (sets verification='verified').

    Use after a person checked the value against the original (``document_detail``
    or ``view_document`` shows the source page and the evidence line). Verified
    facts are kept on re-extraction and count in statistics. A corrected fact is
    already human-entered and is left as is.

    Args:
        fact_id: The fact's integer id (``id`` in fact_series / document_detail),
            or its stable ``fhir_id``.
        patient_slug: Explicit patient; defaults to the caller's bound patient.
        note: Optional short note for the audit trail (e.g. "checked p.2").
    """

    async def action(db, pid, agent):
        return await db.verify_fact(fact_id, patient_id=pid, agent_user=agent, note=note)

    return await _run(ctx, "verify_fact", fact_id, patient_slug, action)


async def unverify_fact(
    ctx: Context,
    fact_id: int | str,
    patient_slug: str | None = None,
    note: str | None = None,
) -> str:
    """Take back a verification: the fact returns to its state before ``verify_fact``.

    For a wrong value use ``correct_fact`` instead. Corrected facts cannot be
    unverified (correct them again).

    Args:
        fact_id: The fact's integer id or its stable ``fhir_id``.
        patient_slug: Explicit patient; defaults to the caller's bound patient.
        note: Optional short note for the audit trail.
    """

    async def action(db, pid, agent):
        return await db.unverify_fact(fact_id, patient_id=pid, agent_user=agent, note=note)

    return await _run(ctx, "unverify_fact", fact_id, patient_slug, action)


async def correct_fact(
    ctx: Context,
    fact_id: int | str,
    reason: str,
    value_num: float | None = None,
    value_text: str | None = None,
    unit: str | None = None,
    occurred_at: str | None = None,
    patient_slug: str | None = None,
) -> str:
    """Correct a fact's value, unit or date. Never edits in place.

    The old version is soft-deleted (kept in the audit trail) and a new version
    is created with verification='corrected', the next version_id, the same
    stable id and the same source document, page and evidence line. Give
    ``value_num`` OR ``value_text`` (or neither, to fix only unit/date).

    Args:
        fact_id: The fact's integer id or its stable ``fhir_id``.
        reason: Why the value is wrong (required, stored in the audit trail).
        value_num: Corrected numeric value.
        value_text: Corrected text value (e.g. "negative").
        unit: Corrected unit as printed.
        occurred_at: Corrected sample/event date, ISO (YYYY-MM-DD or datetime).
        patient_slug: Explicit patient; defaults to the caller's bound patient.
    """

    async def action(db, pid, agent):
        return await db.correct_fact(
            fact_id, patient_id=pid, agent_user=agent, reason=reason, value_num=value_num,
            value_text=value_text, unit=unit, occurred_at=occurred_at,
        )  # fmt: skip

    return await _run(ctx, "correct_fact", fact_id, patient_slug, action)


def register(mcp):
    mcp.tool(annotations={**WRITE, "idempotentHint": True})(verify_fact)
    mcp.tool(annotations={**WRITE, "idempotentHint": True})(unverify_fact)
    mcp.tool(annotations={**WRITE, "idempotentHint": False})(correct_fact)
