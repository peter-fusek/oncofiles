"""FHIR R4 read projection + MCP read surface (#716 phase 3a). Synthetic data only.

Every value, name, code and Drive id below is invented; every date is 2027 or
later. Values sit on their own lines so no line looks like a lab-report line.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from unittest.mock import MagicMock

import pytest

from oncofiles.fhir import search as fs
from oncofiles.fhir import structure
from oncofiles.fhir import terminology as term
from oncofiles.fhir.ids import ONCOFILES_FHIR_NAMESPACE, derived_id
from oncofiles.fhir.search import PatientIndex
from oncofiles.fhir.store import load_patient
from oncofiles.models import (
    Coding,
    Condition,
    Device,
    MedicationEvent,
    Procedure,
    ProvenanceRecord,
    TreatmentCourse,
    TreatmentCycle,
)
from oncofiles.persistent_oauth import (
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
)
from oncofiles.tools import fhir as tool
from tests.helpers import ERIKA_UUID, make_doc

BOB_UUID = "00000000-0000-4000-8000-000000000002"
ERIKA_SLUG = "erika-test"
BOB_SLUG = "bob-test"
AIAST = "AIAST"
STORED_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:synthetic:stored-fhir-id"))


class _Ctx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


async def _exec(db, sql: str, params: tuple = ()) -> int:
    cur = await db.db.execute(sql, params)
    await db.db.commit()
    return cur.lastrowid


async def _fact(db, pid: str, **cols) -> int:
    row = {"patient_id": pid, "record_type": "lab", "source": "curated-extract"}
    row.update(cols)
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    return await _exec(db, f"INSERT INTO clinical_records ({names}) VALUES ({marks})",
                       tuple(row.values()))  # fmt: skip


@pytest.fixture
async def world(db):
    """Two patients. Erika: labs, conditions, a regimen, meds, a procedure. Bob: one lab."""
    await _exec(db, "UPDATE patients SET slug = ? WHERE patient_id = ?", (ERIKA_SLUG, ERIKA_UUID))
    await _exec(
        db,
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (BOB_UUID, BOB_SLUG, "Bob Test", "bob@example.com"),
    )
    await _exec(
        db,
        "INSERT INTO patient_context (patient_id, context_json) VALUES (?, ?)",
        (ERIKA_UUID, json.dumps({"name": "Jana Synteticka", "sex": "female",
                                 "date_of_birth": "2027-01-02"})),
    )  # fmt: skip
    org = await _exec(
        db, "INSERT INTO organizations (code, name) VALUES (?, ?)", ("SynthHosp", "Synth Hospital")
    )
    other_org = await _exec(
        db, "INSERT INTO organizations (code, name) VALUES (?, ?)", ("OtherHosp", "Other Clinic")
    )
    doctor = await _exec(
        db,
        "INSERT INTO practitioners (patient_id, surname_token, display_name, organization_id) "
        "VALUES (?, ?, ?, ?)",
        (ERIKA_UUID, "Testovac", "MUDr. Ivan Testovac", org),
    )
    lab_doc = await db.insert_document(
        make_doc(file_id="fhir_a", document_date=date(2027, 3, 10), gdrive_id="synthDriveA"),
        patient_id=ERIKA_UUID,
    )
    await _exec(db, "UPDATE documents SET organization_id = ? WHERE id = ?", (org, lab_doc.id))
    report_doc = await db.insert_document(
        make_doc(file_id="gdrive:synthDriveB", category="report",
                 document_date=date(2027, 4, 1)),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    bob_doc = await db.insert_document(
        make_doc(file_id="fhir_b", gdrive_id="synthDriveC"), patient_id=BOB_UUID
    )
    await _exec(
        db, "UPDATE documents SET organization_id = ? WHERE id = ?", (other_org, bob_doc.id)
    )

    ids = {"lab_doc": lab_doc.id, "report_doc": report_doc.id, "bob_doc": bob_doc.id,
           "org": org, "doctor": doctor}  # fmt: skip
    ids["hgb"] = await _fact(
        db, ERIKA_UUID,
        code="HGB", param="Hemoglobín", unit="g/dl", unit_canonical="g/L",
        value_num=9.8,
        value_canonical=98.0,
        ref_range_low=12.0,
        ref_range_high=15.5,
        flag="L", occurred_at="2027-03-10T08:15", verification="machine",
        extracted_by="rules-synth-v1", source_document_id=lab_doc.id, page_number=1,
        evidence="Hemoglobín low", organization_id=org,
    )  # fmt: skip
    ids["plt"] = await _fact(
        db, ERIKA_UUID,
        code="PLT", param="Trombocyty", unit="10^9/l", unit_canonical="10^9/L",
        value_num=210.0,
        value_canonical=210.0,
        occurred_at="2027-03-10", verification="unverified",
        extracted_by="rules-synth-v1", source_document_id=lab_doc.id,
    )  # fmt: skip
    ids["cea"] = await _fact(
        db, ERIKA_UUID,
        record_type="tumor_marker", code="CEA", param="CEA v sére", unit="ug/L",
        value_num=4.2,
        value_canonical=4.2,
        unit_canonical="µg/L", occurred_at="2027-04-01", verification="verified",
        result_status="amended", source_document_id=report_doc.id,
        metadata_json=json.dumps({"date_range_start": "2027-03-20",
                                  "date_range_end": "2027-03-30"}),
    )  # fmt: skip
    ids["fhir_fact"] = await _fact(
        db, ERIKA_UUID,
        code="CRP", param="CRP", value_text="negat", occurred_at="2027-04-02",
        verification="machine", fhir_id=STORED_ID,
    )  # fmt: skip
    ids["bob_wbc"] = await _fact(
        db, BOB_UUID,
        code="WBC", param="Leukocyty",
        value_num=6.1,
        value_canonical=6.1,
        occurred_at="2027-05-05", verification="machine", source_document_id=bob_doc.id,
    )  # fmt: skip

    cond = await db.insert_condition(
        Condition(printed_text="Adenocarcinoma coli (synthetic)", category="problem-list-item",
                  clinical_status="active", clinical_certainty="confirmed",
                  onset_at="2027-02-01", asserted_at="2027-02-03",
                  verification="verified", extracted_by="llm-synth",
                  source_document_id=report_doc.id, page_number=2,
                  evidence="dg. adenocarcinoma coli"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    for system, code in ((term.ICD10_WHO, "X99.9"), ("icd-10-cm", "X99.9"), ("mkch-10", "X99.9")):
        await db.add_coding(
            Coding(owner_type="condition", owner_id=str(cond.id), system=system, code=code),
            patient_id=ERIKA_UUID,
        )
    unverified_cond = await db.insert_condition(
        Condition(printed_text="Hypertensio (synthetic)", verification="unverified",
                  extracted_by="llm-synth", onset_at="2027-01-15"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    course = await db.insert_treatment_course(
        TreatmentCourse(regimen_printed="SYNTHFOX", line_of_therapy=1, intent="palliative",
                        start_at="2027-02-10", status="active", condition_id=cond.id,
                        verification="machine", extracted_by="llm-synth"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    cycle = await db.insert_treatment_cycle(
        TreatmentCycle(course_id=course.id, cycle_number_printed="C2", actual_at="2027-02-24",
                       verification="machine", extracted_by="llm-synth"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    req = await db.insert_medication_event(
        MedicationEvent(kind="request", ingredient_printed="Synthoplatin", course_id=course.id,
                        dose_value=85.0,
                        dose_unit_ucum="mg/m2", authored_at="2027-02-09",
                        prescriber_practitioner_id=doctor, status="active",
                        verification="verified", extracted_by="llm-synth"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    for system, code in ((term.ATC, "L01XX99"), ("rxnorm", "999999")):
        await db.add_coding(
            Coding(owner_type="medication_event", owner_id=str(req.id), system=system, code=code),
            patient_id=ERIKA_UUID,
        )
    admin_ev = await db.insert_medication_event(
        MedicationEvent(kind="administration", ingredient_printed="Synthoplatin",
                        course_id=course.id, cycle_id=cycle.id, request_id=req.id,
                        verification="machine", extracted_by="llm-synth"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    proc = await db.insert_procedure(
        Procedure(kind="surgery", printed_text="Hemicolectomia dx (synthetic)",
                  performed_at="2027-01-20", condition_id=cond.id, verification="machine",
                  extracted_by="llm-synth"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    device = await db.upsert_device(Device(kind="llm-extractor", name="synth-extractor",
                                           version="v1"))  # fmt: skip
    await db.add_provenance(
        ProvenanceRecord(target_type="fact", target_id=ids["hgb"], activity="extract",
                         agent_role="assembler", agent_device_id=device.id,
                         source_document_id=lab_doc.id, page_number=1,
                         quote="Hemoglobín low", recorded_at="2027-03-11T10:00:00Z"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    await db.add_provenance(
        ProvenanceRecord(target_type="fact", target_id=ids["hgb"], activity="verify",
                         agent_role="verifier", agent_user="someone@example.com",
                         recorded_at="2027-03-12T10:00:00Z"),
        patient_id=ERIKA_UUID,
    )  # fmt: skip
    ids.update(cond=cond, unverified_cond=unverified_cond, course=course, cycle=cycle,
               req=req, admin_ev=admin_ev, proc=proc, device=device)  # fmt: skip
    return db, ids


async def _index(db, pid=ERIKA_UUID) -> PatientIndex:
    return PatientIndex(await load_patient(db, pid))


def _obs(index: PatientIndex, row_id: int) -> dict:
    return index.get("Observation", derived_id("clinical_records", row_id))


def _systems(cc: dict) -> set[str]:
    return {c["system"] for c in cc.get("coding", [])}


# ── structure: every projection is structurally valid FHIR R4 ──


async def test_every_projected_resource_is_structurally_valid(world):
    db, _ = world
    index = await _index(db)
    types = {r["resourceType"] for r in index.resources}
    assert {"Patient", "Observation", "Condition", "Procedure", "MedicationRequest",
            "MedicationAdministration", "CarePlan", "DocumentReference", "Organization",
            "Practitioner", "Device", "Provenance"} <= types  # fmt: skip
    for res in index.resources:
        assert structure.problems(res) == [], (res["resourceType"], structure.problems(res))


async def test_structural_check_catches_mistakes():
    bad_obs = {"resourceType": "Observation", "id": "x", "status": "final",
               "valueQuantity": {"value": 1}, "valueString": "a", "foo": ""}  # fmt: skip
    msgs = " ".join(structure.problems(bad_obs))
    assert "'code' missing" in msgs and "value[x] occurs 2" in msgs and "not allowed" in msgs
    bad_admin = {"resourceType": "MedicationAdministration", "id": "y", "status": "completed",
                 "subject": {"reference": "Patient/p"},
                 "medicationCodeableConcept": {"text": "x"}}  # fmt: skip
    assert "effective[x] must occur exactly once" in " ".join(structure.problems(bad_admin))
    cm = {
        "resourceType": "Condition",
        "id": "z",
        "subject": {"reference": "Patient/p"},
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "A"}]},
    }
    assert "forbidden code system" in " ".join(structure.problems(cm))
    assert "invalid dateTime" in " ".join(
        structure.problems({"resourceType": "Condition", "id": "c",
                            "subject": {"reference": "Patient/p"},
                            "onsetDateTime": "2027-03-10T08:15"})
    )  # fmt: skip


# ── Observation ──


async def test_observation_lab_shape(world):
    db, ids = world
    hgb = _obs(await _index(db), ids["hgb"])
    assert hgb["status"] == "final"
    assert hgb["category"][0]["coding"][0]["code"] == "laboratory"
    codes = {(c["system"], c["code"]) for c in hgb["code"]["coding"]}
    assert (term.LOINC, "718-7") in codes and (term.FACT_CODE_SYSTEM, "HGB") in codes
    assert hgb["code"]["text"] == "Hemoglobín"  # printed label = originalText
    q = hgb["valueQuantity"]
    assert q["system"] == term.UCUM and q["code"] == "g/L" and q["value"] == 98.0
    rr = hgb["referenceRange"][0]
    assert rr["low"]["value"] == 120.0  # scaled to g/L
    assert rr["high"]["value"] == 155.0
    assert hgb["interpretation"][0]["coding"][0]["code"] == "L"
    assert hgb["derivedFrom"][0]["reference"].startswith("DocumentReference/")
    assert hgb["performer"][0]["reference"].startswith("Organization/")
    # naive local time gets the Europe/Bratislava offset (FHIR requires one)
    assert hgb["effectiveDateTime"].startswith("2027-03-10T08:15:00+0")


async def test_ucum_units_for_our_canonical_units():
    assert term.ucum_code("10^9/L") == "10*9/L"
    assert term.ucum_code("µmol/L") == "umol/L"
    assert term.ucum_code("kU/L") == "k[U]/L"
    assert term.ucum_code("°C") == "Cel"
    assert term.ucum_code("nonsense") is None


async def test_verification_tag_and_aiast(world):
    db, ids = world
    index = await _index(db)
    hgb, plt, cea = (_obs(index, ids[k]) for k in ("hgb", "plt", "cea"))
    tag = {"system": term.VERIFICATION_SYSTEM, "code": "machine"}
    assert tag in hgb["meta"]["tag"]
    assert hgb["meta"]["security"][0]["code"] == AIAST
    assert hgb["meta"]["security"][0]["system"] == term.V3_OBSERVATION_VALUE
    assert {"system": term.VERIFICATION_SYSTEM, "code": "unverified"} in plt["meta"]["tag"]
    # unverified is NEVER mapped to preliminary
    assert plt["status"] == "final"
    # result_status wins when the report printed/implied one
    assert cea["status"] == "amended"
    cond = index.get("Condition", ids["cond"].fhir_id)
    assert cond["meta"]["security"][0]["code"] == AIAST
    assert {"system": term.VERIFICATION_SYSTEM, "code": "verified"} in cond["meta"]["tag"]
    # a document is not a fact: no verification tag, no AI label
    doc = index.get("DocumentReference", derived_id("documents", ids["lab_doc"]))
    assert "tag" not in doc.get("meta", {}) and "security" not in doc.get("meta", {})


async def test_range_dated_fact_gets_effective_period(world):
    db, ids = world
    cea = _obs(await _index(db), ids["cea"])
    assert "effectiveDateTime" not in cea
    assert cea["effectivePeriod"] == {"start": "2027-03-20", "end": "2027-03-30"}


async def test_text_value_and_stored_fhir_id(world):
    db, ids = world
    index = await _index(db)
    crp = index.get("Observation", STORED_ID)
    assert crp is not None and crp["valueString"] == "negat"


# ── codes: WHO ICD-10, ATC, never ICD-10-CM / RxNorm ──


async def test_code_systems_who_icd10_atc_only(world):
    db, ids = world
    index = await _index(db)
    cond = index.get("Condition", ids["cond"].fhir_id)
    assert term.ICD10_WHO in _systems(cond["code"])
    assert term.MKCH10 in _systems(cond["code"])
    assert "http://hl7.org/fhir/sid/icd-10-cm" not in _systems(cond["code"])
    req = index.get("MedicationRequest", ids["req"].fhir_id)
    med = _systems(req["medicationCodeableConcept"])
    assert term.ATC in med and not any("rxnorm" in s for s in med)
    blob = json.dumps(index.resources)
    assert "icd-10-cm" not in blob and "rxnorm" not in blob


# ── the #725 tables ──


async def test_condition_careplan_medication_procedure(world):
    db, ids = world
    index = await _index(db)
    cond = index.get("Condition", ids["cond"].fhir_id)
    assert cond["clinicalStatus"]["coding"][0]["code"] == "active"
    assert cond["verificationStatus"]["coding"][0]["code"] == "confirmed"
    assert cond["category"][0]["coding"][0]["code"] == "problem-list-item"
    assert {"url": term.CONDITION_ASSERTED_DATE_EXT, "valueDateTime": "2027-02-03"} in cond[
        "extension"
    ]
    course = index.get("CarePlan", ids["course"].fhir_id)
    cycle = index.get("CarePlan", ids["cycle"].fhir_id)
    assert course["status"] == "active" and course["intent"] == "plan"
    assert course["addresses"][0]["reference"] == f"Condition/{ids['cond'].fhir_id}"
    assert cycle["partOf"][0]["reference"] == f"CarePlan/{ids['course'].fhir_id}"
    assert cycle["status"] == "completed"
    req = index.get("MedicationRequest", ids["req"].fhir_id)
    dose = req["dosageInstruction"][0]["doseAndRate"][0]["doseQuantity"]
    assert dose["code"] == "mg/m2" and dose["system"] == term.UCUM
    assert req["basedOn"][0]["reference"] == f"CarePlan/{ids['course'].fhir_id}"
    assert req["requester"]["reference"].startswith("Practitioner/")
    adm = index.get("MedicationAdministration", ids["admin_ev"].fhir_id)
    assert adm["request"]["reference"] == f"MedicationRequest/{ids['req'].fhir_id}"
    assert "_effectiveDateTime" in adm  # 1..1, data-absent-reason when unknown
    proc = index.get("Procedure", ids["proc"].fhir_id)
    assert proc["status"] == "completed" and proc["performedDateTime"] == "2027-01-20"


async def test_patient_is_minimal(world):
    db, _ = world
    patient = (await _index(db)).patient
    assert patient["gender"] == "female" and patient["birthDate"] == "2027-01-02"
    assert "name" not in patient and "Synteticka" not in json.dumps(patient)
    assert patient["id"] == str(uuid.uuid5(ONCOFILES_FHIR_NAMESPACE, f"patients:{ERIKA_UUID}"))


async def test_document_reference(world):
    db, ids = world
    index = await _index(db)
    lab = index.get("DocumentReference", derived_id("documents", ids["lab_doc"]))
    assert lab["status"] == "current"
    assert {"system": term.LOINC, "code": "11502-2", "display": "Laboratory report"} in lab["type"][
        "coding"
    ]
    assert lab["content"][0]["attachment"]["url"].endswith("/synthDriveA/view")
    assert lab["context"]["period"]["start"] == "2027-03-10"
    rep = index.get("DocumentReference", derived_id("documents", ids["report_doc"]))
    assert rep["content"][0]["attachment"]["url"].endswith("/synthDriveB/view")


# ── Provenance ──


async def test_provenance_assembler_verifier_and_quotation(world):
    db, ids = world
    index = await _index(db)
    hgb = _obs(index, ids["hgb"])
    provs = index.provenance_by_target[f"Observation/{hgb['id']}"]
    roles = {p["agent"][0]["type"]["coding"][0]["code"] for p in provs}
    assert roles == {"assembler", "verifier"}
    assembler = next(p for p in provs if p["agent"][0]["type"]["coding"][0]["code"] == "assembler")
    assert assembler["agent"][0]["who"]["reference"] == f"Device/{ids['device'].fhir_id}"
    entity = assembler["entity"][0]
    assert entity["role"] == "source"
    assert (
        entity["what"]["reference"]
        == f"DocumentReference/{derived_id('documents', ids['lab_doc'])}"
    )
    ext = {e["url"]: e for e in entity["extension"]}
    assert ext[term.EXT_SOURCE_PAGE]["valueInteger"] == 1
    assert ext[term.EXT_SOURCE_QUOTATION]["valueString"] == "Hemoglobín low"
    # a verifier's e-mail never leaves Oncofiles
    assert "someone@example.com" not in json.dumps(provs)
    # facts without stored provenance get one derived from their columns
    cond_provs = index.provenance_by_target[f"Condition/{ids['cond'].fhir_id}"]
    codes = {a["type"]["coding"][0]["code"] for a in cond_provs[0]["agent"]}
    assert codes == {"assembler", "verifier"}
    quote = cond_provs[0]["entity"][0]["extension"]
    assert {"url": term.EXT_SOURCE_QUOTATION, "valueString": "dg. adenocarcinoma coli"} in quote


# ── stable ids ──


async def test_ids_are_stable_across_loads(world):
    db, ids = world
    a = {f"{r['resourceType']}/{r['id']}" for r in (await _index(db)).resources}
    b = {f"{r['resourceType']}/{r['id']}" for r in (await _index(db)).resources}
    assert a == b
    assert f"Condition/{ids['cond'].fhir_id}" in a  # #725 rows use their fhir_id


# ── isolation ──


async def test_projection_contains_only_this_patients_rows(world):
    db, ids = world
    erika = await _index(db)
    bob = await _index(db, BOB_UUID)
    erika_keys = set(erika.by_key)
    assert f"Observation/{derived_id('clinical_records', ids['bob_wbc'])}" not in erika_keys
    assert f"DocumentReference/{derived_id('documents', ids['bob_doc'])}" not in erika_keys
    assert not (erika_keys - {f"Organization/{derived_id('organizations', ids['org'])}"}) & set(
        bob.by_key
    )
    # shared organisations appear only where the patient's own rows reference them
    assert "Other Clinic" not in json.dumps(erika.resources)
    assert "Synth Hospital" not in json.dumps(bob.resources)
    assert [r["resourceType"] for r in bob.resources].count("Observation") == 1


@pytest.fixture
def non_admin_bound_to_erika():
    tokens = (
        _verified_caller_is_admin.set(False),
        _verified_caller_email.set(""),
        _verified_patient_id.set(ERIKA_UUID),
    )
    yield
    _verified_caller_is_admin.reset(tokens[0])
    _verified_caller_email.reset(tokens[1])
    _verified_patient_id.reset(tokens[2])


@pytest.fixture
def as_admin():
    tok = _verified_caller_is_admin.set(True)
    yield
    _verified_caller_is_admin.reset(tok)


@pytest.mark.parametrize(
    "call",
    [
        lambda ctx: tool.fhir_search(ctx, type="Observation", patient_slug=BOB_SLUG),
        lambda ctx: tool.fhir_read(ctx, type="Observation", id="x", patient_slug=BOB_SLUG),
        lambda ctx: tool.patient_everything(ctx, patient_slug=BOB_SLUG),
        lambda ctx: tool.export_patient_summary(ctx, patient_slug=BOB_SLUG),
    ],
)
async def test_every_fhir_tool_denies_a_foreign_patient(world, non_admin_bound_to_erika, call):
    db, _ = world
    with pytest.raises(ValueError, match="access denied"):
        await call(_Ctx(db))


async def test_foreign_id_reads_as_not_found(world, non_admin_bound_to_erika):
    db, ids = world
    bob_obs = derived_id("clinical_records", ids["bob_wbc"])
    out = json.loads(await tool.fhir_read(_Ctx(db), type="Observation", id=bob_obs))
    assert out["resourceType"] == "OperationOutcome"
    assert out["issue"][0]["code"] == "not-found"
    own = derived_id("clinical_records", ids["hgb"])
    assert json.loads(await tool.fhir_read(_Ctx(db), type="Observation", id=own))["id"] == own


async def test_admin_without_slug_is_refused(world, as_admin):
    db, _ = world
    for call in (
        tool.fhir_search(_Ctx(db), type="Observation"),
        tool.fhir_read(_Ctx(db), type="Patient", id="x"),
        tool.patient_everything(_Ctx(db)),
        tool.export_patient_summary(_Ctx(db)),
    ):
        out = json.loads(await call)
        assert out["resourceType"] == "OperationOutcome"
        assert "patient_slug" in out["issue"][0]["diagnostics"]


async def test_admin_with_slug_gets_only_that_patient(world, as_admin):
    db, ids = world
    out = json.loads(await tool.fhir_search(_Ctx(db), type="Observation", patient_slug=BOB_SLUG))
    got = [e["resource"]["id"] for e in out["entry"]]
    assert got == [derived_id("clinical_records", ids["bob_wbc"])]


# ── search parameters ──


async def _ids(index, rtype, params, **kw) -> list[str]:
    bundle = fs.search(index, rtype, params, **kw)
    assert structure.problems(bundle) == []
    return [e["resource"]["id"] for e in bundle.get("entry", []) if e["search"]["mode"] == "match"]


async def test_search_by_code_category_date_status_tag(world):
    db, ids = world
    index = await _index(db)
    hgb = derived_id("clinical_records", ids["hgb"])
    plt = derived_id("clinical_records", ids["plt"])
    cea = derived_id("clinical_records", ids["cea"])
    assert await _ids(index, "Observation", {"code": "http://loinc.org|718-7"}) == [hgb]
    assert await _ids(index, "Observation", {"code": "|HGB"}) == []  # |code = no system
    assert await _ids(index, "Observation", {"code": "HGB"}) == [hgb]
    assert await _ids(index, "Observation", {"code:text": "trombo"}) == [plt]
    assert set(await _ids(index, "Observation", {"code": "718-7,PLT"})) == {hgb, plt}  # OR
    in_march = await _ids(index, "Observation", {"date": ["ge2027-03-01", "le2027-03-31"]})
    assert set(in_march) == {hgb, plt, cea}  # CEA's printed range lies in March
    assert await _ids(index, "Observation", {"status": "amended"}) == [cea]
    tagged = await _ids(index, "Observation", {"_tag": f"{term.VERIFICATION_SYSTEM}|unverified"})
    assert tagged == [plt]
    assert await _ids(index, "Observation", {"category": "vital-signs"}) == []
    assert await _ids(index, "Condition", {"code": f"{term.ICD10_WHO}|X99.9"}) == [
        ids["cond"].fhir_id
    ]
    assert await _ids(index, "MedicationRequest", {"code": f"{term.ATC}|L01XX99"}) == [
        ids["req"].fhir_id
    ]
    assert len(await _ids(index, "DocumentReference", {"type": f"{term.LOINC}|11502-2"})) == 1


async def test_search_newest_first_and_revinclude(world):
    db, ids = world
    index = await _index(db)
    bundle = fs.search(index, "Observation", {"code": "HGB", "_revinclude": "Provenance:target"})
    modes = [e["search"]["mode"] for e in bundle["entry"]]
    assert modes == ["match", "include", "include"]
    assert bundle["total"] == 1
    got = await _ids(index, "Observation", {})
    assert got[0] == STORED_ID  # CRP, the newest date first


async def test_search_errors_are_operation_outcomes(world, non_admin_bound_to_erika):
    db, _ = world
    ctx = _Ctx(db)
    for kwargs, code in (
        ({"type": "Observation", "params": {"subject": "x"}}, "not-supported"),
        ({"type": "DiagnosticReport"}, "not-supported"),
        ({"type": "Observation", "params": {"date": "yesterday"}}, "invalid"),
        ({"type": "Observation", "params": {"_revinclude": "Provenance:entity"}}, "not-supported"),
        ({"type": "Observation", "page_token": "garbage"}, "invalid"),
    ):
        out = json.loads(await tool.fhir_search(ctx, **kwargs))
        assert out["resourceType"] == "OperationOutcome", kwargs
        assert out["issue"][0]["code"] == code, kwargs


# ── paging ──


async def test_paging_with_count_and_token(world):
    db, _ = world
    index = await _index(db)
    seen, token = [], None
    for _ in range(10):
        bundle = fs.search(index, "Observation", {}, count=1, page_token=token)
        assert bundle["total"] == 4 and len(bundle["entry"]) == 1
        seen += [e["resource"]["id"] for e in bundle["entry"]]
        nxt = [link for link in bundle["link"] if link["relation"] == "next"]
        if not nxt:
            break
        token = nxt[0]["url"].split("page_token=")[1]
    assert len(seen) == 4 and len(set(seen)) == 4
    # a token belongs to its query
    with pytest.raises(fs.SearchError):
        fs.search(index, "Observation", {"code": "HGB"}, count=1, page_token=token)


async def test_paging_respects_the_byte_budget(world, monkeypatch):
    db, _ = world
    for i in range(30):
        await _fact(db, ERIKA_UUID, code="GLU", param=f"Glukoza {'x' * 2000}",
                    occurred_at="2027-06-01", value_text=f"t{i}")  # fmt: skip
    index = await _index(db)
    monkeypatch.setattr(fs, "PAGE_BYTE_BUDGET", 10_000)
    bundle = fs.search(index, "Observation", {"code": "GLU"}, count=50)
    assert 1 <= len(bundle["entry"]) < 30
    size = len(json.dumps(bundle["entry"], ensure_ascii=False, separators=(",", ":")))
    assert size <= 10_000
    assert any(link["relation"] == "next" for link in bundle["link"])
    monkeypatch.setattr(fs, "PAGE_BYTE_BUDGET", 150_000)
    full = fs.search(index, "Observation", {"code": "GLU"}, count=200)
    assert len(full["entry"]) == 30


async def test_count_is_clamped(world):
    db, _ = world
    index = await _index(db)
    assert "_count=200" in fs.search(index, "Observation", {}, count=5000)["link"][0]["url"]


# ── $everything ──


async def test_patient_everything(world):
    db, ids = world
    index = await _index(db)
    bundle = fs.everything(index, count=200)
    assert structure.problems(bundle) == []
    assert bundle["entry"][0]["resource"]["resourceType"] == "Patient"
    assert bundle["total"] == len(index.resources)
    only = fs.everything(index, types="Condition,Procedure", count=200)
    assert {e["resource"]["resourceType"] for e in only["entry"]} == {"Condition", "Procedure"}
    window = fs.everything(index, start="2027-03-01", end="2027-03-31", types=["Observation"])
    assert {e["resource"]["id"] for e in window["entry"]} == {
        derived_id("clinical_records", ids[k]) for k in ("hgb", "plt", "cea")
    }


# ── IPS summary ──


async def test_patient_summary_excludes_unverified_by_default(world):
    db, ids = world
    index = await _index(db)
    doc = fs.patient_summary(index)
    assert structure.problems(doc) == []
    comp = doc["entry"][0]["resource"]
    assert comp["resourceType"] == "Composition"
    assert comp["type"]["coding"][0]["code"] == "60591-5"
    sections = {s["code"]["coding"][0]["code"]: s for s in comp["section"]}
    assert set(sections) == {"11450-4", "10160-0", "48765-2", "30954-2"}
    allergies = sections["48765-2"]
    assert allergies["emptyReason"]["coding"][0]["code"] == "unavailable"
    assert "entry" not in allergies
    ids_in = {e["resource"]["id"] for e in doc["entry"]}
    assert derived_id("clinical_records", ids["plt"]) not in ids_in
    assert ids["unverified_cond"].fhir_id not in ids_in
    assert derived_id("clinical_records", ids["hgb"]) in ids_in
    assert ids["cond"].fhir_id in ids_in
    # every reference inside the document resolves to an entry
    full_urls = {e["fullUrl"] for e in doc["entry"]}
    refs = fs._refs_in(doc["entry"])
    local = [r for r in refs if r.startswith("urn:uuid:")]
    assert local and set(local) <= full_urls
    assert all(r.startswith("urn:uuid:") or "/_history/" in r for r in refs)

    with_unverified = fs.patient_summary(index, include_unverified=True)
    ids_all = {e["resource"]["id"] for e in with_unverified["entry"]}
    assert derived_id("clinical_records", ids["plt"]) in ids_all
    assert ids["unverified_cond"].fhir_id in ids_all


async def test_export_tool_and_empty_patient(world, as_admin):
    db, _ = world
    out = json.loads(await tool.export_patient_summary(_Ctx(db), patient_slug=BOB_SLUG))
    assert out["type"] == "document" and structure.problems(out) == []
    sections = out["entry"][0]["resource"]["section"]
    empty = [s for s in sections if "emptyReason" in s]
    assert len(empty) == 3  # Bob has one lab result and nothing else


# ── capabilities + registration ──


async def test_capabilities():
    cs = json.loads(await tool.fhir_capabilities(None))
    assert structure.problems(cs) == []
    assert cs["fhirVersion"] == "4.0.1"
    obs = next(r for r in cs["rest"][0]["resource"] if r["type"] == "Observation")
    assert {p["name"] for p in obs["searchParam"]} >= {"code", "date", "category", "status", "_tag"}
    assert obs["searchRevInclude"] == ["Provenance:target"]
    assert "icd-10-cm" not in json.dumps(cs).replace("Never ICD-10-CM", "")


def _annotations_by_tool() -> dict[str, dict]:
    mcp = MagicMock()
    out: dict[str, dict] = {}

    def tool_decorator(annotations=None):
        return lambda fn: out.__setitem__(fn.__name__, annotations or {})

    mcp.tool.side_effect = tool_decorator
    tool.register(mcp)
    return out


def test_fhir_lookup_tools_are_read_only():
    ann = _annotations_by_tool()
    assert set(ann) == {"fhir_capabilities", "fhir_search", "fhir_read",
                        "patient_everything", "export_patient_summary"}  # fmt: skip
    for name in ("fhir_capabilities", "fhir_search", "fhir_read"):
        assert ann[name]["readOnlyHint"] is True, name


def test_fhir_bulk_egress_tools_are_never_read_only():
    """Bulk export of a patient's record must keep manual approval in MCP clients
    (same class as export_manifest / export_document_package)."""
    assert {"patient_everything", "export_patient_summary"} == tool.FHIR_EGRESS_TOOLS
    ann = _annotations_by_tool()
    for name in tool.FHIR_EGRESS_TOOLS:
        assert not ann[name].get("readOnlyHint", False), name
