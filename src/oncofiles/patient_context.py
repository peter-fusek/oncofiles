"""Patient context: load, save, and access patient clinical data.

Load order: DB → JSON file → hardcoded default.
Updates are persisted to DB (works on Railway where filesystem is ephemeral).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Minimal fallback — real clinical data is loaded from DB or JSON file at runtime.
# NEVER commit patient-specific data (diagnosis, biomarkers, physicians) to this repo.
_DEFAULT_CONTEXT: dict[str, Any] = {
    "name": os.environ.get("PATIENT_NAME", ""),
    "patient_type": "oncology",  # "oncology" or "general"
    "date_of_birth": "",  # ISO date, e.g. "1980-01-15"
    "sex": "",  # "male" or "female"
    "diagnosis": "",
    "staging": "",
    "histology": "",
    "tumor_site": "",
    "diagnosis_date": "",
    "biomarkers": {},
    "treatment": {},
    "metastases": [],
    "comorbidities": [],
    "surgeries": [],
    "physicians": {},
    "excluded_therapies": [],
    "note": "",
}

# Per-patient context cache — keyed by patient_id
_contexts: dict[str, dict[str, Any]] = {}
# Legacy alias for backward compat during migration
_context: dict[str, Any] = {}


def get_context(patient_id: str | None = None) -> dict[str, Any]:
    """Return the patient context dict.

    Resolution order:
    1. Explicit patient_id argument
    2. Current request ContextVar (set by PatientResolutionMiddleware)
    3. Legacy global _context (backward compat / startup / tests)
    """
    pid = patient_id
    if not pid:
        try:
            from oncofiles.patient_middleware import get_current_patient_id

            pid = get_current_patient_id()
        except (ImportError, LookupError):
            pass  # startup or test context without middleware
    if pid and pid in _contexts:
        return _contexts[pid]
    # Fallback to legacy global for backward compat
    return _context if _context else _DEFAULT_CONTEXT.copy()


def get_patient_name(patient_id: str | None = None) -> str:
    """Return the patient's name from context."""
    return get_context(patient_id).get("name", "")


def load_from_json(path: str | Path) -> dict[str, Any]:
    """Load patient context from a JSON file."""
    p = Path(path)
    if p.exists():
        data = json.loads(p.read_text())
        _context.update(data)
        logger.info("Patient context loaded from %s", p)
        return _context
    return {}


async def load_from_db(db: Any, patient_id: str | None = None) -> dict[str, Any]:
    """Load patient context from the database (patient_context table).

    If patient_id is given, loads that patient's context.
    Otherwise loads the legacy id=1 row (backward compat).
    """
    try:
        if patient_id:
            async with db.execute(
                "SELECT context_json FROM patient_context WHERE patient_id = ?",
                (patient_id,),
            ) as cursor:
                row = await cursor.fetchone()
        else:
            async with db.execute(
                "SELECT context_json FROM patient_context WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
        if row:
            data_str = row["context_json"] if isinstance(row, dict) else row[0]
            data = json.loads(data_str)
            if patient_id:
                _contexts[patient_id] = data
            _context.update(data)
            logger.info(
                "Patient context loaded from database (patient_id=%s)", patient_id or "legacy"
            )
            return data
    except Exception:
        logger.debug("No patient context in database (table may not exist yet)")
    return {}


async def save_to_db(
    db: Any, context: dict[str, Any] | None = None, *, patient_id: str | None = None
) -> None:
    """Save patient context to the database. Per-patient if patient_id given."""
    data = context or (get_context(patient_id) if patient_id else _context)
    json_data = json.dumps(data, ensure_ascii=False)
    if patient_id:
        await db.execute(
            """
            INSERT INTO patient_context (patient_id, context_json, updated_at)
            VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(patient_id) DO UPDATE SET
                context_json = excluded.context_json,
                updated_at = excluded.updated_at
            """,
            (patient_id, json_data),
        )
    else:
        await db.execute(
            """
            INSERT INTO patient_context (id, context_json, updated_at)
            VALUES (1, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(id) DO UPDATE SET
                context_json = excluded.context_json,
                updated_at = excluded.updated_at
            """,
            (json_data,),
        )
    await db.commit()
    if patient_id:
        _contexts[patient_id] = data


def update_context(updates: dict[str, Any], patient_id: str | None = None) -> dict[str, Any]:
    """Merge updates into the context. Returns the updated context."""
    ctx = get_context(patient_id).copy()
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(ctx.get(key), dict):
            ctx[key].update(value)
        else:
            ctx[key] = value
    if patient_id:
        _contexts[patient_id] = ctx
    _context.update(ctx)
    return ctx


async def initialize(db: Any, json_path: str | Path | None = None) -> dict[str, Any]:
    """Load patient context: DB → JSON file → hardcoded default.

    Called once at server startup.
    """
    # 1. Try DB first (persisted updates take priority)
    result = await load_from_db(db)
    if result:
        return result

    # 2. Try JSON file
    if json_path:
        result = load_from_json(json_path)
        if result:
            return result

    # 3. Fall back to hardcoded default
    _context.update(_DEFAULT_CONTEXT)
    logger.info("Patient context loaded from hardcoded default")
    return _context


def format_context_text(patient_id: str | None = None) -> str:
    """Format patient context as a human-readable string for tool output."""
    ctx = get_context(patient_id)
    bio = ctx.get("biomarkers", {})
    biomarkers = "\n".join(f"  - {k}: {v}" for k, v in bio.items())
    mets = ", ".join(ctx.get("metastases", []))
    comorb = ", ".join(ctx.get("comorbidities", []))
    excluded = "\n".join(f"  - {t}" for t in ctx.get("excluded_therapies", []))
    tx = ctx.get("treatment", {})
    phys = ctx.get("physicians", {})
    patient_type = ctx.get("patient_type", "oncology")
    lines = [
        f"**Patient:** {ctx.get('name', 'Unknown')}",
        f"**Type:** {patient_type}",
    ]
    if ctx.get("date_of_birth"):
        lines.append(f"**Date of birth:** {ctx['date_of_birth']}")
    if ctx.get("sex"):
        lines.append(f"**Sex:** {ctx['sex']}")

    if patient_type == "oncology":
        lines.extend(
            [
                f"**Diagnosis:** {ctx.get('diagnosis', '')}",
                f"**Staging:** {ctx.get('staging', '')}",
                f"**Histology:** {ctx.get('histology', '')}",
                f"**Tumor site:** {ctx.get('tumor_site', '')}",
                f"**Biomarkers:**\n{biomarkers}",
                f"**Treatment:** {tx.get('regimen', '')} (cycle {tx.get('current_cycle', '?')}) "
                f"at {tx.get('institution', '')}",
                f"**Metastases:** {mets}",
            ]
        )
    else:
        if ctx.get("diagnosis"):
            lines.append(f"**Conditions:** {ctx.get('diagnosis', '')}")

    lines.extend(
        [
            f"**Comorbidities:** {comorb}",
            f"**Physicians:** {phys.get('treating', '')}; {phys.get('admitting', '')}",
        ]
    )

    if patient_type == "oncology":
        lines.append(f"**Excluded therapies:**\n{excluded}")

    lines.append(f"**Note:** {ctx.get('note', '')}")
    return "\n".join(lines)
