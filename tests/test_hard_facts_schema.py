"""Hard-data model, phase 1 (#711, migration 080). Synthetic data only.

Covers: the migration applies and is idempotent under the existing runner,
every helper round-trips, a foreign ``patient_id`` sees nothing, cross-patient
references are rejected, and the CHECK constraints hold.
"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from oncofiles.database._hard_facts import (
    CODING_OWNER_TYPES,
    ID_SYSTEM_ACCESSION,
    ID_SYSTEM_ICO,
    ID_SYSTEM_UDZS_DOCTOR,
    ID_SYSTEM_UDZS_PROVIDER,
    IDENTIFIER_OWNER_TYPES,
    IDENTIFIER_SYSTEMS_BY_OWNER,
    PATIENT_OWNER_TABLES,
    SHARED_OWNER_TABLES,
)
from oncofiles.models import (
    CancerStage,
    Coding,
    Condition,
    Device,
    Identifier,
    MedicationEvent,
    Procedure,
    ProvenanceRecord,
    Report,
    Specimen,
    TreatmentCourse,
    TreatmentCycle,
)
from tests.helpers import ERIKA_UUID, make_doc

BOB_UUID = "00000000-0000-4000-8000-000000000002"

NEW_TABLES = (
    "codings", "provenance", "devices", "identifiers", "conditions", "cancer_stages",
    "treatment_courses", "treatment_cycles", "medication_events", "procedures",
    "specimens", "reports",
)  # fmt: skip
NEW_CR_COLUMNS = {
    "result_status", "value_comparator", "unit_printed", "unit_ucum", "interpretation_code",
    "effective_start", "effective_end", "effective_precision", "issued_at", "specimen_id",
    "report_id", "fhir_id", "version_id", "last_updated",
}  # fmt: skip
FACT_TABLES = NEW_TABLES[4:]
COMMON_FACT_COLUMNS = {
    "fhir_id", "version_id", "last_updated", "verification", "deleted_at", "deleted_by",
    "created_at", "patient_id",
}  # fmt: skip


@pytest.fixture
async def two_patients(db):
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
        (BOB_UUID, "bob-test", "Bob Test"),
    )
    await db.db.commit()
    jana_doc = await db.insert_document(make_doc(file_id="hf_a"), patient_id=ERIKA_UUID)
    bob_doc = await db.insert_document(make_doc(file_id="hf_b"), patient_id=BOB_UUID)
    return db, jana_doc.id, bob_doc.id


async def _columns(db, table: str) -> set[str]:
    async with db.db.execute(f"PRAGMA table_info({table})") as cur:
        return {r[1] for r in await cur.fetchall()}


# ── migration ──


async def test_migration_creates_tables_and_columns(db):
    for table in NEW_TABLES:
        assert await _columns(db, table), table
    for table in FACT_TABLES:
        assert await _columns(db, table) >= COMMON_FACT_COLUMNS, table
    assert await _columns(db, "clinical_records") >= NEW_CR_COLUMNS
    assert await _columns(db, "fact_codes") >= {"plausible_low", "plausible_high"}


async def test_fact_code_plausibility_is_not_seeded(db):
    async with db.db.execute(
        "SELECT COUNT(*) FROM fact_codes "
        "WHERE plausible_low IS NOT NULL OR plausible_high IS NOT NULL"
    ) as cur:
        assert (await cur.fetchone())[0] == 0


async def test_migration_is_idempotent_with_runner(db):
    await db.migrate()  # already applied → skipped
    # Forget 080 and re-run: CREATE IF NOT EXISTS is a no-op, the ALTERs fall
    # into the runner's best-effort path ("duplicate column") without raising.
    await db.db.execute("DELETE FROM schema_migrations WHERE version = '080_hard_data_model'")
    await db.db.commit()
    await db.migrate()
    assert await _columns(db, "fact_codes") >= {"plausible_low", "plausible_high"}
    async with db.db.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version = '080_hard_data_model'"
    ) as cur:
        assert (await cur.fetchone())[0] == 1
    assert await _columns(db, "clinical_records") >= NEW_CR_COLUMNS


async def test_existing_clinical_records_insert_unchanged(db):
    """Old writers that know nothing about the new columns keep working."""
    await db.db.execute(
        "INSERT INTO clinical_records (patient_id, record_type, param, source) "
        "VALUES (?, 'lab', 'Synthetic', 'manual')",
        (ERIKA_UUID,),
    )
    await db.db.commit()
    async with db.db.execute(
        "SELECT version_id, fhir_id, result_status FROM clinical_records"
    ) as cur:
        row = dict(await cur.fetchone())
    assert row == {"version_id": 1, "fhir_id": None, "result_status": None}


# ── CHECK constraints ──


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "INSERT INTO conditions (fhir_id, patient_id, printed_text, verification) "
            "VALUES ('x', ?, 'Synthetic', 'trusted')",
            (ERIKA_UUID,),
        ),
        (
            "INSERT INTO medication_events (fhir_id, patient_id, kind, ingredient_printed) "
            "VALUES ('x', ?, 'injection', 'Synthocillin')",
            (ERIKA_UUID,),
        ),
        (
            "INSERT INTO conditions (fhir_id, patient_id, printed_text, clinical_status) "
            "VALUES ('x', ?, 'Synthetic', 'cured')",
            (ERIKA_UUID,),
        ),
        (
            "INSERT INTO reports (fhir_id, patient_id, status) VALUES ('x', ?, 'done')",
            (ERIKA_UUID,),
        ),
        (
            "INSERT INTO clinical_records (patient_id, record_type, source, value_comparator) "
            "VALUES (?, 'lab', 'manual', '~')",
            (ERIKA_UUID,),
        ),
        (
            "INSERT INTO clinical_records (patient_id, record_type, source, effective_precision) "
            "VALUES (?, 'lab', 'manual', 'week')",
            (ERIKA_UUID,),
        ),
        (
            "INSERT INTO conditions (patient_id, printed_text) VALUES (?, 'no fhir id')",
            (ERIKA_UUID,),
        ),
    ],
)
async def test_check_constraints_hold(db, sql, params):
    with pytest.raises(sqlite3.IntegrityError):
        await db.db.execute(sql, params)
    await db.db.rollback()


async def test_verification_defaults_to_unverified(db):
    cond = await db.insert_condition(Condition(printed_text="Synthetic"), patient_id=ERIKA_UUID)
    assert cond.verification == "unverified"
    assert cond.version_id == 1 and cond.fhir_id and cond.last_updated


def test_models_reject_unknown_closed_values():
    with pytest.raises(ValidationError):
        MedicationEvent(kind="injection", ingredient_printed="Synthocillin")
    with pytest.raises(ValidationError):
        Condition(printed_text="Synthetic", verification="trusted")


# ── round trips ──


async def test_condition_and_stage_round_trip(two_patients):
    db, doc_a, _ = two_patients
    primary = await db.insert_condition(
        Condition(
            printed_text="Synthetic adenocarcinoma", is_primary_cancer=True,
            clinical_certainty="confirmed", asserted_at="2027-05", source_document_id=doc_a,
            evidence="Dg: synthetic adenocarcinoma",
        ),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    meta = await db.insert_condition(
        Condition(printed_text="Synthetic secondary site", primary_condition_id=primary.id),
        patient_id=ERIKA_UUID,
    )
    got = await db.get_condition(primary.id, patient_id=ERIKA_UUID)
    assert got.is_primary_cancer is True and got.patient_id == ERIKA_UUID
    assert got.evidence == "Dg: synthetic adenocarcinoma"
    assert [c.id for c in await db.list_conditions(patient_id=ERIKA_UUID)] == [meta.id, primary.id]

    stage = await db.insert_cancer_stage(
        CancerStage(condition_id=primary.id, printed="pTX NX synthetic", system="UICC", prefix="p"),
        patient_id=ERIKA_UUID,
    )
    stages = await db.list_cancer_stages(patient_id=ERIKA_UUID, condition_id=primary.id)
    assert [s.id for s in stages] == [stage.id]
    assert stages[0].printed == "pTX NX synthetic"


async def test_treatment_round_trip(two_patients):
    db, doc_a, _ = two_patients
    course = await db.insert_treatment_course(
        TreatmentCourse(regimen_printed="SYNTH-OX", line_of_therapy=1, status="active"),
        patient_id=ERIKA_UUID,
    )
    cycle = await db.insert_treatment_cycle(
        TreatmentCycle(course_id=course.id, cycle_number_printed="C1", planned_at="2027-06"),
        patient_id=ERIKA_UUID,
    )
    request = await db.insert_medication_event(
        MedicationEvent(
            kind="request", course_id=course.id, cycle_id=cycle.id,
            ingredient_printed="Synthocillin", dose_value=50, dose_unit_ucum="mg",
        ),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    admin = await db.insert_medication_event(
        MedicationEvent(
            kind="administration", request_id=request.id, cycle_id=cycle.id,
            ingredient_printed="Synthocillin", source_document_id=doc_a,
        ),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    assert (await db.get_treatment_course(course.id, patient_id=ERIKA_UUID)).line_of_therapy == 1
    assert [c.id for c in await db.list_treatment_courses(patient_id=ERIKA_UUID)] == [course.id]
    cycles = await db.list_treatment_cycles(patient_id=ERIKA_UUID, course_id=course.id)
    assert [c.id for c in cycles] == [cycle.id]
    got = await db.get_medication_event(admin.id, patient_id=ERIKA_UUID)
    assert got.request_id == request.id and got.kind == "administration"
    events = await db.list_medication_events(patient_id=ERIKA_UUID, cycle_id=cycle.id)
    assert {e.id for e in events} == {request.id, admin.id}
    only_requests = await db.list_medication_events(patient_id=ERIKA_UUID, kind="request")
    assert [e.id for e in only_requests] == [request.id]


async def test_procedure_specimen_report_round_trip(two_patients):
    db, doc_a, _ = two_patients
    proc = await db.insert_procedure(
        Procedure(kind="surgery", printed_text="Synthetic resection", performed_at="2027-04"),
        patient_id=ERIKA_UUID,
    )
    assert (await db.get_procedure(proc.id, patient_id=ERIKA_UUID)).printed_text == (
        "Synthetic resection"
    )
    assert [p.id for p in await db.list_procedures(patient_id=ERIKA_UUID)] == [proc.id]
    spec = await db.insert_specimen(
        Specimen(type_printed="Serum", accession="SYN-A"), patient_id=ERIKA_UUID
    )
    rep = await db.insert_report(
        Report(panel_printed="Synthetic panel", status="final", specimen_id=spec.id,
               source_document_id=doc_a),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    assert (await db.get_specimen(spec.id, patient_id=ERIKA_UUID)).accession == "SYN-A"
    assert (await db.get_report(rep.id, patient_id=ERIKA_UUID)).specimen_id == spec.id


async def test_device_upsert_is_idempotent(db):
    a = await db.upsert_device(Device(kind="llm_extractor", name="synthetic-model", version="v1"))
    b = await db.upsert_device(Device(kind="llm_extractor", name="synthetic-model", version="v1"))
    assert a.id == b.id and a.fhir_id == b.fhir_id
    assert (await db.get_device(a.id)).name == "synthetic-model"


async def test_codings_identifiers_provenance_round_trip(two_patients):
    db, doc_a, _ = two_patients
    cond = await db.insert_condition(Condition(printed_text="Synthetic"), patient_id=ERIKA_UUID)
    await db.add_coding(
        Coding(owner_type="condition", owner_id=str(cond.id), system="urn:synthetic:icd",
               code="X99", is_original=True),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    codings = await db.list_codings("condition", cond.id, patient_id=ERIKA_UUID)
    assert [(c.code, c.is_original) for c in codings] == [("X99", True)]

    # shared reference owners carry no patient_id
    await db.add_coding(
        Coding(owner_type="fact_code", owner_id="CEA", system="http://loinc.org", code="SYN-1"),
        patient_id=None,
    )
    assert len(await db.list_codings("fact_code", "CEA", patient_id=None)) == 1
    org_id = await db.upsert_organization("SYNORG")
    await db.add_identifier(
        Identifier(owner_type="organization", owner_id=org_id, system=ID_SYSTEM_ICO,
                   value="SYN-ICO"),
        patient_id=None,
    )  # fmt: skip
    found = await db.find_by_identifier(ID_SYSTEM_ICO, "SYN-ICO", patient_id=None)
    assert [i.owner_id for i in found] == [org_id]

    spec = await db.insert_specimen(Specimen(accession="SYN-B"), patient_id=ERIKA_UUID)
    await db.add_identifier(
        Identifier(owner_type="specimen", owner_id=spec.id, system=ID_SYSTEM_ACCESSION,
                   value="SYN-B"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    assert len(await db.list_identifiers("specimen", spec.id, patient_id=ERIKA_UUID)) == 1

    device = await db.upsert_device(Device(kind="ocr_model", name="synthetic-ocr"))
    prov = await db.add_provenance(
        ProvenanceRecord(
            target_type="condition", target_id=cond.id, activity="extract",
            agent_role="assembler", agent_device_id=device.id, source_document_id=doc_a,
            page_number=1, quote="Synthetic",
        ),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    assert prov.fhir_id and prov.recorded_at
    rows = await db.list_provenance("condition", cond.id, patient_id=ERIKA_UUID)
    assert [r.id for r in rows] == [prov.id]


async def test_soft_delete_only(db):
    cond = await db.insert_condition(Condition(printed_text="Synthetic"), patient_id=ERIKA_UUID)
    assert await db.soft_delete_hard_fact(
        "condition", cond.id, patient_id=BOB_UUID, deleted_by="test"
    ) is False  # fmt: skip
    assert await db.soft_delete_hard_fact(
        "condition", cond.id, patient_id=ERIKA_UUID, deleted_by="test"
    )  # fmt: skip
    assert await db.get_condition(cond.id, patient_id=ERIKA_UUID) is None
    kept = await db.list_conditions(patient_id=ERIKA_UUID, include_deleted=True)
    assert [c.deleted_by for c in kept] == ["test"]


# ── isolation ──


async def test_foreign_patient_gets_nothing(two_patients):
    db, _, _ = two_patients
    cond = await db.insert_condition(Condition(printed_text="Synthetic"), patient_id=ERIKA_UUID)
    course = await db.insert_treatment_course(
        TreatmentCourse(regimen_printed="SYNTH"), patient_id=ERIKA_UUID
    )
    event = await db.insert_medication_event(
        MedicationEvent(kind="statement", ingredient_printed="Synthocillin"),
        patient_id=ERIKA_UUID,
    )
    proc = await db.insert_procedure(
        Procedure(kind="biopsy", printed_text="Synthetic"), patient_id=ERIKA_UUID
    )
    spec = await db.insert_specimen(Specimen(), patient_id=ERIKA_UUID)
    rep = await db.insert_report(Report(), patient_id=ERIKA_UUID)
    await db.add_provenance(
        ProvenanceRecord(target_type="condition", target_id=cond.id, activity="manual",
                         agent_role="author", agent_user="caregiver",
                         reason="no_source_document"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    await db.add_coding(
        Coding(owner_type="condition", owner_id=str(cond.id), system="urn:s", code="X"),
        patient_id=ERIKA_UUID,
    )

    assert await db.get_condition(cond.id, patient_id=BOB_UUID) is None
    assert await db.get_treatment_course(course.id, patient_id=BOB_UUID) is None
    assert await db.get_medication_event(event.id, patient_id=BOB_UUID) is None
    assert await db.get_procedure(proc.id, patient_id=BOB_UUID) is None
    assert await db.get_specimen(spec.id, patient_id=BOB_UUID) is None
    assert await db.get_report(rep.id, patient_id=BOB_UUID) is None
    assert await db.list_conditions(patient_id=BOB_UUID) == []
    assert await db.list_treatment_courses(patient_id=BOB_UUID) == []
    assert await db.list_medication_events(patient_id=BOB_UUID) == []
    assert await db.list_procedures(patient_id=BOB_UUID) == []
    assert await db.list_provenance("condition", cond.id, patient_id=BOB_UUID) == []
    assert await db.list_codings("condition", cond.id, patient_id=BOB_UUID) == []
    assert await db.list_codings("condition", cond.id, patient_id=None) == []


async def test_model_patient_mismatch_rejected(db):
    with pytest.raises(ValueError, match="does not match"):
        await db.insert_condition(
            Condition(printed_text="Synthetic", patient_id=BOB_UUID), patient_id=ERIKA_UUID
        )


async def test_cross_patient_references_rejected(two_patients):
    db, _, doc_b = two_patients
    bob_cond = await db.insert_condition(Condition(printed_text="Synthetic"), patient_id=BOB_UUID)
    bob_course = await db.insert_treatment_course(
        TreatmentCourse(regimen_printed="SYNTH"), patient_id=BOB_UUID
    )
    bob_cycle = await db.insert_treatment_cycle(
        TreatmentCycle(course_id=bob_course.id), patient_id=BOB_UUID
    )
    bob_req = await db.insert_medication_event(
        MedicationEvent(kind="request", ingredient_printed="Synthocillin"), patient_id=BOB_UUID
    )

    with pytest.raises(ValueError, match="primary_condition_id"):
        await db.insert_condition(
            Condition(printed_text="Secondary", primary_condition_id=bob_cond.id),
            patient_id=ERIKA_UUID,
        )
    with pytest.raises(ValueError, match="condition_id"):
        await db.insert_cancer_stage(
            CancerStage(condition_id=bob_cond.id, printed="Synthetic"), patient_id=ERIKA_UUID
        )
    with pytest.raises(ValueError, match="course_id"):
        await db.insert_treatment_cycle(
            TreatmentCycle(course_id=bob_course.id), patient_id=ERIKA_UUID
        )
    for field, value in (
        ("course_id", bob_course.id),
        ("cycle_id", bob_cycle.id),
        ("request_id", bob_req.id),
        ("source_document_id", doc_b),
    ):
        with pytest.raises(ValueError, match=field):
            await db.insert_medication_event(
                MedicationEvent(kind="administration", ingredient_printed="S", **{field: value}),
                patient_id=ERIKA_UUID,
            )

    jana_cond = await db.insert_condition(Condition(printed_text="Own"), patient_id=ERIKA_UUID)
    with pytest.raises(ValueError, match="source_document_id"):
        await db.add_provenance(
            ProvenanceRecord(target_type="condition", target_id=jana_cond.id,
                             activity="extract", agent_role="assembler", agent_user="t",
                             source_document_id=doc_b),
            patient_id=ERIKA_UUID,
        )  # fmt: skip
    with pytest.raises(ValueError, match="target"):
        await db.add_provenance(
            ProvenanceRecord(target_type="condition", target_id=bob_cond.id,
                             activity="extract", agent_role="assembler", agent_user="t"),
            patient_id=ERIKA_UUID,
        )  # fmt: skip
    with pytest.raises(ValueError, match="condition"):
        await db.add_coding(
            Coding(owner_type="condition", owner_id=str(bob_cond.id), system="urn:s", code="X"),
            patient_id=ERIKA_UUID,
        )
    # nothing was written for Jana by the rejected calls
    assert [c.id for c in await db.list_conditions(patient_id=ERIKA_UUID)] == [jana_cond.id]
    assert await db.list_medication_events(patient_id=ERIKA_UUID) == []


async def test_medication_link_consistency(db):
    course_a = await db.insert_treatment_course(
        TreatmentCourse(regimen_printed="A"), patient_id=ERIKA_UUID
    )
    course_b = await db.insert_treatment_course(
        TreatmentCourse(regimen_printed="B"), patient_id=ERIKA_UUID
    )
    cycle_b = await db.insert_treatment_cycle(
        TreatmentCycle(course_id=course_b.id), patient_id=ERIKA_UUID
    )
    with pytest.raises(ValueError, match="different course"):
        await db.insert_medication_event(
            MedicationEvent(kind="request", ingredient_printed="S", course_id=course_a.id,
                            cycle_id=cycle_b.id),
            patient_id=ERIKA_UUID,
        )  # fmt: skip
    stmt = await db.insert_medication_event(
        MedicationEvent(kind="statement", ingredient_printed="S"), patient_id=ERIKA_UUID
    )
    with pytest.raises(ValueError, match="medication request"):
        await db.insert_medication_event(
            MedicationEvent(kind="administration", ingredient_printed="S", request_id=stmt.id),
            patient_id=ERIKA_UUID,
        )


# ── allowlists ──


async def test_owner_and_target_allowlists(db):
    cond = await db.insert_condition(Condition(printed_text="Synthetic"), patient_id=ERIKA_UUID)
    with pytest.raises(ValueError, match="not allowed"):
        await db.add_coding(
            Coding(owner_type="patients", owner_id="1", system="urn:s", code="X"),
            patient_id=ERIKA_UUID,
        )
    with pytest.raises(ValueError, match="not allowed"):
        await db.add_identifier(
            Identifier(owner_type="device", owner_id=1, system="urn:s", value="X"),
            patient_id=None,
        )
    with pytest.raises(ValueError, match="patient_id is required"):
        await db.add_coding(
            Coding(owner_type="condition", owner_id=str(cond.id), system="urn:s", code="X"),
            patient_id=None,
        )
    with pytest.raises(ValueError, match="shared reference"):
        await db.add_coding(
            Coding(owner_type="fact_code", owner_id="CEA", system="urn:s", code="X"),
            patient_id=ERIKA_UUID,
        )
    with pytest.raises(ValueError, match="not found"):
        await db.add_coding(
            Coding(owner_type="fact_code", owner_id="NO_SUCH", system="urn:s", code="X"),
            patient_id=None,
        )
    base = {"target_id": cond.id, "agent_user": "t"}
    for bad in (
        {"target_type": "patients", "activity": "extract", "agent_role": "author"},
        {"target_type": "condition", "activity": "guess", "agent_role": "author"},
        {"target_type": "condition", "activity": "extract", "agent_role": "oracle"},
    ):
        with pytest.raises(ValueError, match="not allowed"):
            await db.add_provenance(ProvenanceRecord(**base, **bad), patient_id=ERIKA_UUID)
    with pytest.raises(ValueError, match="agent"):
        await db.add_provenance(
            ProvenanceRecord(target_type="condition", target_id=cond.id, activity="verify",
                             agent_role="verifier"),
            patient_id=ERIKA_UUID,
        )  # fmt: skip


# ── patient owners vs catalog owners (codings + identifiers) ──

CATALOG_OWNERS = {"organization", "department", "device", "fact_code"}


async def _make_owner(db, owner_type: str, doc_id: int) -> str:
    """Create one synthetic owner row of ``owner_type`` for Jana, return its id."""
    pid = ERIKA_UUID
    if owner_type == "fact":
        cur = await db.db.execute(
            "INSERT INTO clinical_records (patient_id, record_type, param, source) "
            "VALUES (?, 'lab', 'Synthetic', 'manual')",
            (pid,),
        )
        await db.db.commit()
        return str(cur.lastrowid)
    if owner_type == "document":
        return str(doc_id)
    if owner_type == "encounter":
        enc_id = await db.upsert_encounter(
            patient_id=pid, natural_key="syn-visit", start_at="2027-07", kind="visit",
            title=None, organization_id=None, department_id=None, practitioner_id=None,
        )  # fmt: skip
        return str(enc_id)
    if owner_type == "practitioner":
        return str(await db.upsert_practitioner(patient_id=pid, surname_token="Synthetic"))
    cond = await db.insert_condition(Condition(printed_text="Synthetic"), patient_id=pid)
    course = await db.insert_treatment_course(
        TreatmentCourse(regimen_printed="SYNTH"), patient_id=pid
    )
    if owner_type == "condition":
        return str(cond.id)
    if owner_type == "treatment_course":
        return str(course.id)
    if owner_type == "cancer_stage":
        made = await db.insert_cancer_stage(
            CancerStage(condition_id=cond.id, printed="synthetic"), patient_id=pid
        )
    elif owner_type == "treatment_cycle":
        made = await db.insert_treatment_cycle(TreatmentCycle(course_id=course.id), patient_id=pid)
    elif owner_type == "medication_event":
        made = await db.insert_medication_event(
            MedicationEvent(kind="statement", ingredient_printed="Synthocillin"), patient_id=pid
        )
    elif owner_type == "procedure":
        made = await db.insert_procedure(
            Procedure(kind="biopsy", printed_text="Synthetic"), patient_id=pid
        )
    elif owner_type == "specimen":
        made = await db.insert_specimen(Specimen(), patient_id=pid)
    elif owner_type == "report":
        made = await db.insert_report(Report(), patient_id=pid)
    else:
        raise AssertionError(f"no factory for {owner_type}")
    return str(made.id)


def test_owner_type_partition_is_complete():
    assert set(SHARED_OWNER_TABLES) | {"fact_code"} == CATALOG_OWNERS
    assert set(CODING_OWNER_TYPES) == set(PATIENT_OWNER_TABLES) | CATALOG_OWNERS
    assert set(IDENTIFIER_OWNER_TYPES) <= set(PATIENT_OWNER_TABLES) | CATALOG_OWNERS


@pytest.mark.parametrize("owner_type", sorted(PATIENT_OWNER_TABLES))
async def test_coding_patient_owner_requires_and_checks_patient(two_patients, owner_type):
    db, doc_a, _ = two_patients
    owner_id = await _make_owner(db, owner_type, doc_a)
    coding = Coding(owner_type=owner_type, owner_id=owner_id, system="urn:s", code="X")
    with pytest.raises(ValueError, match="patient_id is required"):
        await db.add_coding(coding, patient_id=None)
    with pytest.raises(ValueError, match="not found for this patient"):
        await db.add_coding(coding, patient_id=BOB_UUID)
    await db.add_coding(coding, patient_id=ERIKA_UUID)
    assert len(await db.list_codings(owner_type, owner_id, patient_id=ERIKA_UUID)) == 1
    assert await db.list_codings(owner_type, owner_id, patient_id=None) == []


@pytest.mark.parametrize("owner_type", sorted(CATALOG_OWNERS))
async def test_coding_catalog_owner_requires_null_patient(db, owner_type):
    if owner_type == "organization":
        owner_id = str(await db.upsert_organization("SYNORG"))
    elif owner_type == "department":
        owner_id = str(await db.upsert_department(await db.upsert_organization("SYNORG"), "D"))
    elif owner_type == "device":
        owner_id = str((await db.upsert_device(Device(kind="verifier", name="syn"))).id)
    else:
        owner_id = "CEA"
    coding = Coding(owner_type=owner_type, owner_id=owner_id, system="urn:s", code="X")
    with pytest.raises(ValueError, match="shared reference"):
        await db.add_coding(coding, patient_id=ERIKA_UUID)
    assert (await db.add_coding(coding, patient_id=None)).patient_id is None


@pytest.mark.parametrize(
    "owner_type", sorted(set(IDENTIFIER_OWNER_TYPES) & set(PATIENT_OWNER_TABLES))
)
async def test_identifier_patient_owner_requires_and_checks_patient(two_patients, owner_type):
    db, doc_a, _ = two_patients
    owner_id = int(await _make_owner(db, owner_type, doc_a))
    system = sorted(IDENTIFIER_SYSTEMS_BY_OWNER[owner_type])[0]
    ident = Identifier(owner_type=owner_type, owner_id=owner_id, system=system, value="SYN-X")
    with pytest.raises(ValueError, match="patient_id is required"):
        await db.add_identifier(ident, patient_id=None)
    with pytest.raises(ValueError, match="not found for this patient"):
        await db.add_identifier(ident, patient_id=BOB_UUID)
    await db.add_identifier(ident, patient_id=ERIKA_UUID)
    assert len(await db.list_identifiers(owner_type, owner_id, patient_id=ERIKA_UUID)) == 1


@pytest.mark.parametrize("owner_type", ["organization", "department"])
async def test_identifier_catalog_owner_requires_null_patient(db, owner_type):
    org_id = await db.upsert_organization("SYNORG")
    owner_id = org_id if owner_type == "organization" else await db.upsert_department(org_id, "D")
    ident = Identifier(
        owner_type=owner_type, owner_id=owner_id, system=ID_SYSTEM_UDZS_PROVIDER, value="SYN-P"
    )
    with pytest.raises(ValueError, match="shared reference"):
        await db.add_identifier(ident, patient_id=ERIKA_UUID)
    assert (await db.add_identifier(ident, patient_id=None)).patient_id is None


# ── identifiers: data minimisation ──

FORBIDDEN_SYSTEMS = [
    "urn:oid:synthetic-rodne-cislo",
    "rodné číslo",
    "RC",
    "national-id",
    "urn:synthetic:birth-number",
    "personal_id",
    "SSN",
    "insurance-number",
    "cislo-poistenca",
    "urn:synthetic:health-card",
    "EHIC",
    "MRN",
    "medical-record-number",
    "patient-id",
    "patient number",
    "chart number",
    "hospital case number",
]


@pytest.mark.parametrize("system", FORBIDDEN_SYSTEMS)
@pytest.mark.parametrize("owner_type", ["organization", "practitioner", "document", "condition"])
async def test_identifier_forbidden_systems_rejected_for_any_owner(db, system, owner_type):
    ident = Identifier(owner_type=owner_type, owner_id=1, system=system, value="SYN-X")
    with pytest.raises(ValueError, match="never stored"):
        await db.add_identifier(ident, patient_id=ERIKA_UUID)
    async with db.db.execute("SELECT COUNT(*) FROM identifiers") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_identifier_birth_number_shaped_value_rejected(db):
    org_id = await db.upsert_organization("SYNORG")
    ident = Identifier(
        owner_type="organization", owner_id=org_id, system=ID_SYSTEM_ICO, value="000000/0000"
    )
    with pytest.raises(ValueError, match="never stored"):
        await db.add_identifier(ident, patient_id=None)


async def test_identifier_system_allowlist_per_owner(db):
    org_id = await db.upsert_organization("SYNORG")
    prac_id = await db.upsert_practitioner(patient_id=ERIKA_UUID, surname_token="Synthetic")
    with pytest.raises(ValueError, match="not allowed for organization"):
        await db.add_identifier(
            Identifier(owner_type="organization", owner_id=org_id, system="urn:any", value="X"),
            patient_id=None,
        )
    with pytest.raises(ValueError, match="not allowed for practitioner"):
        await db.add_identifier(
            Identifier(owner_type="practitioner", owner_id=prac_id, system=ID_SYSTEM_ICO,
                       value="X"),
            patient_id=ERIKA_UUID,
        )  # fmt: skip
    ok = await db.add_identifier(
        Identifier(owner_type="practitioner", owner_id=prac_id, system=ID_SYSTEM_UDZS_DOCTOR,
                   value="SYN-D"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    assert ok.system == ID_SYSTEM_UDZS_DOCTOR
    for owner_type in ("condition", "encounter", "fact", "medication_event"):
        with pytest.raises(ValueError, match="is not allowed"):
            await db.add_identifier(
                Identifier(owner_type=owner_type, owner_id=1, system=ID_SYSTEM_ACCESSION,
                           value="X"),
                patient_id=ERIKA_UUID,
            )  # fmt: skip
