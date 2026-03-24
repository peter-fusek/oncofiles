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
# NEVER commit diagnosis, biomarkers, physicians, or treatment data to this public repo.
_DEFAULT_CONTEXT: dict[str, Any] = {
    "name": os.environ.get("PATIENT_NAME", "Erika Fusekova"),
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
    "note": (
        "Lab values should be interpreted considering active chemotherapy. "
        "Key markers: CEA, CA 19-9, liver (ALT, AST, bilirubin), "
        "renal (creatinine, urea), blood counts (WBC, neutrophils, Hb, platelets). "
        "[CLINICAL_REDACTED] — bevacizumab is HIGH RISK."
    ),
}

# Module-level mutable context — loaded at startup, updated via tools
_context: dict[str, Any] = {}


def get_context() -> dict[str, Any]:
    """Return the current patient context dict."""
    return _context if _context else _DEFAULT_CONTEXT.copy()


def get_patient_name() -> str:
    """Return the patient's name from context."""
    return get_context().get("name", "")


def load_from_json(path: str | Path) -> dict[str, Any]:
    """Load patient context from a JSON file."""
    p = Path(path)
    if p.exists():
        data = json.loads(p.read_text())
        _context.update(data)
        logger.info("Patient context loaded from %s", p)
        return _context
    return {}


async def load_from_db(db: Any) -> dict[str, Any]:
    """Load patient context from the database (patient_context table)."""
    try:
        async with db.execute("SELECT context_json FROM patient_context WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            if row:
                data_str = row["context_json"] if isinstance(row, dict) else row[0]
                data = json.loads(data_str)
                _context.update(data)
                logger.info("Patient context loaded from database")
                return _context
    except Exception:
        logger.debug("No patient context in database (table may not exist yet)")
    return {}


async def save_to_db(db: Any, context: dict[str, Any] | None = None) -> None:
    """Save current patient context to the database."""
    data = context or _context
    await db.execute(
        """
        INSERT INTO patient_context (id, context_json, updated_at)
        VALUES (1, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ON CONFLICT(id) DO UPDATE SET
            context_json = excluded.context_json,
            updated_at = excluded.updated_at
        """,
        (json.dumps(data, ensure_ascii=False),),
    )
    await db.commit()


def update_context(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge updates into the current context. Returns the updated context."""
    ctx = get_context()
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(ctx.get(key), dict):
            ctx[key].update(value)
        else:
            ctx[key] = value
    _context.update(ctx)
    return _context


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


def format_context_text() -> str:
    """Format patient context as a human-readable string for tool output."""
    ctx = get_context()
    bio = ctx.get("biomarkers", {})
    biomarkers = "\n".join(f"  - {k}: {v}" for k, v in bio.items())
    mets = ", ".join(ctx.get("metastases", []))
    comorb = ", ".join(ctx.get("comorbidities", []))
    excluded = "\n".join(f"  - {t}" for t in ctx.get("excluded_therapies", []))
    tx = ctx.get("treatment", {})
    phys = ctx.get("physicians", {})
    return (
        f"**Patient:** {ctx.get('name', 'Unknown')}\n"
        f"**Diagnosis:** {ctx.get('diagnosis', '')}\n"
        f"**Staging:** {ctx.get('staging', '')}\n"
        f"**Histology:** {ctx.get('histology', '')}\n"
        f"**Tumor site:** {ctx.get('tumor_site', '')}\n"
        f"**Biomarkers:**\n{biomarkers}\n"
        f"**Treatment:** {tx.get('regimen', '')} (cycle {tx.get('current_cycle', '?')}) "
        f"at {tx.get('institution', '')}\n"
        f"**Metastases:** {mets}\n"
        f"**Comorbidities:** {comorb}\n"
        f"**Physicians:** {phys.get('treating', '')}; {phys.get('admitting', '')}\n"
        f"**Excluded therapies:**\n{excluded}\n"
        f"**Note:** {ctx.get('note', '')}"
    )
