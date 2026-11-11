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
    db = _get_db(ctx)
    # Hydrate from the stored row before merging (#713): on a cold cache the
    # merge would otherwise start from the default context and save
    # default + update over the patient's stored context. DB errors propagate.
    await patient_context.ensure_loaded(db.db, pid)
    updated = patient_context.update_context(updates, patient_id=pid)
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
    """List the patients you can access.

    Scoping (#487 / #483 / #590): never more than the caller's own access.
    OAuth callers (admin or not) see only patients whose `caregiver_email`
    matches their Google email; patient-bearer (`onco_*`) and stdio callers
    see only their bound patient; the static operator bearer keeps the full
    list for server-to-server use. See `_accessible_patients`.
    """
    from oncofiles.tools._helpers import _accessible_patients

    db = _get_db(ctx)
    current_pid = _get_patient_id(required=False)
    patients = await _accessible_patients(db)

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
    patient's data. The selection persists across requests for OAuth callers
    who are a caregiver of the selected patient.

    Access (#590): the target goes through the same ACL as every other
    patient-scoped tool (`_resolve_patient_id`). An unknown slug and a slug
    you may not access return the same error.

    Args:
        patient_slug: Patient slug or UUID (e.g. 'q1b', 'e5g').
    """
    from oncofiles.persistent_oauth import _email_matches_caregiver, _verified_patient_id
    from oncofiles.tools._helpers import _caller_email, _patient_not_accessible_message

    db = _get_db(ctx)

    # Accept a UUID as well as a slug, then run the ACL on the slug. Both
    # failure paths return the identical message (no existence oracle).
    target = None
    for p in await db.list_patients(active_only=True):
        if p.slug == patient_slug or p.patient_id == patient_slug:
            target = p
            break
    if target is None:
        return json.dumps({"error": _patient_not_accessible_message(patient_slug)})
    try:
        await _resolve_patient_id(target.slug, ctx)
    except ValueError:
        return json.dumps({"error": _patient_not_accessible_message(patient_slug)})

    # Persist the selection under the caller's own OAuth email, and only when
    # that email is a caregiver of the target: `_resolve_oauth_patient` ignores
    # any other stored selection. Never write a selection for someone else's
    # email (the old fallback keyed it by a Drive token's owner_email, so an
    # operator call could change another user's default patient).
    caller_email = _caller_email().strip()
    persisted_across_requests = False
    if caller_email and _email_matches_caregiver(caller_email, target.caregiver_email):
        await db.set_patient_selection(caller_email, target.patient_id)
        persisted_across_requests = True

    # Also update the ContextVar for immediate effect in this request
    _verified_patient_id.set(target.patient_id)

    ctx_data = patient_context.get_context(target.patient_id)
    doc_count = await db.count_documents(patient_id=target.patient_id)

    # #408: when the selection is not persisted (bearer-token auth in
    # stateless HTTP, or an OAuth account that is not a caregiver of the
    # target), say so instead of a misleading plain "switched".
    if persisted_across_requests:
        note = "Selection persisted. All subsequent tool calls will use this patient's data."
        status = "switched"
    else:
        note = (
            "Selection applied to THIS request only. It cannot persist across "
            "requests in stateless HTTP for this identity (bearer token, or an "
            "OAuth account that is not a caregiver of this patient). Subsequent "
            "tool calls revert to your bound patient unless you pass "
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


async def whoami(ctx: Context) -> str:
    """Show who you are calling as: identity, auth method, admin flag, patients.

    Read-only, no PHI (#604). Use it to understand why a patient is or is not
    visible ("why can't I see patient X?").

    Returns:
        auth_method: "oauth" (Claude.ai / ChatGPT connector), "static_bearer"
            (operator token), "patient_bearer" (onco_* patient token),
            "stdio" (local process) or "none".
        email: the Google email bound to an OAuth connection, else null.
        is_admin: whether admin-only tools are available to this caller.
        bound_patient: slug + name of the patient tool calls default to, or null.
        patients: slug + display name of every patient you may list
            (the same scope as list_patients).
    """
    from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
    from oncofiles.patient_middleware import get_current_patient_id
    from oncofiles.tools._helpers import (
        _accessible_patients,
        _caller_auth_method,
        _caller_email,
        _is_admin_caller,
    )

    db = _get_db(ctx)
    auth_method = _caller_auth_method()
    if not auth_method:
        transport = ctx.request_context.lifespan_context.get("transport", "")
        auth_method = "stdio" if transport == "stdio" else "none"

    patients = await _accessible_patients(db)
    bound_pid = get_current_patient_id()
    if bound_pid == NO_PATIENT_ACCESS_SENTINEL:
        bound_pid = ""
    # Report the bound patient only when it is inside the listed scope, so
    # whoami can never reveal a patient that list_patients would hide.
    bound = next((p for p in patients if p.patient_id == bound_pid), None)

    is_admin = _is_admin_caller()
    result: dict = {
        "auth_method": auth_method,
        "email": _caller_email().strip() or None,
        "is_admin": is_admin,
        "bound_patient": {"slug": bound.slug, "name": bound.display_name} if bound else None,
        "patients": [{"slug": p.slug, "name": p.display_name} for p in patients],
    }
    if not patients:
        result["guidance"] = (
            "No patients are linked to this identity. Sign in at "
            "https://oncofiles.com/dashboard with the Google account that is the "
            "patient's caregiver, or ask the patient's caregiver to add your email."
        )
    elif is_admin and auth_method == "oauth":
        result["note"] = (
            "Admin scope: admin tools are available and a known patient_slug can be "
            "passed to any tool. Only your own caregiver patients are listed (#590)."
        )
    return json.dumps(result, ensure_ascii=False, indent=2)


READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}


def register(mcp):
    mcp.tool()(get_patient_context)
    mcp.tool()(update_patient_context)
    mcp.tool()(list_patients)
    mcp.tool()(select_patient)
    mcp.tool(annotations=READ_ONLY)(whoami)
