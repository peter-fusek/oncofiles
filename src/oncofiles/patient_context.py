"""Patient context: load, save, and access per-patient clinical data.

Every read and write is keyed by a patient id (#733). There is no module-level
"current patient": the legacy single-patient global (`_context`), the JSON-file
loader and the legacy `id = 1` row path were removed, and `get_context()`
without a resolvable patient raises instead of serving whatever an earlier
call left behind (the #429/#713 class of cross-patient leaks).

Storage is the `patient_context` table (one row per patient_id). The only
module-level mutable state is `_contexts`, a cache keyed by patient_id.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Per-patient context cache — keyed by patient_id. The ONLY module-level
# mutable state in this module (locked by tests/test_patient_context_no_global.py).
_contexts: dict[str, dict[str, Any]] = {}

_NO_PATIENT_MSG = (
    "patient context requires a patient: pass patient_slug (or an explicit "
    "patient_id). There is no default/legacy patient (#733)."
)


def _default_context() -> dict[str, Any]:
    """Return a fresh template for a patient with no stored row.

    Built on every call, so nested dicts/lists are never shared between
    patients (#713). The name is always empty: the old `PATIENT_NAME` env
    default belonged to the removed single-patient flow (#733).
    NEVER put patient-specific data (diagnosis, biomarkers, physicians) here.
    """
    return {
        "name": "",
        # Hospital chart name if distinct from display name. Slovak medical records
        # sometimes file patients under mother's/birth surname (common for pediatric
        # or transplant patients). Pipelines that match filename<->patient must accept
        # EITHER `name` OR `medical_record_name` — see _try_patient_name_swap. #439
        "medical_record_name": "",
        # Home region for trial/protocol localization. oncoteam prioritizes home-country
        # clinical trials. {city, country, country_code (ISO 3166-1 alpha-2)}. #421
        "home_region": {},
        "patient_type": "oncology",  # "oncology" or "general"
        "date_of_birth": "",  # ISO date
        "sex": "",  # "male" or "female"
        "diagnosis": "",
        "staging": "",
        "histology": "",
        "tumor_site": "",
        "diagnosis_date": "",
        "biomarkers": {},
        "germline_findings": {},  # {gene: {classification, variant, zygosity, test_date, test_lab}}
        "treatment": {},
        "metastases": [],
        "comorbidities": [],
        "surgeries": [],
        "physicians": {},
        "excluded_therapies": [],
        "note": "",
    }


# Name used by #713 code and tests for "context of a pid with no stored row".
_empty_template = _default_context


def resolve_patient_id(patient_id: str | None = None) -> str:
    """Return the explicit pid, else the current request's pid; raise if neither.

    Empty string and None both count as "no pid". The request ContextVar is
    set by PatientResolutionMiddleware (and by the test `db` fixture).
    """
    pid = patient_id
    if not pid:
        try:
            from oncofiles.patient_middleware import get_current_patient_id

            pid = get_current_patient_id()
        except (ImportError, LookupError):
            pid = ""
    if not pid:
        raise ValueError(_NO_PATIENT_MSG)
    return pid


def get_context(patient_id: str | None = None) -> dict[str, Any]:
    """Return the context dict for one patient.

    Resolution: the non-empty explicit `patient_id`, else the current request
    ContextVar. With neither, raises ValueError naming patient_slug (#733) —
    there is no legacy fallback any more.

    A resolved pid with no cached row gets a fresh empty template (only
    `patient_type` set, `name` empty) — never another patient's data (#713).
    Callers that need the stored row must hydrate first
    (`await load_from_db(db, pid)` or `await ensure_loaded(db, pid)`).
    """
    pid = resolve_patient_id(patient_id)
    if pid in _contexts:
        return _contexts[pid]
    return _default_context()


def get_patient_name(patient_id: str | None = None) -> str:
    """Return the patient's display name from context (raises without a patient)."""
    return get_context(patient_id).get("name", "")


def get_medical_record_name(patient_id: str | None = None) -> str:
    """Return the hospital-chart name if distinct from display name.

    Empty string means "no distinct chart name" — display_name is authoritative.
    Populated means filenames containing this name ALSO belong to the patient
    (e.g. a pediatric chart filed under the mother's surname). See #439.
    """
    return get_context(patient_id).get("medical_record_name", "") or ""


async def _fetch_context_row(db: Any, patient_id: str) -> dict[str, Any] | None:
    """Read one stored context row. Returns None when no row exists; DB errors propagate."""
    if not patient_id:
        # The legacy `WHERE id = 1` path is gone (#733): an empty pid must never
        # read some other row (#476 class of bugs).
        raise ValueError(_NO_PATIENT_MSG)
    async with db.execute(
        "SELECT context_json FROM patient_context WHERE patient_id = ?",
        (patient_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    data_str = row["context_json"] if isinstance(row, dict) else row[0]
    return json.loads(data_str)


async def ensure_loaded(db: Any, patient_id: str) -> None:
    """Hydrate the per-patient cache from the DB before a read-modify-write (#713).

    Unlike `load_from_db`, a DB error is NOT swallowed: a writer that merged
    into the default context after a failed read would save default + update
    over the stored row (data loss). A missing row is fine (new patient).
    """
    if not patient_id:
        raise ValueError("ensure_loaded requires a non-empty patient_id")
    if patient_id in _contexts:
        return
    data = await _fetch_context_row(db, patient_id)
    if data is not None:
        _contexts[patient_id] = data


async def load_from_db(db: Any, patient_id: str) -> dict[str, Any]:
    """Load one patient's context from the `patient_context` table into the cache.

    A non-empty `patient_id` is required (ValueError otherwise, #733). Returns
    the stored dict, or `{}` when there is no row or the table is unreadable.
    """
    if not patient_id:
        raise ValueError(_NO_PATIENT_MSG)
    try:
        data = await _fetch_context_row(db, patient_id)
        if data is not None:
            _contexts[patient_id] = data
            logger.info("Patient context loaded from database (patient_id=%s)", patient_id)
            return data
    except Exception:
        logger.debug("No patient context in database (table may not exist yet)")
    return {}


async def save_to_db(db: Any, context: dict[str, Any] | None = None, *, patient_id: str) -> None:
    """Save one patient's context to the database (a non-empty patient_id is required)."""
    if not patient_id:
        raise ValueError(_NO_PATIENT_MSG)
    data = context or get_context(patient_id)
    json_data = json.dumps(data, ensure_ascii=False)
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
    await db.commit()
    # Cache a private copy so later mutation of the caller's dict cannot
    # change the cached context behind its back (#713).
    _contexts[patient_id] = copy.deepcopy(data)


def update_context(updates: dict[str, Any], patient_id: str | None = None) -> dict[str, Any]:
    """Merge updates into one patient's context. Returns the updated context.

    Isolation rules (#713):
    - The base context is deep-copied before merging, so nested dicts of the
      cached context or of the default template are never mutated in place.
    - A non-empty `patient_id` is required; None or "" raises ValueError (same
      rule as #686/#696). Only that patient's cache entry is written.
    - The caller must hydrate the cache first (`await ensure_loaded(db, pid)`),
      otherwise the merge starts from the default template.
    """
    if not patient_id:
        raise ValueError(
            "update_context requires a patient: pass patient_slug (or an explicit "
            "patient_id). Updates without a patient are refused so they can never "
            "land in another patient's context (#713)."
        )
    ctx = copy.deepcopy(get_context(patient_id))
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(ctx.get(key), dict):
            ctx[key].update(copy.deepcopy(value))
        else:
            ctx[key] = copy.deepcopy(value)
    _contexts[patient_id] = ctx
    return copy.deepcopy(ctx)


# ── Germline findings helpers (v5.8.0 #385) ──────────────────────────────────

# ACMG variant classification values (canonical form).
_GERMLINE_CLASSIFICATIONS = frozenset(
    {
        "pathogenic",
        "likely_pathogenic",
        "vus",  # variant of uncertain significance (ACMG class 3)
        "likely_benign",
        "benign",
        "unknown",
    }
)


def set_germline_finding(
    gene: str,
    classification: str,
    *,
    variant: str = "",
    zygosity: str = "",
    test_date: str = "",
    test_lab: str = "",
    patient_id: str | None = None,
) -> dict[str, Any]:
    """Record a germline variant finding for a gene.

    Gene names are uppercased (BRCA1, MLH1). Classification is lower-cased
    and validated against ACMG values; unrecognized values fall back to
    "unknown" so stored data stays queryable.
    """
    gene_key = (gene or "").strip().upper()
    if not gene_key:
        raise ValueError("gene is required")
    cls = (classification or "").strip().lower().replace(" ", "_").replace("-", "_")
    if cls not in _GERMLINE_CLASSIFICATIONS:
        cls = "unknown"
    if not patient_id:
        # Same rule as update_context: never resolve a write target implicitly.
        raise ValueError(_NO_PATIENT_MSG)
    ctx = get_context(patient_id)
    findings = dict(ctx.get("germline_findings") or {})
    findings[gene_key] = {
        "classification": cls,
        "variant": variant,
        "zygosity": zygosity,
        "test_date": test_date,
        "test_lab": test_lab,
    }
    return update_context({"germline_findings": findings}, patient_id=patient_id)


def get_germline_status(gene: str, patient_id: str | None = None) -> str:
    """Return the ACMG classification for a gene, or "unknown" if not recorded."""
    gene_key = (gene or "").strip().upper()
    if not gene_key:
        return "unknown"
    ctx = get_context(patient_id)
    finding = (ctx.get("germline_findings") or {}).get(gene_key)
    if not isinstance(finding, dict):
        return "unknown"
    cls = str(finding.get("classification") or "unknown").lower()
    return cls if cls in _GERMLINE_CLASSIFICATIONS else "unknown"


async def initialize(database: Any) -> int:
    """Hydrate the cache for every active patient, by pid. Called once at startup.

    `database` is the `Database` wrapper (needs `list_patients` and `.db`).
    There is no legacy row, JSON file or hardcoded default any more (#733);
    a patient without a stored row simply gets the empty template on read.
    Returns the number of patients whose stored context was loaded.
    """
    loaded = 0
    for p in await database.list_patients(active_only=True):
        if await load_from_db(database.db, p.patient_id):
            loaded += 1
    logger.info("Patient context hydrated for %d active patient(s)", loaded)
    return loaded


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
        germline = ctx.get("germline_findings") or {}
        if germline:
            germ_lines = []
            for gene, finding in sorted(germline.items()):
                if not isinstance(finding, dict):
                    continue
                cls = finding.get("classification", "unknown")
                variant = finding.get("variant", "")
                lab = finding.get("test_lab", "")
                bits = [f"{gene} ({cls})"]
                if variant:
                    bits.append(variant)
                if lab:
                    bits.append(lab)
                germ_lines.append("  - " + " · ".join(bits))
            lines.append("**Germline findings:**\n" + "\n".join(germ_lines))
        lines.append(f"**Excluded therapies:**\n{excluded}")

    lines.append(f"**Note:** {ctx.get('note', '')}")
    return "\n".join(lines)
