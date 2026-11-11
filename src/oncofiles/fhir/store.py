"""Load one patient's rows for the FHIR projection. Read only.

Every patient-data query filters on ``patient_id`` in SQL. Shared reference
rows (organisations, departments, devices) are loaded only when this patient's
own rows reference them, so a caller never learns which institutions other
patients attend. Queries run one after another (single libsql connection: never
``asyncio.gather``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from oncofiles.fhir.terminology import OBSERVATION_RECORD_TYPES

# fact tables of the hard-data model (#725) read by the projection
FACT_TABLES = (
    "conditions",
    "cancer_stages",
    "treatment_courses",
    "treatment_cycles",
    "medication_events",
    "procedures",
)

# provenance.target_type / codings.owner_type → table
ENTITY_TABLE = {
    "fact": "clinical_records",
    "condition": "conditions",
    "cancer_stage": "cancer_stages",
    "treatment_course": "treatment_courses",
    "treatment_cycle": "treatment_cycles",
    "medication_event": "medication_events",
    "procedure": "procedures",
    "document": "documents",
    "encounter": "encounters",
    "practitioner": "practitioners",
}
TABLE_ENTITY = {t: e for e, t in ENTITY_TABLE.items()}

_DOC_COLUMNS = (
    "id, patient_id, filename, title, category, document_date, mime_type, size_bytes, "
    "gdrive_id, file_id, source_url, organization_id, department_id, encounter_id, "
    "practitioner_id, created_at, updated_at, version"
)


@dataclass
class PatientData:
    patient_id: str
    patient: dict
    context: dict
    observations: list[dict] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    facts: dict[str, list[dict]] = field(default_factory=dict)  # table → rows
    encounters: list[dict] = field(default_factory=list)
    practitioners: list[dict] = field(default_factory=list)
    organizations: list[dict] = field(default_factory=list)
    departments: list[dict] = field(default_factory=list)
    devices: list[dict] = field(default_factory=list)
    # (table, row id) → codings / provenance rows
    codings: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    fact_code_codings: dict[str, list[dict]] = field(default_factory=dict)
    provenance: dict[tuple[str, int], list[dict]] = field(default_factory=dict)
    # (owner_type, owner id) → identifier rows
    identifiers: dict[tuple[str, int], list[dict]] = field(default_factory=dict)


async def _all(db: Any, sql: str, params: tuple = ()) -> list[dict]:
    async with db.db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


def _ids(rows: list[dict], *cols: str) -> set[int]:
    return {int(r[c]) for r in rows for c in cols if r.get(c) is not None}


def _in(values: set[int]) -> tuple[str, tuple]:
    vals = tuple(sorted(values))
    return ", ".join("?" for _ in vals), vals


async def load_patient(db: Any, patient_id: str) -> PatientData:
    """Every row the projection needs for one patient (sequential queries)."""
    if not patient_id:
        raise ValueError("patient_id is required")
    prow = await _all(db, "SELECT patient_id, slug FROM patients WHERE patient_id = ?",
                      (patient_id,))  # fmt: skip
    ctx_rows = await _all(
        db, "SELECT context_json FROM patient_context WHERE patient_id = ?", (patient_id,)
    )
    try:
        context = json.loads(ctx_rows[0]["context_json"]) if ctx_rows else {}
    except (TypeError, ValueError):
        context = {}
    data = PatientData(
        patient_id=patient_id,
        patient=prow[0] if prow else {"patient_id": patient_id},
        context=context if isinstance(context, dict) else {},
    )

    types = tuple(OBSERVATION_RECORD_TYPES)
    data.observations = await _all(
        db,
        "SELECT f.*, fc.loinc AS fc_loinc, fc.name_en AS fc_name_en, "
        "fc.unit_canonical AS fc_unit_canonical FROM clinical_records f "
        "LEFT JOIN fact_codes fc ON fc.code = f.code "
        f"WHERE f.patient_id = ? AND f.deleted_at IS NULL AND f.record_type IN "
        f"({', '.join('?' for _ in types)}) ORDER BY f.occurred_at, f.id",
        (patient_id, *types),
    )
    data.documents = await _all(
        db,
        f"SELECT {_DOC_COLUMNS} FROM documents WHERE patient_id = ? AND deleted_at IS NULL "
        "ORDER BY document_date, id",
        (patient_id,),
    )
    for table in FACT_TABLES:
        data.facts[table] = await _all(
            db,
            f"SELECT * FROM {table} WHERE patient_id = ? AND deleted_at IS NULL ORDER BY id",
            (patient_id,),
        )
    data.encounters = await _all(
        db,
        "SELECT * FROM encounters WHERE patient_id = ? AND deleted_at IS NULL ORDER BY start_at",
        (patient_id,),
    )
    data.practitioners = await _all(
        db, "SELECT * FROM practitioners WHERE patient_id = ? ORDER BY id", (patient_id,)
    )

    # codings: patient-owned rows, plus the shared fact_code codings of our codes
    for c in await _all(
        db,
        "SELECT * FROM codings WHERE patient_id = ? AND deleted_at IS NULL "
        "ORDER BY is_original DESC, id",
        (patient_id,),
    ):
        table = ENTITY_TABLE.get(c["owner_type"])
        if table:
            data.codings.setdefault((table, str(c["owner_id"])), []).append(c)
    codes = sorted({r["code"] for r in data.observations if r.get("code")})
    if codes:
        marks = ", ".join("?" for _ in codes)
        for c in await _all(
            db,
            "SELECT * FROM codings WHERE patient_id IS NULL AND owner_type = 'fact_code' "
            f"AND owner_id IN ({marks}) AND deleted_at IS NULL ORDER BY is_original DESC, id",
            tuple(codes),
        ):
            data.fact_code_codings.setdefault(str(c["owner_id"]), []).append(c)

    for p in await _all(
        db, "SELECT * FROM provenance WHERE patient_id = ? ORDER BY recorded_at, id", (patient_id,)
    ):
        table = ENTITY_TABLE.get(p["target_type"])
        if table:
            data.provenance.setdefault((table, int(p["target_id"])), []).append(p)

    for i in await _all(
        db,
        "SELECT * FROM identifiers WHERE patient_id = ? AND deleted_at IS NULL ORDER BY id",
        (patient_id,),
    ):
        data.identifiers.setdefault((i["owner_type"], int(i["owner_id"])), []).append(i)

    # shared reference rows, only those this patient's rows point to
    all_rows = [*data.observations, *data.documents, *data.encounters, *data.practitioners]
    for rows in data.facts.values():
        all_rows.extend(rows)
    dept_ids = _ids(all_rows, "department_id")
    if dept_ids:
        marks, vals = _in(dept_ids)
        data.departments = await _all(
            db, f"SELECT * FROM departments WHERE id IN ({marks}) ORDER BY id", vals
        )
    org_ids = _ids(all_rows, "organization_id") | _ids(data.departments, "organization_id")
    if org_ids:
        marks, vals = _in(org_ids)
        data.organizations = await _all(
            db, f"SELECT * FROM organizations WHERE id IN ({marks}) ORDER BY id", vals
        )
    for owner, ids in (("organization", org_ids), ("department", dept_ids)):
        if ids:
            marks, vals = _in(ids)
            for i in await _all(
                db,
                "SELECT * FROM identifiers WHERE patient_id IS NULL AND owner_type = ? "
                f"AND owner_id IN ({marks}) AND deleted_at IS NULL ORDER BY id",
                (owner, *vals),
            ):
                data.identifiers.setdefault((owner, int(i["owner_id"])), []).append(i)
    device_ids = {
        int(p["agent_device_id"])
        for rows in data.provenance.values()
        for p in rows
        if p.get("agent_device_id") is not None
    }
    if device_ids:
        marks, vals = _in(device_ids)
        data.devices = await _all(
            db,
            f"SELECT * FROM devices WHERE id IN ({marks}) AND deleted_at IS NULL ORDER BY id",
            vals,
        )
    return data
