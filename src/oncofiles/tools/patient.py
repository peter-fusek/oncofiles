"""Patient context tools — get and update patient clinical data."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles import patient_context
from oncofiles.tools._helpers import _get_db, _get_patient_id, _resolve_patient_id


async def get_patient_context(ctx: Context, patient_slug: str | None = None) -> str:
    """Get the current patient clinical context.

    Returns structured patient data including diagnosis, biomarkers,
    treatment, metastases, comorbidities, and excluded therapies.

    Scoping (#493 + #497/#498): admin callers see every patient; non-admin
    OAuth callers see only patients whose `caregiver_email` matches the
    caller's Google email; patient-bearer (`onco_*`) callers see only the
    single patient their token is bound to. Unauthorized slug lookups return
    the same empty-plus-guidance shape as `list_patients` — never PHI
    (name, DOB, surgeries, etc.).

    Implementation note: ACL is enforced centrally by `_resolve_patient_id`
    (#497/#498). When that resolver raises an "access denied" ValueError,
    this tool intentionally swallows it and returns the empty-guidance shape
    documented in #493 — the resolver's raised error is the canonical signal
    everywhere else, but get_patient_context preserves the graceful shape
    for chat-client UX (claude.ai / dashboard) so an unauthorized probe
    looks like "no patients" rather than an ugly error.

    Args:
        patient_slug: Optional — explicit patient slug (e.g. 'q1b'). Required
            in stateless HTTP contexts (Claude.ai connector, ChatGPT) where
            select_patient() state does not persist across tool calls (#429).
            Stdio + single-patient bearer flows can omit.
    """
    from oncofiles.tools._helpers import _with_clinical_disclaimer

    try:
        pid = await _resolve_patient_id(patient_slug, ctx)
    except ValueError as exc:
        # The resolver raises ValueError for both "Patient not found" and
        # "access denied". Either way, return the canonical empty shape so
        # we never leak existence/PHI through error message variation.
        if "access denied" in str(exc) or "Patient not found" in str(exc):
            return json.dumps(
                {
                    "patients": [],
                    "guidance": (
                        "No patient access resolved for this slug. "
                        "Use list_patients() to see patients you can access. "
                        "If you expect access, ensure your Google email appears in "
                        "the patient's caregiver_email on the dashboard."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        raise

    # Try loading from DB if not cached yet
    ctx_data = patient_context.get_context(pid)
    if not ctx_data or not ctx_data.get("name"):
        db = _get_db(ctx)
        ctx_data = await patient_context.load_from_db(db.db, patient_id=pid)
        if not ctx_data:
            ctx_data = patient_context.get_context(pid)
    return json.dumps(_with_clinical_disclaimer(ctx_data or {}), ensure_ascii=False, indent=2)


async def update_patient_context(
    ctx: Context,
    updates_json: str,
    patient_slug: str | None = None,
) -> str:
    """Update specific fields in the patient clinical context.

    Merges the provided updates into the current context. Nested dicts
    (like biomarkers, treatment, physicians) are merged recursively.
    Persisted to database for durability.

    Args:
        updates_json: JSON object with fields to update. Example:
            '{"treatment": {"current_cycle": 3}}'
        patient_slug: Optional — explicit patient slug (e.g. 'q1b'). Required
            in stateless HTTP contexts (Claude.ai connector, ChatGPT) where
            select_patient() state does not persist across tool calls (#429).
    """
    try:
        updates = json.loads(updates_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    if not isinstance(updates, dict):
        return json.dumps({"error": "updates_json must be a JSON object"})

    pid = await _resolve_patient_id(patient_slug, ctx)
    updated = patient_context.update_context(updates, patient_id=pid)
    db = _get_db(ctx)
    await patient_context.save_to_db(db.db, updated, patient_id=pid)

    return json.dumps(
        {
            "status": "updated",
            "updated_fields": list(updates.keys()),
            "patient_name": updated.get("name", ""),
            "patient_slug": patient_slug or "current",
        }
    )


async def list_patients(ctx: Context) -> str:
    """List available patients.

    Scoping (#487 / #483): admin callers see every active patient; non-admin
    callers see only patients whose `caregiver_email` matches the caller's
    OAuth-bound Google email. Patient-bearer (`onco_*`) tokens see only the
    single patient their token is bound to.
    """
    from oncofiles.persistent_oauth import _email_matches_caregiver
    from oncofiles.tools._helpers import _caller_email, _is_admin_caller

    db = _get_db(ctx)
    current_pid = _get_patient_id(required=False)
    patients = await db.list_patients(active_only=True)

    # Apply caller-scope filter BEFORE building the response.
    if not _is_admin_caller():
        caller_email = _caller_email()
        if caller_email:
            # OAuth caller: show only patients whose caregiver_email matches.
            patients = [
                p for p in patients if _email_matches_caregiver(caller_email, p.caregiver_email)
            ]
        elif current_pid:
            # No email (patient bearer or stdio): restrict to the bound patient.
            patients = [p for p in patients if p.patient_id == current_pid]
        else:
            # No identity at all: refuse to enumerate.
            patients = []

    if not patients:
        return json.dumps(
            {
                "patients": [],
                "guidance": (
                    "No patients found. To get started:\n"
                    "1. Go to https://oncofiles.com/dashboard\n"
                    "2. Sign in with Google\n"
                    "3. Click '+ New Patient' to create your first patient\n"
                    "4. Connect Google Drive to start uploading documents"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    result = []
    for p in patients:
        doc_count = await db.count_documents(patient_id=p.patient_id)
        ctx_data = patient_context.get_context(p.patient_id)
        result.append(
            {
                "patient_id": p.patient_id,
                "slug": p.slug,
                "name": p.display_name,
                "patient_type": ctx_data.get("patient_type", "oncology"),
                "documents": doc_count,
                "is_current": p.patient_id == current_pid,
            }
        )
    return json.dumps(result, ensure_ascii=False, indent=2)


async def select_patient(
    ctx: Context,
    patient_slug: str,
) -> str:
    """Switch to a different patient for this connection.

    After calling this, all subsequent tool calls will use the selected
    patient's data. The selection persists across requests.

    Args:
        patient_slug: Patient slug or UUID (e.g. 'q1b', 'e5g').
    """
    db = _get_db(ctx)

    # Resolve slug to patient_id

    patients = await db.list_patients(active_only=True)
    target = None
    for p in patients:
        if p.slug == patient_slug or p.patient_id == patient_slug:
            target = p
            break
    if not target:
        return json.dumps(
            {"error": f"Patient '{patient_slug}' not found. Use list_patients to see available."}
        )

    # Store selection — keyed by the owner_email from the current patient's OAuth token
    current_pid = _get_patient_id(required=False)
    token = await db.get_oauth_token(patient_id=current_pid)
    owner_email = token.owner_email if token else None

    if not owner_email:
        # Try the target patient's token
        token = await db.get_oauth_token(patient_id=target.patient_id)
        owner_email = token.owner_email if token else None

    persisted_across_requests = False
    if owner_email:
        await db.set_patient_selection(owner_email, target.patient_id)
        persisted_across_requests = True

    # Also update the ContextVar for immediate effect in this request
    from oncofiles.persistent_oauth import _verified_patient_id

    _verified_patient_id.set(target.patient_id)

    ctx_data = patient_context.get_context(target.patient_id)
    doc_count = await db.count_documents(patient_id=target.patient_id)

    # #408: when the session has no OAuth owner_email (e.g. bearer-token auth
    # in stateless HTTP), the selection is NOT persisted across requests.
    # Returning a plain "switched" response was misleading — subsequent
    # calls like list_patients / search_documents would still scope to the
    # bearer's bound patient. Honest contract: tell the caller what will
    # and will NOT work.
    if persisted_across_requests:
        note = "Selection persisted. All subsequent tool calls will use this patient's data."
        status = "switched"
    else:
        note = (
            "Selection applied to THIS request only. The session is authenticated "
            "via a patient-scoped bearer token (not OAuth), so select_patient "
            "cannot persist across requests in stateless HTTP. Subsequent tool "
            "calls will revert to the bearer-bound patient unless you pass "
            "patient_slug explicitly on each call (Option A, #429)."
        )
        status = "switched_single_request"

    return json.dumps(
        {
            "status": status,
            "patient_id": target.patient_id,
            "slug": target.slug,
            "name": target.display_name,
            "patient_type": ctx_data.get("patient_type", "oncology"),
            "documents": doc_count,
            "persisted_across_requests": persisted_across_requests,
            "note": note,
        },
        ensure_ascii=False,
    )


def register(mcp):
    mcp.tool()(get_patient_context)
    mcp.tool()(update_patient_context)
    mcp.tool()(list_patients)
    mcp.tool()(select_patient)
