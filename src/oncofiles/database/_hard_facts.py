"""Hard-data model, phase 1 (#711, migration 080): insert / get / list helpers.

Design: ``docs/architecture/hard_data_model.md``. Phase 1 is additive: these
helpers write only the new tables, nothing reads them yet, and no existing data
is moved.

Rules enforced here:
* every patient-data helper takes a keyword-only ``patient_id`` and filters on
  it in SQL, so a foreign id returns nothing (no existence oracle);
* every patient-scoped reference (source document, encounter, practitioner,
  condition, course, cycle, request, specimen, report) must belong to the same
  patient, otherwise ``ValueError`` before anything is written;
* ``codings.owner_type``, ``identifiers.owner_type`` and
  ``provenance.target_type`` are free text in SQL and validated here against an
  allowlist;
* deletion is soft (``deleted_at`` / ``deleted_by``), provenance is append-only.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from oncofiles.models import (
    CancerStage,
    Coding,
    Condition,
    Device,
    HardFact,
    Identifier,
    MedicationEvent,
    Procedure,
    ProvenanceRecord,
    Report,
    Specimen,
    TreatmentCourse,
    TreatmentCycle,
)

_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"

# entity type → (table, model) for the fact tables
FACT_ENTITIES: dict[str, tuple[str, type[HardFact]]] = {
    "condition": ("conditions", Condition),
    "cancer_stage": ("cancer_stages", CancerStage),
    "treatment_course": ("treatment_courses", TreatmentCourse),
    "treatment_cycle": ("treatment_cycles", TreatmentCycle),
    "medication_event": ("medication_events", MedicationEvent),
    "procedure": ("procedures", Procedure),
    "specimen": ("specimens", Specimen),
    "report": ("reports", Report),
}

# patient-scoped owner type → table (every one has a patient_id column)
PATIENT_OWNER_TABLES: dict[str, str] = {
    **{entity: table for entity, (table, _) in FACT_ENTITIES.items()},
    "fact": "clinical_records",
    "document": "documents",
    "encounter": "encounters",
    "practitioner": "practitioners",
}
# shared reference owner type → table (no patient_id)
SHARED_OWNER_TABLES: dict[str, str] = {
    "organization": "organizations",
    "department": "departments",
    "device": "devices",
}
CODING_OWNER_TYPES = (
    frozenset(PATIENT_OWNER_TABLES) | frozenset(SHARED_OWNER_TABLES) | {"fact_code"}
)
# ── identifiers: data minimisation ──
# Only business identifiers of providers and of lab/document artefacts are
# stored. A patient's national ID (rodné číslo), insurance number or any
# MRN-like patient number is NEVER stored, for any owner. If matching on them
# is ever needed: only a salted hash, and only after a maintainer decision.
# Extraction phases must not fill identifiers by default.
ID_SYSTEM_UDZS_PROVIDER = "urn:oncofiles:id:udzs-provider"  # ÚDZS provider code
ID_SYSTEM_UDZS_DOCTOR = "urn:oncofiles:id:udzs-doctor"  # ÚDZS doctor code
ID_SYSTEM_ICO = "urn:oncofiles:id:ico"  # IČO (organisation registry number)
ID_SYSTEM_ACCESSION = "urn:oncofiles:id:accession"  # lab / document accession number
IDENTIFIER_SYSTEMS_BY_OWNER: dict[str, frozenset[str]] = {
    "organization": frozenset({ID_SYSTEM_UDZS_PROVIDER, ID_SYSTEM_ICO}),
    "department": frozenset({ID_SYSTEM_UDZS_PROVIDER}),
    "practitioner": frozenset({ID_SYSTEM_UDZS_DOCTOR}),
    "specimen": frozenset({ID_SYSTEM_ACCESSION}),
    "report": frozenset({ID_SYSTEM_ACCESSION}),
    "document": frozenset({ID_SYSTEM_ACCESSION}),
}
IDENTIFIER_OWNER_TYPES = frozenset(IDENTIFIER_SYSTEMS_BY_OWNER)
# Explicitly forbidden for every owner: national ID, insurance, MRN-like numbers.
_FORBIDDEN_ID_SYSTEM = re.compile(
    r"rodn|birth|national|\bnid\b|personal.?(id|number|code)|ssn|social.?security"
    r"|insur|poist|\bpoi\b|health.?card|ehic|mrn|medical.?record|patient.?(id|number)"
    r"|chart.?number|case.?number|\brc\b",
    re.I,
)
# A value shaped like a Slovak/Czech birth number with its slash (YYMMDD/NNNN).
_BIRTH_NUMBER_VALUE = re.compile(r"^\s*\d{6}\s*/\s*\d{3,4}\s*$")


def check_identifier_allowed(owner_type: str, system: str, value: str) -> None:
    """Reject forbidden identifier systems/values, then enforce the per-owner allowlist."""
    if _FORBIDDEN_ID_SYSTEM.search(system) or _BIRTH_NUMBER_VALUE.match(value):
        raise ValueError(
            "identifier rejected: national ID, insurance or patient record numbers are never stored"
        )
    allowed = IDENTIFIER_SYSTEMS_BY_OWNER.get(owner_type)
    if allowed is None:
        raise ValueError(f"owner_type {owner_type!r} is not allowed")
    if system not in allowed:
        raise ValueError(f"identifier system {system!r} is not allowed for {owner_type}")


PROVENANCE_TARGET_TYPES = frozenset(FACT_ENTITIES) | {"fact", "encounter"}
PROVENANCE_ACTIVITIES = frozenset({"extract", "reread", "verify", "correct", "import", "manual"})
PROVENANCE_AGENT_ROLES = frozenset({"assembler", "verifier", "author", "enterer"})

# column → table for references that must belong to the same patient
_PATIENT_REFS: dict[str, str] = {
    "source_document_id": "documents",
    "encounter_id": "encounters",
    "specimen_id": "specimens",
    "condition_id": "conditions",
    "primary_condition_id": "conditions",
    "course_id": "treatment_courses",
    "cycle_id": "treatment_cycles",
    "request_id": "medication_events",
    "asserter_practitioner_id": "practitioners",
    "interpreter_practitioner_id": "practitioners",
    "prescriber_practitioner_id": "practitioners",
    "performer_practitioner_id": "practitioners",
}

# set by the writer, never taken from the caller's model
_SERVER_FIELDS = frozenset(
    {"id", "fhir_id", "patient_id", "version_id", "last_updated", "created_at", "deleted_at",
     "deleted_by"}
)  # fmt: skip


def _check_model_patient(model_pid: str | None, patient_id: str) -> None:
    if not patient_id:
        raise ValueError("patient_id is required")
    if model_pid is not None and model_pid != patient_id:
        raise ValueError("model patient_id does not match the patient_id argument")


def _sql_value(value: Any) -> Any:
    return int(value) if isinstance(value, bool) else value


class HardFactsMixin:
    """Conditions, stages, treatments, procedures, specimens, reports + building blocks."""

    # ── internals ─────────────────────────────────────────────────────

    async def _hf_one(self, sql: str, params: tuple) -> dict | None:
        async with self.db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _hf_all(self, sql: str, params: tuple) -> list[dict]:
        async with self.db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _require_owned(self, table: str, row_id: int, patient_id: str, what: str) -> dict:
        """The row exists and belongs to ``patient_id``, else ``ValueError``."""
        row = await self._hf_one(
            f"SELECT * FROM {table} WHERE id = ? AND patient_id = ?",
            (row_id, patient_id),
        )
        if row is None:
            raise ValueError(f"{what} {row_id} not found for this patient")
        return row

    async def _insert_row(self, table: str, data: dict[str, Any]) -> int:
        cols = list(data)
        cursor = await self.db.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            tuple(_sql_value(data[c]) for c in cols),
        )
        await self.db.commit()
        return cursor.lastrowid

    # ── fact tables (generic) ─────────────────────────────────────────

    async def _insert_fact(self, entity: str, item: HardFact, *, patient_id: str) -> HardFact:
        table, model_cls = FACT_ENTITIES[entity]
        if not isinstance(item, model_cls):
            raise TypeError(f"{entity} expects {model_cls.__name__}")
        _check_model_patient(item.patient_id, patient_id)
        data = item.model_dump(exclude=set(_SERVER_FIELDS))
        refs: dict[str, dict] = {}
        for col, ref_table in _PATIENT_REFS.items():
            if data.get(col) is not None:
                refs[col] = await self._require_owned(ref_table, data[col], patient_id, col)
        if entity == "medication_event":
            req = refs.get("request_id")
            if req is not None and req["kind"] != "request":
                raise ValueError("request_id must point to a medication request")
            cyc = refs.get("cycle_id")
            course_id = data.get("course_id")
            if cyc is not None and course_id is not None and cyc["course_id"] != course_id:
                raise ValueError("cycle_id belongs to a different course")
        data["patient_id"] = patient_id
        data["fhir_id"] = str(uuid.uuid4())
        row_id = await self._insert_row(table, data)
        return await self._get_fact(entity, row_id, patient_id=patient_id)

    async def _get_fact(
        self, entity: str, row_id: int, *, patient_id: str, include_deleted: bool = False
    ) -> HardFact | None:
        table, model_cls = FACT_ENTITIES[entity]
        sql = f"SELECT * FROM {table} WHERE id = ? AND patient_id = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        row = await self._hf_one(sql, (row_id, patient_id))
        return model_cls.model_validate(row) if row else None

    async def _list_facts(
        self,
        entity: str,
        *,
        patient_id: str,
        order_by: str,
        where: dict[str, Any] | None = None,
        include_deleted: bool = False,
        limit: int = 500,
    ) -> list[HardFact]:
        table, model_cls = FACT_ENTITIES[entity]
        clauses, params = ["patient_id = ?"], [patient_id]
        for col, value in (where or {}).items():
            clauses.append(f"{col} = ?")
            params.append(value)
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        rows = await self._hf_all(
            f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} ORDER BY {order_by}, id LIMIT ?",
            (*params, limit),
        )
        return [model_cls.model_validate(r) for r in rows]

    async def soft_delete_hard_fact(
        self, entity: str, row_id: int, *, patient_id: str, deleted_by: str
    ) -> bool:
        """Soft-delete one fact row. Returns False when nothing matched."""
        table, _ = FACT_ENTITIES[entity]
        cursor = await self.db.execute(
            f"UPDATE {table} SET deleted_at = {_NOW}, deleted_by = ?, "
            f"last_updated = {_NOW} WHERE id = ? AND patient_id = ? AND deleted_at IS NULL",
            (deleted_by, row_id, patient_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    # ── conditions + stages ───────────────────────────────────────────

    async def insert_condition(self, condition: Condition, *, patient_id: str) -> Condition:
        return await self._insert_fact("condition", condition, patient_id=patient_id)

    async def get_condition(self, condition_id: int, *, patient_id: str) -> Condition | None:
        return await self._get_fact("condition", condition_id, patient_id=patient_id)

    async def list_conditions(
        self, *, patient_id: str, include_deleted: bool = False
    ) -> list[Condition]:
        return await self._list_facts(
            "condition", patient_id=patient_id, order_by="asserted_at",
            include_deleted=include_deleted,
        )  # fmt: skip

    async def insert_cancer_stage(self, stage: CancerStage, *, patient_id: str) -> CancerStage:
        return await self._insert_fact("cancer_stage", stage, patient_id=patient_id)

    async def list_cancer_stages(
        self, *, patient_id: str, condition_id: int | None = None
    ) -> list[CancerStage]:
        where = {"condition_id": condition_id} if condition_id is not None else None
        return await self._list_facts(
            "cancer_stage", patient_id=patient_id, order_by="assessed_at", where=where
        )

    # ── treatments ────────────────────────────────────────────────────

    async def insert_treatment_course(
        self, course: TreatmentCourse, *, patient_id: str
    ) -> TreatmentCourse:
        return await self._insert_fact("treatment_course", course, patient_id=patient_id)

    async def get_treatment_course(
        self, course_id: int, *, patient_id: str
    ) -> TreatmentCourse | None:
        return await self._get_fact("treatment_course", course_id, patient_id=patient_id)

    async def list_treatment_courses(self, *, patient_id: str) -> list[TreatmentCourse]:
        return await self._list_facts(
            "treatment_course", patient_id=patient_id, order_by="start_at"
        )

    async def insert_treatment_cycle(
        self, cycle: TreatmentCycle, *, patient_id: str
    ) -> TreatmentCycle:
        return await self._insert_fact("treatment_cycle", cycle, patient_id=patient_id)

    async def list_treatment_cycles(
        self, *, patient_id: str, course_id: int | None = None
    ) -> list[TreatmentCycle]:
        where = {"course_id": course_id} if course_id is not None else None
        return await self._list_facts(
            "treatment_cycle", patient_id=patient_id, order_by="planned_at", where=where
        )

    async def insert_medication_event(
        self, event: MedicationEvent, *, patient_id: str
    ) -> MedicationEvent:
        return await self._insert_fact("medication_event", event, patient_id=patient_id)

    async def get_medication_event(
        self, event_id: int, *, patient_id: str
    ) -> MedicationEvent | None:
        return await self._get_fact("medication_event", event_id, patient_id=patient_id)

    async def list_medication_events(
        self,
        *,
        patient_id: str,
        course_id: int | None = None,
        cycle_id: int | None = None,
        kind: str | None = None,
    ) -> list[MedicationEvent]:
        where = {
            k: v
            for k, v in (("course_id", course_id), ("cycle_id", cycle_id), ("kind", kind))
            if v is not None
        }
        return await self._list_facts(
            "medication_event", patient_id=patient_id, order_by="effective_start", where=where
        )

    # ── procedures, specimens, reports ────────────────────────────────

    async def insert_procedure(self, procedure: Procedure, *, patient_id: str) -> Procedure:
        return await self._insert_fact("procedure", procedure, patient_id=patient_id)

    async def get_procedure(self, procedure_id: int, *, patient_id: str) -> Procedure | None:
        return await self._get_fact("procedure", procedure_id, patient_id=patient_id)

    async def list_procedures(self, *, patient_id: str) -> list[Procedure]:
        return await self._list_facts("procedure", patient_id=patient_id, order_by="performed_at")

    async def insert_specimen(self, specimen: Specimen, *, patient_id: str) -> Specimen:
        return await self._insert_fact("specimen", specimen, patient_id=patient_id)

    async def get_specimen(self, specimen_id: int, *, patient_id: str) -> Specimen | None:
        return await self._get_fact("specimen", specimen_id, patient_id=patient_id)

    async def insert_report(self, report: Report, *, patient_id: str) -> Report:
        return await self._insert_fact("report", report, patient_id=patient_id)

    async def get_report(self, report_id: int, *, patient_id: str) -> Report | None:
        return await self._get_fact("report", report_id, patient_id=patient_id)

    # ── devices (shared reference data, not patient data) ─────────────

    async def upsert_device(self, device: Device) -> Device:
        """Insert or return the device with the same (kind, name, version)."""
        if device.version_id != 1 or device.id is not None:
            raise ValueError("upsert_device takes a new device")
        await self.db.execute(
            "INSERT OR IGNORE INTO devices (fhir_id, kind, name, version, model, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), device.kind, device.name, device.version, device.model,
             device.note),
        )  # fmt: skip
        await self.db.commit()
        row = await self._hf_one(
            "SELECT * FROM devices WHERE kind = ? AND name = ? AND version = ?",
            (device.kind, device.name, device.version),
        )
        return Device.model_validate(row)

    async def get_device(self, device_id: int) -> Device | None:
        row = await self._hf_one("SELECT * FROM devices WHERE id = ?", (device_id,))
        return Device.model_validate(row) if row else None

    # ── owner checks for codings / identifiers ────────────────────────

    async def _check_owner(
        self, owner_type: str, owner_id: Any, patient_id: str | None, allowed: frozenset[str]
    ) -> None:
        if owner_type not in allowed:
            raise ValueError(f"owner_type {owner_type!r} is not allowed")
        if owner_type in PATIENT_OWNER_TABLES:
            if not patient_id:
                raise ValueError(f"{owner_type} is patient data: patient_id is required")
            await self._require_owned(
                PATIENT_OWNER_TABLES[owner_type], int(owner_id), patient_id, owner_type
            )
            return
        if patient_id is not None:
            raise ValueError(f"{owner_type} is shared reference data: patient_id must be None")
        if owner_type == "fact_code":
            found = await self._hf_one("SELECT code FROM fact_codes WHERE code = ?", (owner_id,))
        else:
            found = await self._hf_one(
                f"SELECT id FROM {SHARED_OWNER_TABLES[owner_type]} WHERE id = ?",
                (int(owner_id),),
            )
        if found is None:
            raise ValueError(f"{owner_type} {owner_id} not found")

    # ── codings ───────────────────────────────────────────────────────

    async def add_coding(self, coding: Coding, *, patient_id: str | None) -> Coding:
        """Attach a code to an entity. ``patient_id`` is None only for shared owners."""
        if coding.patient_id is not None and coding.patient_id != patient_id:
            raise ValueError("model patient_id does not match the patient_id argument")
        await self._check_owner(coding.owner_type, coding.owner_id, patient_id, CODING_OWNER_TYPES)
        data = coding.model_dump(exclude={"id", "patient_id", "created_at", "deleted_at",
                                          "deleted_by"})  # fmt: skip
        data["patient_id"] = patient_id
        row_id = await self._insert_row("codings", data)
        row = await self._hf_one("SELECT * FROM codings WHERE id = ?", (row_id,))
        return Coding.model_validate(row)

    async def list_codings(
        self, owner_type: str, owner_id: Any, *, patient_id: str | None
    ) -> list[Coding]:
        rows = await self._hf_all(
            "SELECT * FROM codings WHERE owner_type = ? AND owner_id = ? AND patient_id IS ? "
            "AND deleted_at IS NULL ORDER BY is_original DESC, id",
            (owner_type, str(owner_id), patient_id),
        )
        return [Coding.model_validate(r) for r in rows]

    # ── identifiers ───────────────────────────────────────────────────

    async def add_identifier(self, identifier: Identifier, *, patient_id: str | None) -> Identifier:
        """Attach an identifier. ``patient_id`` is None only for organisations/departments.

        Data minimisation: only the systems in ``IDENTIFIER_SYSTEMS_BY_OWNER`` are
        accepted, and a national ID / insurance / MRN-like number is rejected for
        every owner, before anything else is checked.
        """
        check_identifier_allowed(identifier.owner_type, identifier.system, identifier.value)
        if identifier.patient_id is not None and identifier.patient_id != patient_id:
            raise ValueError("model patient_id does not match the patient_id argument")
        await self._check_owner(
            identifier.owner_type, identifier.owner_id, patient_id, IDENTIFIER_OWNER_TYPES
        )
        data = identifier.model_dump(exclude={"id", "patient_id", "created_at", "deleted_at",
                                              "deleted_by"})  # fmt: skip
        data["patient_id"] = patient_id
        row_id = await self._insert_row("identifiers", data)
        row = await self._hf_one("SELECT * FROM identifiers WHERE id = ?", (row_id,))
        return Identifier.model_validate(row)

    async def list_identifiers(
        self, owner_type: str, owner_id: int, *, patient_id: str | None
    ) -> list[Identifier]:
        rows = await self._hf_all(
            "SELECT * FROM identifiers WHERE owner_type = ? AND owner_id = ? "
            "AND patient_id IS ? AND deleted_at IS NULL ORDER BY id",
            (owner_type, owner_id, patient_id),
        )
        return [Identifier.model_validate(r) for r in rows]

    async def find_by_identifier(
        self, system: str, value: str, *, patient_id: str | None
    ) -> list[Identifier]:
        """Identifier rows with this system/value, within one patient (or shared rows)."""
        rows = await self._hf_all(
            "SELECT * FROM identifiers WHERE system = ? AND value = ? AND patient_id IS ? "
            "AND deleted_at IS NULL ORDER BY id",
            (system, value, patient_id),
        )
        return [Identifier.model_validate(r) for r in rows]

    # ── provenance (append-only) ──────────────────────────────────────

    async def add_provenance(
        self, record: ProvenanceRecord, *, patient_id: str
    ) -> ProvenanceRecord:
        _check_model_patient(record.patient_id, patient_id)
        if record.target_type not in PROVENANCE_TARGET_TYPES:
            raise ValueError(f"target_type {record.target_type!r} is not allowed")
        if record.activity not in PROVENANCE_ACTIVITIES:
            raise ValueError(f"activity {record.activity!r} is not allowed")
        if record.agent_role not in PROVENANCE_AGENT_ROLES:
            raise ValueError(f"agent_role {record.agent_role!r} is not allowed")
        if record.agent_device_id is None and not record.agent_user:
            raise ValueError("provenance needs an agent (device or user)")
        await self._require_owned(
            PATIENT_OWNER_TABLES[record.target_type], record.target_id, patient_id, "target"
        )
        if record.source_document_id is not None:
            await self._require_owned(
                "documents", record.source_document_id, patient_id, "source_document_id"
            )
        if (
            record.agent_device_id is not None
            and await self.get_device(record.agent_device_id) is None
        ):
            raise ValueError(f"device {record.agent_device_id} not found")
        data = record.model_dump(exclude={"id", "fhir_id", "patient_id", "created_at"})
        if data["recorded_at"] is None:
            del data["recorded_at"]
        data["patient_id"] = patient_id
        data["fhir_id"] = str(uuid.uuid4())
        row_id = await self._insert_row("provenance", data)
        row = await self._hf_one(
            "SELECT * FROM provenance WHERE id = ? AND patient_id = ?", (row_id, patient_id)
        )
        return ProvenanceRecord.model_validate(row)

    async def list_provenance(
        self, target_type: str, target_id: int, *, patient_id: str
    ) -> list[ProvenanceRecord]:
        rows = await self._hf_all(
            "SELECT * FROM provenance WHERE patient_id = ? AND target_type = ? AND target_id = ? "
            "ORDER BY recorded_at, id",
            (patient_id, target_type, target_id),
        )
        return [ProvenanceRecord.model_validate(r) for r in rows]
