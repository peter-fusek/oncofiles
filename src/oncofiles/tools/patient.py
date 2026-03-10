"""Patient context tools — get and update patient clinical data."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles import patient_context
from oncofiles.tools._helpers import _get_db


async def get_patient_context(ctx: Context) -> str:
    """Get the current patient clinical context.

    Returns structured patient data including diagnosis, biomarkers,
    treatment, metastases, comorbidities, and excluded therapies.
    """
    return json.dumps(patient_context.get_context(), ensure_ascii=False, indent=2)


async def update_patient_context(
    ctx: Context,
    updates_json: str,
) -> str:
    """Update specific fields in the patient clinical context.

    Merges the provided updates into the current context. Nested dicts
    (like biomarkers, treatment, physicians) are merged recursively.
    Persisted to database for durability.

    Args:
        updates_json: JSON object with fields to update. Example:
            '{"treatment": {"current_cycle": 3}, "comorbidities": ["[CLINICAL_REDACTED] (resolved)"]}'
    """
    try:
        updates = json.loads(updates_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    if not isinstance(updates, dict):
        return json.dumps({"error": "updates_json must be a JSON object"})

    updated = patient_context.update_context(updates)
    db = _get_db(ctx)
    await patient_context.save_to_db(db.db, updated)

    return json.dumps({
        "status": "updated",
        "updated_fields": list(updates.keys()),
        "patient_name": updated.get("name", ""),
    })


def register(mcp):
    mcp.tool()(get_patient_context)
    mcp.tool()(update_patient_context)
