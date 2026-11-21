"""#928: preventive care from the facts model, interpretation tools off the
user surface, journey / export timelines from ``timeline_v``.

Synthetic patients, texts and dates only (years 2027+).
"""

from __future__ import annotations

import json
from datetime import date

from oncofiles import patient_context
from oncofiles.models import DocumentCategory, Procedure, TreatmentEvent
from oncofiles.preventive_care import (
    SCREENING_PROTOCOLS,
    add_months,
    get_preventive_care_status,
)
from oncofiles.tool_surface import OPERATOR_TOOLS, USER_TOOLS
from oncofiles.tools import conversations as conv_tools
from oncofiles.tools import export as export_tools
from oncofiles.tools import lab_trends
from tests.helpers import PATIENT_A_UUID, make_doc

REF = date(2031, 3, 15)


class _StubCtx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


def _general(dob: str = "1975-08-31", sex: str = "male") -> None:
    patient_context._contexts[PATIENT_A_UUID] = {
        "name": "Synthetic General",
        "patient_type": "general",
        "date_of_birth": dob,
        "sex": sex,
    }


async def _proc(db, kind: str, text: str, at: str | None, doc_id: int | None = None, **kw):
    return await db.insert_procedure(
        Procedure(
            kind=kind,
            printed_text=text,
            performed_at=at,
            source_document_id=doc_id,
            page_number=1 if doc_id else None,
            verification=kw.pop("verification", "machine"),
            extracted_by="llm-clinical-v11/synthetic-model",
            **kw,
        ),
        patient_id=PATIENT_A_UUID,
    )


# ── calendar months ─────────────────────────────────────────────────────────


def test_add_months_is_calendar_months_not_30_days():
    assert add_months(date(2027, 8, 31), 6) == date(2028, 2, 29)  # leap year clamp
    assert add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)
    assert add_months(date(2027, 5, 10), 120) == date(2037, 5, 10)
    assert add_months(date(2027, 11, 30), 3) == date(2028, 2, 29)


def test_every_guideline_item_names_its_source():
    for p in SCREENING_PROTOCOLS:
        assert p["source"].strip(), p["id"]
        assert p["match"], p["id"]


# ── get_preventive_care_status ─────────────────────────────────────────────


async def test_general_patient_with_dob_and_sex_reads_procedures_and_documents(db):
    """The old code raised TypeError (wrong list_treatment_events signature)."""
    _general()
    colo_doc = await db.insert_document(
        make_doc(
            file_id="f-colo",
            filename="20290610_Synthetic_Clinic_Preventive_Colonoscopy.pdf",
            category=DocumentCategory.PREVENTIVE,
            document_date=date(2029, 6, 10),
        ),
        patient_id=PATIENT_A_UUID,
    )
    colo = await _proc(db, "screening", "Kolonoskopia, bez nálezu", "2029-06-10", colo_doc.id)
    tet = await _proc(db, "vaccination", "Tetanus-diftéria (synthetic vaccine)", "2027-04-02")
    # flu shot without a printed date: dated by its document
    flu_doc = await db.insert_document(
        make_doc(
            file_id="f-flu",
            filename="20300920_Synthetic_Clinic_Vaccination_Influenza.pdf",
            category=DocumentCategory.VACCINATION,
            document_date=date(2030, 9, 20),
        ),
        patient_id=PATIENT_A_UUID,
    )
    await _proc(db, "vaccination", "Očkovanie proti chrípke", None, flu_doc.id)
    dental = await db.insert_document(
        make_doc(
            file_id="f-dent",
            filename="20300801_Synthetic_Clinic_Dental_Checkup.pdf",
            category=DocumentCategory.DENTAL,
            document_date=date(2030, 8, 1),
        ),
        patient_id=PATIENT_A_UUID,
    )
    await _proc(db, "screening", "Synthetic unrelated examination", "2030-01-05")
    # a manual entry with an explicit id (add_treatment_event)
    await db.insert_treatment_event(
        TreatmentEvent(
            event_date=date(2028, 2, 1),
            event_type="screening",
            title="Invented check",
            metadata=json.dumps({"screening_id": "ophthalmology"}),
        ),
        patient_id=PATIENT_A_UUID,
    )

    out = json.loads(await get_preventive_care_status(db, PATIENT_A_UUID, reference_date=REF))
    assert "error" not in out
    by_id = {s["id"]: s for s in out["screenings"]}
    assert out["summary"]["age"] == 55
    assert out["summary"]["sex"] == "male"
    assert "not a fact about this patient" in out["guideline_note"]
    assert "mammography" not in by_id  # sex range
    assert "psa_screening" in by_id

    c = by_id["colonoscopy"]
    assert c["guideline"]["kind"] == "guideline_interval"
    assert c["guideline"]["source"] == "EU Council Recommendation 2022/C 473/01"
    assert c["last_evidence_date"] == "2029-06-10"
    assert c["next_by_guideline"] == "2039-06-10"  # 120 calendar months
    assert c["status"] == "within_interval"
    ev = c["evidence"][0]
    assert ev["source"] == "procedure"
    assert ev["procedure_id"] == colo.id
    assert ev["document_id"] == colo_doc.id
    assert ev["page_number"] == 1
    assert ev["date_source"] == "printed"
    assert ev["verification"] == "machine"
    assert ev["method"] == "llm-clinical"
    # the preventive document itself is evidence too (filename names it)
    assert any(e["source"] == "document" and e["document_id"] == colo_doc.id for e in c["evidence"])

    t = by_id["tetanus_booster"]
    assert t["evidence"][0]["procedure_id"] == tet.id
    assert t["next_by_guideline"] == "2037-04-02"

    f = by_id["flu_vaccine"]
    assert f["last_evidence_date"] == "2030-09-20"
    assert f["evidence"][0]["date_source"] == "document"
    assert f["next_by_guideline"] == "2031-09-20"
    assert f["status"] == "within_interval"

    d = by_id["dental_checkup"]
    assert d["evidence"][0]["document_id"] == dental.id
    assert d["next_by_guideline"] == "2031-02-01"
    assert d["status"] == "past_interval"

    o = by_id["ophthalmology"]
    assert o["evidence"][0]["source"] == "treatment_event"
    assert o["status"] == "past_interval"  # 2028-02-01 + 24 months < REF

    assert by_id["psa_screening"]["status"] == "no_evidence"
    assert by_id["psa_screening"]["evidence"] == []
    # nothing the records hold is hidden
    assert [u["text"] for u in out["unmatched_evidence"]] == ["Synthetic unrelated examination"]
    # vaccination evidence never counts for a screening item
    assert all(e["kind"] != "vaccination" for e in by_id["fobt_fit"]["evidence"])


async def test_due_soon_uses_calendar_months(db):
    _general(dob="1980-01-10", sex="female")
    await _proc(db, "screening", "Mamografia obojstranná", "2029-05-20")
    out = json.loads(await get_preventive_care_status(db, PATIENT_A_UUID, reference_date=REF))
    m = {s["id"]: s for s in out["screenings"]}["mammography"]
    assert m["next_by_guideline"] == "2031-05-20"
    assert m["status"] == "due_soon"  # within 3 calendar months of 2031-03-15


async def test_deleted_procedure_is_not_evidence(db):
    _general()
    p = await _proc(db, "screening", "Kolonoskopia (synthetic)", "2029-07-23")
    await db.db.execute(
        "UPDATE procedures SET deleted_at = '2031-02-17T08:41:00Z' WHERE id = ?", (p.id,)
    )
    await db.db.commit()
    out = json.loads(await get_preventive_care_status(db, PATIENT_A_UUID, reference_date=REF))
    c = {s["id"]: s for s in out["screenings"]}["colonoscopy"]
    assert c["status"] == "no_evidence"


async def test_missing_dob_or_sex_and_oncology_patient(db):
    patient_context._contexts[PATIENT_A_UUID] = {
        "patient_type": "general",
        "date_of_birth": "1975-08-31",
    }
    assert "error" in json.loads(await get_preventive_care_status(db, PATIENT_A_UUID))
    patient_context._contexts[PATIENT_A_UUID] = {"patient_type": "oncology"}
    assert "oncology" in json.loads(await get_preventive_care_status(db, PATIENT_A_UUID))["error"]


async def test_mcp_tool_wrapper_runs(db):
    _general()
    out = json.loads(await lab_trends.get_preventive_care_status(_StubCtx(db)))
    assert "screenings" in out and "error" not in out


# ── user surface ──────────────────────────────────────────────────────────


MOVED = {
    "get_lab_safety_check",
    "get_precycle_checklist",
    "analyze_labs",
    "compare_labs",
    "fetch_clinical_trials",
    "add_clinical_analysis",
}


def test_data_tools_stay_on_the_user_surface():
    # the moved tools themselves are locked in tests/test_tool_surface.py
    assert MOVED <= OPERATOR_TOOLS
    assert {"get_preventive_care_status", "get_journey_timeline", "export_document_package"} <= (
        USER_TOOLS
    )


def test_instructions_promise_no_moved_tool():
    from oncofiles.server import _MCP_INSTRUCTIONS

    for name in MOVED:
        assert name not in _MCP_INSTRUCTIONS
    assert "pre-cycle" not in _MCP_INSTRUCTIONS


# ── timelines from the facts model ─────────────────────────────────────────


async def _legacy_event(db):
    return await db.insert_treatment_event(
        TreatmentEvent(event_date=date(2027, 3, 1), event_type="chemo", title="Invented cycle"),
        patient_id=PATIENT_A_UUID,
    )


async def test_journey_timeline_reads_facts_and_drops_legacy(db):
    doc = await db.insert_document(
        make_doc(file_id="f-op", document_date=date(2027, 6, 1)), patient_id=PATIENT_A_UUID
    )
    proc = await _proc(db, "surgery", "Synthetic resection", "2027-06-01", doc.id)
    await _legacy_event(db)
    out = json.loads(await conv_tools.get_journey_timeline(_StubCtx(db)))
    assert out["timeline_source"] == "facts"
    tables = {i["source_table"] for i in out["items"]}
    assert "treatment_events" not in tables
    facts = [i for i in out["items"] if i["source_table"] == "facts"]
    assert len(facts) == 1
    f = facts[0]
    assert f["id"] == proc.id and f["entity"] == "procedure"
    assert f["date"] == "2027-06-01"
    assert f["title"] == "Synthetic resection"
    assert f["summary"] == "surgery"
    assert f["document_id"] == doc.id and f["page_number"] == 1
    assert f["method"] == "llm-clinical"
    assert f["verification_label"]
    # documents are still listed once, from the documents table
    assert [i["id"] for i in out["items"] if i["source_table"] == "documents"] == [doc.id]
    # a legacy filter name maps to the matching facts rows
    tx = json.loads(
        await conv_tools.get_journey_timeline(_StubCtx(db), source_tables="treatment_events")
    )
    assert [i["source_table"] for i in tx["items"]] == ["facts"]
    labs = json.loads(
        await conv_tools.get_journey_timeline(_StubCtx(db), source_tables="lab_values")
    )
    assert labs["items"] == []


async def test_journey_timeline_legacy_fallback_without_facts(db):
    ev = await _legacy_event(db)
    out = json.loads(await conv_tools.get_journey_timeline(_StubCtx(db)))
    assert out["timeline_source"] == "legacy"
    assert ("treatment_events", ev.id) in {(i["source_table"], i["id"]) for i in out["items"]}


async def test_export_package_timeline_from_facts(db):
    doc = await db.insert_document(
        make_doc(file_id="f-op2", document_date=date(2027, 6, 1)), patient_id=PATIENT_A_UUID
    )
    await _proc(db, "biopsy", "Synthetic biopsy", "2027-05-01", doc.id)
    await _proc(db, "surgery", "Synthetic resection", "2027-06-01", doc.id)
    await _legacy_event(db)
    out = json.loads(await export_tools.export_document_package(_StubCtx(db)))
    assert out["timeline_source"] == "facts"
    tl = out["treatment_timeline"]
    assert [t["title"] for t in tl] == ["Synthetic biopsy", "Synthetic resection"]  # oldest first
    assert tl[0]["event_date"] == "2027-05-01"
    assert tl[0]["source_document_id"] == doc.id
    assert {"id", "event_date", "event_type", "title", "notes"} <= set(tl[0])


async def test_export_package_legacy_fallback(db):
    ev = await _legacy_event(db)
    out = json.loads(await export_tools.export_document_package(_StubCtx(db)))
    assert out["timeline_source"] == "legacy"
    assert [t["id"] for t in out["treatment_timeline"]] == [ev.id]
