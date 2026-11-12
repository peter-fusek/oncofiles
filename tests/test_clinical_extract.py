"""LLM-first extraction of the non-lab kinds (issue #726, item four).

Phase 2b of the hard-data model epic.

Synthetic data only: invented drugs, labels, values and dates (2027+). The
documents are the worked examples of the prompt (``llm_extract_clinical.
EXAMPLES``) and a few invented lines; the local model is always mocked.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import cloud_guard, patient_context, pre_store
from oncofiles.cloud_guard import CloudForbiddenError, local_only
from oncofiles.curated import clinical_store, llm, verifier
from oncofiles.curated import llm_extract_clinical as lx
from oncofiles.curated import verifier_clinical as vc
from oncofiles.curated.facts_rules import Vocabulary
from oncofiles.tools import clinical_facts as cf
from oncofiles.tools import curated as tool
from oncofiles.tools import treatment as tt
from tests.helpers import ERIKA_UUID

BOB_UUID = "00000000-0000-4000-8000-000000000002"
DOB = "2027-01-02"  # invented
DAY = "2027-07-12"  # document date of the seeded documents
DOC_A = 7261
DOC_B = DOC_A + 1
DOC_BOB = DOC_B + 1
PAGES_A = [page for page, _ in lx.EXAMPLES[:2]]  # oncology report, CT report
# discharge, postponement, empty page (the #773 date example stays out: fixed counts)
PAGES_B = [page for page, _ in lx.EXAMPLES if page not in PAGES_A and page != lx._EX5]
_, EX1_ANSWER = next(iter(lx.EXAMPLES))
EX1_DIAGNOSIS = next(iter(EX1_ANSWER["diagnoses"]))
ANSWERS = {page: answer for page, answer in lx.EXAMPLES}
HARD_TABLES = (
    "conditions", "cancer_stages", "treatment_courses", "treatment_cycles", "medication_events",
    "procedures", "provenance", "codings", "devices", "encounters", "clinical_records",
    "clinical_record_audit", "curated_runs",
)  # fmt: skip


def model_chat(system, user, info=None, **kw):
    """The mocked local model: the worked-example answer of the page it is shown."""
    assert system == lx.SYSTEM_PROMPT
    if info is not None:
        info["finish_reason"] = "stop"
    return json.dumps(ANSWERS[user.split("\n", 1)[1]], ensure_ascii=False)


async def _vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


async def _extract(db, pages, *, births=(DOB,), chat=model_chat):
    vocab = await _vocab(db)
    with local_only("test"):
        return lx.extract_document(
            list(enumerate(pages, 1)), vocab=vocab, report_date=DAY, birth_dates=set(births),
            chat=chat,
        )  # fmt: skip


async def _doc(db, doc_id, pid, pages, category="report"):
    meta = {"ocr_engine": "qwen-local", "curated": {"sidecar_id": f"s{doc_id}"}}
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, mime_type, "
        "size_bytes, patient_id, gdrive_id, structured_metadata, document_date) "
        "VALUES (?, ?, ?, ?, ?, 'application/pdf', 1, ?, ?, ?, ?)",
        (doc_id, f"gdrive:g{doc_id}", f"d{doc_id}.pdf", f"d{doc_id}.pdf", category, pid,
         f"g{doc_id}", json.dumps(meta), DAY),
    )  # fmt: skip
    for n, text in enumerate(pages, 1):
        await db.save_ocr_page(doc_id, n, text, "curated-sidecar")
    await db.db.commit()


@pytest.fixture
async def seeded(db):
    await db.db.execute(
        "INSERT OR IGNORE INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
        (BOB_UUID, "bob-test", "Bob Test"),
    )
    await _doc(db, DOC_A, ERIKA_UUID, PAGES_A)
    await _doc(db, DOC_B, ERIKA_UUID, PAGES_B, category="discharge")
    await _doc(db, DOC_BOB, BOB_UUID, PAGES_B, category="discharge")
    return db


async def _store(db, doc_id, pages, pid=ERIKA_UUID):
    out = await _extract(db, pages)
    return await clinical_store.store_document(
        db, patient_id=pid, document_id=doc_id, items=out["items"], document_date=DAY,
        birth_dates={DOB}, model="mock-model",
    )  # fmt: skip


async def _rows(db, table, pid=ERIKA_UUID, where="1=1"):
    async with db.db.execute(
        f"SELECT * FROM {table} WHERE patient_id = ? AND {where} ORDER BY id",  # noqa: S608
        (pid,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _active(db, table, pid=ERIKA_UUID):
    return await _rows(db, table, pid, "deleted_at IS NULL")


async def _snapshot(db):
    out = {}
    for table in HARD_TABLES:
        async with db.db.execute(f"SELECT * FROM {table} ORDER BY 1") as cur:  # noqa: S608
            out[table] = [tuple(dict(r).values()) for r in await cur.fetchall()]
    return out


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


@pytest.fixture
def local_model(monkeypatch):
    from oncofiles import config

    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(config, "LLM_CLINICAL_EXTRACT_DISABLED", False)
    monkeypatch.setattr(llm, "chat", model_chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_readme_births", {ERIKA_UUID: {DOB}, BOB_UUID: {DOB}})
    copies: list[str] = []

    def fake_copy(db, job):  # the in-memory test DB has no file; see test_pre_store_*
        copies.append(job)
        return f"oncofiles-copy-{len(copies)}-{job}.db"

    monkeypatch.setattr(pre_store, "make_copy", fake_copy)
    return copies


# ── prompt, schema, normalisation ───────────────────────────────────────


def test_prompt_is_versioned_with_two_or_more_worked_examples_per_kind():
    assert lx.EXTRACTOR_VERSION.startswith(lx.EXTRACTOR_FAMILY)
    for kind in lx.KINDS:
        n = sum(1 for _, answer in lx.EXAMPLES if answer[kind])
        assert 2 <= n <= 5, kind
    assert any(not any(a.values()) for _, a in lx.EXAMPLES)  # an empty page too
    for page, answer in lx.EXAMPLES:
        assert page in lx.SYSTEM_PROMPT
        assert json.dumps(answer, ensure_ascii=False) in lx.SYSTEM_PROMPT
    schema = lx.RESPONSE_FORMAT["json_schema"]
    assert schema["strict"] is True and set(schema["schema"]["required"]) == set(lx.KINDS)


async def test_every_worked_example_verifies_as_machine(db):
    """The examples teach the model exactly what the verifier accepts."""
    vocab = await _vocab(db)
    for page, answer in lx.EXAMPLES:
        for kind, raws in answer.items():
            for raw in raws:
                cand, why = lx.normalize_item(kind, raw)
                assert why is None
                item = vc.verify_item(
                    {**cand, "page_number": 1}, page_text=page, vocab=vocab,
                    report_date=DAY, birth_dates={DOB},
                )  # fmt: skip
                assert item["verification"] == "machine", (kind, item["reasons"])


def test_normalisation_drops_items_without_evidence_or_required_values():
    assert lx.normalize_item("medications", {"drug_as_printed": "X"})[1] == "no_evidence"
    planned = {"description_as_printed": "Kontrola", "evidence": "Kontrola"}
    assert lx.normalize_item("planned", planned)[1] == "missing_required"
    no_result = {"marker_as_printed": "KRAS", "evidence": "KRAS"}
    assert lx.normalize_item("biomarkers", no_result)[1] == "missing_required"
    proc, _ = lx.normalize_item("procedures", {"type": "laser", "description_as_printed": "Y",
                                               "evidence": "Y"})  # fmt: skip
    assert proc["type"] == "other"  # an unknown enum value never invents a kind
    cyc, _ = lx.normalize_item("cycles", {"cycle_as_printed": "C2", "status": "done",
                                          "evidence": "C2"})  # fmt: skip
    assert cyc["status"] is None


def test_parse_answer_is_robust():
    body = json.dumps({"diagnoses": [], "lesions": [{"x": 1}]})
    assert lx.parse_answer(f"<think>hm</think>```json\n{body}\n```")["lesions"] == [{"x": 1}]
    assert lx.parse_answer(f"Here: {body} done")["cycles"] == []
    with pytest.raises(llm.LocalLLMError):
        lx.parse_answer("no json at all")


def test_the_extractor_refuses_to_run_outside_local_only():
    with pytest.raises(CloudForbiddenError):
        lx.extract_page("Dg.: X", page_number=1, chat=model_chat)


# ── the verifier rejects invented items ─────────────────────────────────

NEG_PAGE = "\n".join([
    "Kontrola 4.5.2028",
    "Testoplatina 70 mg i.v. podaná 4.5.2028",
    "Ložisko L1 18 mm",
    "KRAS bez mutácie",
    "CEA zvýšené",
    "Dg.: Testóm pečene, dg. 2.1.2027",
])  # fmt: skip
NEG_LINES = NEG_PAGE.splitlines()


def _med(**kw):
    base = {"kind": "medications", "drug_as_printed": "Testoplatina", "dose_as_printed": "70",
            "unit_as_printed": "mg", "route_as_printed": "i.v.", "start_date_as_printed": None,
            "end_date_as_printed": None, "status": "administered", "cycle_as_printed": None,
            "evidence": NEG_LINES[1], "page_number": 1}  # fmt: skip
    return {**base, **kw}


def _lesion(**kw):
    base = {"kind": "lesions", "lesion_as_printed": "L1", "size_as_printed": "18",
            "unit_as_printed": "mm", "date_as_printed": None, "description_as_printed": None,
            "evidence": NEG_LINES[2], "page_number": 1}  # fmt: skip
    return {**base, **kw}


def _marker(**kw):
    base = {"kind": "biomarkers", "marker_as_printed": "KRAS", "variant_as_printed": None,
            "result_as_printed": "bez mutácie", "method_as_printed": None,
            "date_as_printed": None, "evidence": NEG_LINES[3], "page_number": 1}  # fmt: skip
    return {**base, **kw}


@pytest.mark.parametrize(
    ("cand", "reason"),
    [
        (_med(dose_as_printed="75"), "dose_not_in_evidence"),
        (_med(dose_as_printed="4"), "dose_not_in_evidence"),  # only inside the date
        (_med(drug_as_printed="Inventoplatina"), "drug_not_in_evidence"),
        (_med(evidence="Testoplatina 75 mg i.v."), "evidence_not_found"),
        (_med(route_as_printed="p.o."), "route_not_in_evidence"),
        (_med(start_date_as_printed="5.5.2028"), "date_not_printed"),
        (_med(start_date_as_printed="4/5/2028"), "date_unparsed"),
        (_lesion(size_as_printed="1"), "size_not_in_evidence"),  # the digit of the label
        (_lesion(size_as_printed="19"), "size_not_in_evidence"),
        (_lesion(date_as_printed="4.5.2028"), "date_not_in_evidence"),  # on the page only
        (_lesion(unit_as_printed="in"), "unit_unconvertible"),
        (_lesion(lesion_as_printed="L2"), "lesion_not_in_evidence"),
        (_marker(marker_as_printed="BRCA9"), "marker_not_in_evidence"),
        (_marker(marker_as_printed="BRCA9"), "label_unmapped"),
        (_marker(variant_as_printed="c.12A>T"), "variant_not_in_evidence"),
        (
            _marker(marker_as_printed="CEA", result_as_printed="zvýšené", evidence=NEG_LINES[4]),
            "label_not_biomarker",
        ),
        (_marker(evidence="KRAS [?] bez mutácie"), "evidence_not_found"),
    ],
)
async def test_invented_items_stay_unverified(db, cand, reason):
    item = vc.verify_item(cand, page_text=NEG_PAGE, vocab=await _vocab(db), report_date=DAY,
                          birth_dates={DOB})  # fmt: skip
    assert item["verification"] == "unverified"
    assert reason in item["reasons"], item["reasons"]


async def test_correct_items_pass_and_a_birth_date_never_does(db):
    vocab = await _vocab(db)

    def check(c):
        return vc.verify_item(c, page_text=NEG_PAGE, vocab=vocab, report_date=DAY,
                              birth_dates={DOB})  # fmt: skip

    assert check(_med())["verification"] == "machine"
    assert check(_med())["derived"]["basis"] == "flat"
    lesion = check(_lesion(unit_as_printed="mm"))
    assert lesion["verification"] == "machine" and lesion["derived"]["size_mm"] == 18
    assert lesion["derived"]["date_source"] == "document"
    assert check(_marker())["derived"]["code"] == "KRAS"
    diag = {"kind": "diagnoses", "text_as_printed": "Testóm pečene", "code_as_printed": None,
            "date_as_printed": "2.1.2027", "role": "primary", "site_as_printed": None,
            "stage_as_printed": None, "evidence": NEG_LINES[5], "page_number": 1}  # fmt: skip
    assert check(diag)["reasons"] == ["birth_date"]
    assert check({**diag, "date_as_printed": None})["verification"] == "machine"


# #773: printed lesion size forms (invented values). (evidence line, size, unit) →
# dimensions in mm, all machine.
SIZE_PAGE = "\n".join([
    "MR 6.6.2028: ložisko L4 23 x 18 mm.",
    "MR 6.6.2028: ložisko L4 2,3 x 1,8 cm.",
    "MR 6.6.2028: ložisko L4 2,3×1,8cm.",
    "MR 6.6.2028: ložisko L4 23 mm x 18 mm.",
    "MR 6.6.2028: ložisko L4 1,5 cm.",
    "MR 6.6.2028: ložisko L4 37 x 29.",
    "MR 6.6.2028: ložisko L4 23 x 18 (pozri tabuľku v mm).",
    "MR 6.6.2028: ložisko L4 2 cm x 15 mm.",
    "MR 6.6.2028: ložisko L4 objem 47 cm3.",
    "MR 6.6.2028: uzol L4 23 x 18, pečeň 15 cm.",
    "MR 6.6.2028: uzol L4 2,4 x 1,9 cm.",
    "MR 6.6.2028: uzol L4 2,4 cm x 1,9 cm.",
])  # fmt: skip
SIZE_LINES = SIZE_PAGE.splitlines()


def _size(line: int, size: str, unit: str | None) -> dict:
    return _lesion(lesion_as_printed="L4", size_as_printed=size, unit_as_printed=unit,
                   evidence=SIZE_LINES[line])  # fmt: skip


@pytest.mark.parametrize(
    ("cand", "dims_mm"),
    [
        (_size(0, "23 x 18", "mm"), [23, 18]),
        (_size(0, "23 x 18 mm", None), [23, 18]),  # unit only inside the size
        (_size(1, "2,3 x 1,8", "cm"), [23, 18]),  # decimal comma, cm → mm exactly ×10
        (_size(1, "2,3 x 1,8 cm", None), [23, 18]),
        (_size(1, "2.3 x 1.8", "cm"), [23, 18]),
        (_size(2, "2,3×1,8", "cm"), [23, 18]),  # "×", no spaces
        (_size(2, "2,3×1,8cm", None), [23, 18]),
        (_size(3, "23 mm x 18 mm", None), [23, 18]),  # the unit once per dimension
        (_size(3, "23 x 18", "mm"), [23, 18]),
        (_size(4, "1,5", "cm"), [15]),
        (_size(4, "1,5 cm", "cm."), [15]),
        (_size(10, "2,4 x 1,9", "cm"), [24, 19]),  # unit field filled, printed after the size
        (_size(11, "2,4 x 1,9", "cm"), [24, 19]),  # the unit after each number
        (_size(11, "2,4 cm x 1,9 cm", "cm"), [24, 19]),
    ],
)
async def test_printed_lesion_size_forms_convert_to_mm(db, cand, dims_mm):
    item = vc.verify_item(cand, page_text=SIZE_PAGE, vocab=await _vocab(db), report_date=DAY,
                          birth_dates={DOB})  # fmt: skip
    assert item["verification"] == "machine", item["reasons"]
    assert item["derived"]["dims_mm"] == dims_mm
    assert item["derived"]["size_mm"] == max(dims_mm)


@pytest.mark.parametrize(
    ("cand", "reason"),
    [
        (_size(5, "37 x 29", None), "unit_missing"),  # no unit printed: never guessed
        (_size(9, "23 x 18", "cm"), "unit_not_in_evidence"),  # "cm" belongs to another size
        (_size(9, "23 x 18 cm", None), "unit_not_in_evidence"),
        (_size(0, "23 x 18", None), "unit_missing"),  # printed, but not returned: not added
        (_size(6, "23 x 18 mm", None), "unit_not_in_evidence"),  # unit not after the numbers
        (_size(0, "2,3 x 1,8 cm", "mm"), "unit_unconvertible"),  # conflicting units
        (_size(7, "2 cm x 15 mm", None), "unit_unconvertible"),  # mixed units
        (_size(8, "47", "cm3"), "unit_unconvertible"),  # a volume is not a length
    ],
)
async def test_lesion_size_without_a_clear_printed_unit_stays_unverified(db, cand, reason):
    item = vc.verify_item(cand, page_text=SIZE_PAGE, vocab=await _vocab(db), report_date=DAY,
                          birth_dates={DOB})  # fmt: skip
    assert item["verification"] == "unverified"
    assert reason in item["reasons"], item["reasons"]
    if reason != "unit_not_in_evidence":
        assert "size_mm" not in item["derived"]


def test_the_cm_worked_example_copies_the_label_as_printed():
    page, answer = next((p, a) for p, a in lx.EXAMPLES if "cm" in p)
    (lesion,) = answer["lesions"]
    assert lesion["unit_as_printed"] == "cm" and "2028" in lesion["date_as_printed"]
    assert lesion["lesion_as_printed"] in page  # copied, never "liver S6"
    assert "null if not printed" in lx.SYSTEM_PROMPT


def test_dates_keep_their_printed_precision():
    assert vc.parse_any_date("03/2027") == ("2027-03", "month")
    assert vc.parse_any_date("2028") == ("2028", "year")
    assert vc.parse_any_date("5.3.2027") == ("2027-03-05", "day")
    assert vc.parse_any_date("13/2027") == (None, None)
    # #773: two-digit years and Slovak / Czech month names (invented dates)
    assert vc.parse_any_date("5.3.27") == ("2027-03-05", "day")
    assert vc.parse_any_date("31.2.27") == (None, None)
    assert vc.parse_any_date("5. marca 2027") == ("2027-03-05", "day")
    assert vc.parse_any_date("5. března 2027") == ("2027-03-05", "day")
    assert vc.parse_any_date("21.októbra 2028") == ("2028-10-21", "day")
    assert vc.parse_any_date("marec 2027") == ("2027-03", "month")
    assert vc.parse_any_date("v júni 2028") == (None, None)  # never guesses which word
    assert vc.parse_any_date("júni 2028") == ("2028-06", "month")
    assert vc.parse_any_date("3/2027") == ("2027-03", "month")
    assert vc.parse_any_date("5. dávka 2027") == (None, None)  # not a month name
    stage = vc.parse_stage(EX1_DIAGNOSIS["stage_as_printed"])
    assert stage["prefix"] == "p" and stage["stage_group"] == "IV"
    assert stage["t"] == "T3" and stage["n"] == "N1"
    assert stage["m"] == "M1"
    assert vc.intent_of("neoadjuvantná") == "neoadjuvant" and vc.line_of_therapy("II. línia") == 2
    assert vc.dose_unit("mg/kg") == ("mg/kg", "per_kg") and vc.dose_unit("AUC") == (None, None)


# ── extraction → verification → storage, every kind ─────────────────────


async def test_round_trip_stores_every_kind_with_provenance(seeded):
    db = seeded
    res_a = await _store(db, DOC_A, PAGES_A)
    res_b = await _store(db, DOC_B, PAGES_B)
    stored = {k: v.get("added", 0) for k, v in {**res_a["stored"]}.items()}
    assert stored["conditions"] == len(["primary", "metastasis"])
    assert stored["biomarkers"] == len(["KRAS", "NRAS", "MSI"])
    assert stored["lesions"] == 4
    assert stored["planned_encounters"] == len(["CT", "visit"])
    assert res_b["stored"]["cancer_stages"]["added"] == 1

    conds = await _active(db, "conditions")
    primary = next(c for c in conds if c["printed_text"] == "Zhubný nádor sigmoidea")
    meta = next(c for c in conds if c["printed_text"].startswith("Metastázy"))
    assert primary["is_primary_cancer"] == 1 and primary["asserted_at"] == "2027-03"
    assert primary["recorded_at"] == DAY and primary["page_number"] == 1
    assert meta["primary_condition_id"] == primary["id"] and meta["asserted_at"] is None
    assert all(c["verification"] == "machine" for c in conds)
    assert all(c["extracted_by"] == f"{lx.EXTRACTOR_VERSION}/mock-model" for c in conds)
    stage = next(
        s for s in await _active(db, "cancer_stages") if s["condition_id"] == primary["id"]
    )
    assert stage["printed"] == EX1_DIAGNOSIS["stage_as_printed"]
    assert stage["stage_group"] == "IV" and stage["prefix"] == "p"
    assert stage["t"] == "T3"
    assert stage["n"] == "N1" and stage["m"] == "M1"
    codings = await _rows(db, "codings", where="deleted_at IS NULL")
    assert {(c["owner_type"], c["system"], c["code"]) for c in codings} >= {
        ("condition", clinical_store.MKCH10_SYSTEM, "C18.7"),
        ("condition", clinical_store.MKCH10_SYSTEM, "C20"),
        ("fact", clinical_store.HGVS_SYSTEM, "c.35G>A"),
        ("fact", clinical_store.HGVS_SYSTEM, "p.G12D"),
    }

    records = await _active(db, "clinical_records")
    kras = next(r for r in records if r["code"] == "KRAS")
    assert kras["record_type"] == "genetic_variant" and kras["value_text"] == "mutácia"
    assert (
        kras["occurred_at"] == DAY
        and json.loads(kras["metadata_json"])["date_source"] == "document"
    )
    assert {r["code"] for r in records if r["record_type"] == "biomarker"} == {"MSI", "HER2"}

    course = next(
        c for c in await _active(db, "treatment_courses") if c["regimen_printed"] == "mFOLFOX6"
    )
    assert (course["line_of_therapy"], course["intent"], course["start_at"]) == (
        1, "palliative", "2027-04-02",
    )  # fmt: skip
    cycles = await _active(db, "treatment_cycles")
    given = next(c for c in cycles if c["status"] == "administered")
    planned = next(c for c in cycles if c["status"] == "planned")
    postponed = next(c for c in cycles if c["status"] == "postponed")
    assert given["course_id"] == planned["course_id"] == course["id"]
    assert planned["planned_at"] == "2027-06-28" and given["actual_at"] is None
    assert postponed["planned_at"] == "2027-10-18" and postponed["status_reason"] == "neutropéniu"

    meds = {m["ingredient_printed"]: m for m in await _active(db, "medication_events")}
    oxa = meds["Oxaliplatina"]
    assert oxa["kind"] == "administration" and oxa["dose_value"] == 85
    assert (oxa["dose_unit_ucum"], oxa["normalization_basis"]) == ("mg/m2", "per_m2")
    assert oxa["cycle_id"] == given["id"] and oxa["course_id"] == course["id"]
    assert meds["Dexametazón"]["kind"] == "request" and meds["Dexametazón"]["course_id"] is None

    procs = {p["printed_text"]: p for p in await _active(db, "procedures")}
    rt = procs["Neoadjuvantná rádioterapia 50,4 Gy/28 fr."]
    assert (rt["kind"], rt["performed_at"], rt["performed_end"]) == (
        "radiotherapy", "2027-06-10", "2027-07-19",
    )  # fmt: skip
    lesions = [r for r in records if r["code"] == "LESION_SIZE"]
    june = next(r for r in lesions if r["occurred_at"] == "2027-06-10")
    assert june["value_canonical"] == 31 and june["unit_canonical"] == "mm"
    assert json.loads(june["metadata_json"])["imaging_procedure_id"] == procs["CT"]["id"]
    two_d = next(r for r in lesions if r["value_text"] == "23 x 18")
    assert two_d["value_canonical"] == 23 and two_d["occurred_at"] == DAY
    ct_july = procs["CT hrudníka, brucha a panvy"]["id"]
    assert json.loads(two_d["metadata_json"])["imaging_procedure_id"] == ct_july

    encounters = await _rows(db, "encounters", where="status = 'planned'")
    assert {(e["kind"], e["start_at"]) for e in encounters} == {
        ("imaging", "2027-07-20"), ("visit", "2027-08-02"),
    }  # fmt: skip
    assert all(e["source_document_id"] == DOC_A and e["evidence"] for e in encounters)

    # one provenance row per inserted row: extract / assembler / the extractor device
    prov = await _rows(db, "provenance")
    added = sum(c.get("added", 0) for r in (res_a, res_b) for c in r["stored"].values())
    assert len(prov) == added
    assert {(p["activity"], p["agent_role"]) for p in prov} == {("extract", "assembler")}
    device = await db.get_device(prov[0]["agent_device_id"])
    assert device.model == "mock-model" and device.version.startswith(lx.EXTRACTOR_VERSION)
    assert all(p["quote"] and p["page_number"] for p in prov)


async def test_unverified_items_are_stored_with_their_reasons(seeded):
    db = seeded
    out = await _extract(db, PAGES_A, births=(DAY,))  # the document date is the "DOB" here
    lesion = next(
        i for i in out["items"] if i["kind"] == "lesions" and not i["fields"]["date_as_printed"]
    )
    assert lesion["verification"] == "unverified" and "birth_date" in lesion["reasons"]
    with pytest.raises(verifier.NoBirthDateError):  # the store refuses without any DOB
        await clinical_store.store_document(db, patient_id=ERIKA_UUID, document_id=DOC_A,
                                            items=out["items"], document_date=DAY,
                                            birth_dates=set())  # fmt: skip
    assert await _active(db, "conditions") == []
    await clinical_store.store_document(db, patient_id=ERIKA_UUID, document_id=DOC_A,
                                        items=out["items"], document_date=DAY,
                                        birth_dates={DAY})  # fmt: skip
    rows = [r for r in await _active(db, "clinical_records") if r["verification"] == "unverified"]
    assert rows and all("birth_date" in json.loads(r["metadata_json"])["unverified_reasons"]
                        for r in rows)  # fmt: skip
    prov = await _rows(db, "provenance", where="target_type = 'fact'")
    assert any(p["reason"] and "birth_date" in p["reason"] for p in prov)


# ── re-extraction: only this extractor's machine rows of the document ────


async def test_re_extraction_never_touches_verified_or_foreign_rows(seeded):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    await db.replace_document_facts(
        patient_id=ERIKA_UUID, document_id=DOC_A, extracted_by="rule:test",
        facts=[{"code": "GLU", "label": "Glukóza", "value_num": 5.5, "occurred_at": DAY}],
    )  # fmt: skip
    conds = {c["printed_text"]: c for c in await _active(db, "conditions")}
    primary = conds["Zhubný nádor sigmoidea"]
    kras = next(r for r in await _active(db, "clinical_records") if r["code"] == "KRAS")
    course = (await _active(db, "treatment_courses"))[0]
    for table, row_id in (("conditions", primary["id"]), ("clinical_records", kras["id"]),
                          ("treatment_courses", course["id"])):  # fmt: skip
        await db.db.execute(
            f"UPDATE {table} SET verification = 'verified' WHERE id = ?",
            (row_id,),  # noqa: S608
        )
    await db.db.commit()

    res = await _store(db, DOC_A, PAGES_A)
    conditions = res["stored"]["conditions"]
    assert conditions["removed"] == conditions["added"] == 1  # the machine metastasis
    assert conditions["kept_protected"] == 1  # the verified primary
    assert res["stored"]["biomarkers"]["kept_protected"] == 1

    conds = await _active(db, "conditions")
    assert [c["id"] for c in conds if c["printed_text"] == primary["printed_text"]] == [
        primary["id"]
    ]  # no duplicate next to the verified one
    new_meta = next(c for c in conds if c["printed_text"].startswith("Metastázy"))
    assert new_meta["id"] != conds[0]["id"] and new_meta["primary_condition_id"] == primary["id"]
    records = await _active(db, "clinical_records")
    assert [r["id"] for r in records if r["code"] == "KRAS"] == [kras["id"]]
    assert any(r["code"] == "GLU" and r["extracted_by"] == "rule:test" for r in records)
    assert [c["id"] for c in await _active(db, "treatment_courses")] == [course["id"]]
    cycles = await _active(db, "treatment_cycles")
    assert cycles and all(c["course_id"] == course["id"] for c in cycles)
    deleted = await _rows(db, "conditions", where="deleted_at IS NOT NULL")
    assert deleted and all(d["verification"] == "machine" for d in deleted)

    # the lab rule re-extraction of the same document never deletes LLM rows
    await db.replace_document_facts(
        patient_id=ERIKA_UUID, document_id=DOC_A, facts=[], extracted_by="rule:none"
    )
    left = [r for r in await _active(db, "clinical_records") if r["extracted_by"].startswith("llm")]
    assert {r["code"] for r in left} >= {"KRAS", "LESION_SIZE"}
    assert all(not str(r["code"]).startswith("LESION") for r in
               await db.fact_verification_rows(patient_id=ERIKA_UUID))  # fmt: skip


# ── isolation ───────────────────────────────────────────────────────────


async def test_two_patients_never_see_each_other(seeded, local_model):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    await _store(db, DOC_BOB, PAGES_B, pid=BOB_UUID)
    with pytest.raises(ValueError):  # Erika's document under Bob's id
        await _store(db, DOC_A, PAGES_A, pid=BOB_UUID)
    erika = {r["source_document_id"] for r in await db.timeline_rows(patient_id=ERIKA_UUID)}
    bob = {r["source_document_id"] for r in await db.timeline_rows(patient_id=BOB_UUID)}
    assert DOC_BOB not in erika and DOC_A not in bob and DOC_BOB in bob
    assert {c["source_document_id"] for c in await db.condition_rows(patient_id=BOB_UUID)} == {
        DOC_BOB
    }
    bob_treat = await db.treatment_rows(patient_id=BOB_UUID)
    assert all(r["source_document_id"] == DOC_BOB for rows in bob_treat.values() for r in rows)
    # Erika's caller cannot run the extractor on Bob's document id
    out = json.loads(await tool.curated_clinical_extract_preview(_ctx(db), [DOC_BOB]))
    assert out == {"status": "nothing to do"}


# ── read side: timeline, overview, list_treatment_events ────────────────


async def test_timeline_overview_and_treatment_list_return_the_new_kinds(seeded):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    await _store(db, DOC_B, PAGES_B)
    ctx = _ctx(db)
    expect = {
        "condition": {"condition"}, "treatment": {"treatment_course", "treatment_cycle"},
        "medication": {"medication_event"}, "procedure": {"procedure"},
        "biomarker": {"fact"}, "genetic_variant": {"fact"}, "imaging": {"fact"},
        "planned": {"encounter", "treatment_cycle"},
    }  # fmt: skip
    for kind, entities in expect.items():
        rows = json.loads(await cf.timeline(ctx, kinds=[kind]))["rows"]
        assert rows and {r["entity"] for r in rows} == entities, kind
        assert all(r["source_document_id"] in (DOC_A, DOC_B) for r in rows), kind
        assert all(r["page_number"] for r in rows), kind
    cond = json.loads(await cf.timeline(ctx, kinds=["condition"]))["rows"]
    undated = next(r for r in cond if r["title"].startswith("Metastázy"))
    assert undated["date_source"] == "document" and undated["at"] == DAY
    imaging = json.loads(await cf.timeline(ctx, kinds=["imaging"]))["rows"]
    assert any(r["title"] == "Lesion size" and r["detail"] == "S8" for r in imaging)

    overview = json.loads(await cf.patient_overview(ctx))
    dm = overview["diagnoses_and_markers"]
    primary = next(d for d in dm if d["name"] == "Zhubný nádor sigmoidea")
    assert primary["kind"] == "condition" and primary["code"] == "C18.7"
    assert primary["value"] == EX1_DIAGNOSIS["stage_as_printed"]
    assert primary["page"] == 1
    assert {d["code"] for d in dm if d["kind"] != "condition"} >= {"KRAS", "NRAS", "MSI", "HER2"}
    assert all(d["source_document_id"] and d["verification"] for d in dm)
    assert isinstance(overview["upcoming"], list)
    planned = await db.planned_rows(patient_id=ERIKA_UUID, from_day="2027-01-01")
    assert {r["at"] for r in planned} >= {"2027-06-28", "2027-07-20"}

    listed = json.loads(await tt.list_treatment_events(ctx))
    assert "deprecated" in listed and listed["events"] == []
    assert {c["regimen_printed"] for c in listed["courses"]} == {"mFOLFOX6", "FOLFIRI"}
    assert {c["status"] for c in listed["cycles"]} == {"administered", "planned", "postponed"}
    assert all(m["page_number"] and m["source_document_id"] for m in listed["medication_events"])
    surgery_only = json.loads(await tt.list_treatment_events(ctx, event_type="surgery"))
    assert surgery_only["courses"] == []


# ── the MCP tools: preview writes nothing, writes need a DOB ────────────


async def test_preview_writes_nothing_and_reports_counts_only(seeded, local_model):
    db = seeded
    seen = []

    def chat(system, user, **kw):
        seen.append(cloud_guard.local_only_job())
        with pytest.raises(CloudForbiddenError):
            cloud_guard.check_cloud_allowed("anthropic")
        return model_chat(system, user, **kw)

    with patch.object(llm, "chat", chat):
        before = await _snapshot(db)
        started = json.loads(await tool.curated_clinical_extract_preview(_ctx(db), [DOC_A, DOC_B]))
        assert started["kind"] == "clinical_preview" and started["dry_run"] is True
        assert not tool.run_active()  # a preview never blocks a deploy
        await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    job = json.loads(await tool.curated_reocr_status(_ctx(db)))
    assert job["state"] == "done", job
    assert await _snapshot(db) == before
    totals = job["report"]["totals"]
    lesions_taught = sum(len(ANSWERS[p]["lesions"]) for p in PAGES_A + PAGES_B)
    assert totals["counts"]["lesions"] == {"machine": lesions_taught, "unverified": 0}
    assert totals["counts"]["diagnoses"]["machine"] == len(["C18.7", "meta", "C20"])
    assert all(d["result"] == "previewed" for d in job["report"]["documents"])
    assert set(seen) == {"curated clinical extraction"}
    dumped = json.dumps(job, ensure_ascii=False)
    printed = [
        "Oxaliplatina",
        "2400",
        "C18.7",
        "mFOLFOX6",
        "neutropéniu",
        EX1_DIAGNOSIS["text_as_printed"],
    ]
    assert not [value for value in printed if value in dumped]
    # dry_run=True on the write tool is the same preview
    tool._reocr_jobs.clear()
    same = json.loads(await tool.curated_clinical_extract(_ctx(db), [DOC_A]))
    assert same["kind"] == "clinical_preview"
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    assert await _snapshot(db) == before


async def test_write_stores_holds_the_lock_and_is_not_a_curated_run(seeded, local_model):
    db = seeded
    lock_held = []

    def chat(system, user, **kw):
        lock_held.append(tool.patient_lock(ERIKA_UUID).locked())
        return model_chat(system, user, **kw)

    store = clinical_store.store_document
    store_jobs = []

    async def spy_store(*args, **kw):  # the DB writes run local-only too
        store_jobs.append(cloud_guard.local_only_job())
        return await store(*args, **kw)

    with (
        patch.object(llm, "chat", chat),
        patch.object(clinical_store, "store_document", spy_store),
    ):
        started = json.loads(
            await tool.curated_clinical_extract(_ctx(db), [DOC_A, DOC_B], dry_run=False)
        )
        assert started["kind"] == "clinical_extract" and tool.run_active()
        await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    job = tool._reocr_jobs[ERIKA_UUID]
    assert job["state"] == "done", job
    assert lock_held and all(lock_held)
    assert store_jobs == ["curated clinical extraction"] * len([DOC_A, DOC_B])
    assert cloud_guard.local_only_job() is None  # the caller's context is untouched
    assert {d["result"] for d in job["report"]["documents"]} == {"stored"}
    assert job["report"]["totals"]["stored"]["conditions"]["added"] == len(["a", "b", "c"])
    assert len(await _active(db, "conditions")) == len(["a", "b", "c"])
    runs = await _rows(db, "curated_runs")
    assert [(r["trigger"], r["status"]) for r in runs] == [("clinical", "ok")]
    assert await db.list_curated_runs(patient_id=ERIKA_UUID) == []
    assert await db.get_last_curated_run(patient_id=ERIKA_UUID) is None


async def test_write_refuses_without_a_birth_date(seeded, local_model, monkeypatch):
    db = seeded
    monkeypatch.setattr(tool, "_readme_births", {})
    before = await _snapshot(db)
    with patch("oncofiles.server._create_patient_clients", AsyncMock(return_value=None)):
        out = json.loads(await tool.curated_clinical_extract(_ctx(db), [DOC_A], dry_run=False))
        assert "no birth date" in out["error"] and ERIKA_UUID not in tool._reocr_jobs
        # the job itself refuses too (a DOB removed between the call and the job)
        tool._reocr_jobs[ERIKA_UUID] = {"state": "running", "kind": "clinical_extract"}
        docs = await db.curated_reocr_candidates(patient_id=ERIKA_UUID)
        await tool._clinical_job(db, ERIKA_UUID, docs, write=True)
    job = tool._reocr_jobs[ERIKA_UUID]
    assert job["state"] == "failed" and job["error"] == "no_birth_date"
    after = await _snapshot(db)
    assert {t: v for t, v in after.items() if t != "curated_runs"} == {
        t: v for t, v in before.items() if t != "curated_runs"
    }
    # the preview runs without a DOB and says the guard is inactive
    tool._reocr_jobs.clear()
    with patch("oncofiles.server._create_patient_clients", AsyncMock(return_value=None)):
        await tool.curated_clinical_extract_preview(_ctx(db), [DOC_A])
        await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    doc = tool._reocr_jobs[ERIKA_UUID]["report"]["documents"][0]
    assert doc["birth_date_guard"] == "inactive_no_dob"


async def test_a_page_not_assessed_stores_nothing_for_that_document(seeded, local_model):
    db = seeded

    def chat(system, user, info=None, **kw):
        if user.startswith("Page 2"):
            info["finish_reason"] = "length"
            return ""
        return model_chat(system, user, info=info, **kw)

    with patch.object(llm, "chat", chat):
        await tool.curated_clinical_extract(_ctx(db), [DOC_A], dry_run=False)
        await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    doc = tool._reocr_jobs[ERIKA_UUID]["report"]["documents"][0]
    assert doc["result"] == "skipped" and doc["pages_not_assessed"] == [2]
    assert await _active(db, "conditions") == []


async def test_tool_guards(seeded, local_model, monkeypatch):
    from oncofiles import config

    db = seeded
    out = json.loads(await tool.curated_clinical_extract_preview(_ctx(db), list(range(11))))
    assert "at most 10" in out["error"]
    assert "required" in json.loads(await tool.curated_clinical_extract(_ctx(db), []))["error"]
    monkeypatch.setitem(tool._runs, ERIKA_UUID, {"state": "running"})
    out = json.loads(await tool.curated_clinical_extract(_ctx(db), [DOC_A], dry_run=False))
    assert out["error"].startswith("busy")
    tool._runs.pop(ERIKA_UUID)
    monkeypatch.setattr(config, "LLM_CLINICAL_EXTRACT_DISABLED", True)
    out = json.loads(await tool.curated_clinical_extract_preview(_ctx(db), [DOC_A]))
    assert "LLM_CLINICAL_EXTRACT_DISABLED" in out["error"]
    tool._reocr_jobs.update({"x": {"state": "running", "kind": "clinical_preview"}})
    assert not tool.run_active()
    tool._reocr_jobs.update({"y": {"state": "running", "kind": "clinical_extract"}})
    assert tool.run_active()


# ── birth dates: README contract + patient context ──────────────────────


async def test_birth_dates_come_from_the_readme_and_the_patient_context(db, monkeypatch):
    from oncofiles.curated import indexer
    from tests.test_curated_runner import FakeDrive

    drive = FakeDrive()
    assert indexer.readme_birth_dates(drive, drive.root) == {"1970-02-03"}  # fixture README
    assert indexer.scan(drive, drive.root).birth_dates == {"1970-02-03"}
    monkeypatch.setattr(tool, "_readme_births", {})
    with (
        patch("oncofiles.server._create_patient_clients",
              AsyncMock(return_value=(MagicMock(), None, None, drive.root))),
        patch("oncofiles.curated.drive_ops.DriveOps", lambda _own: drive),
    ):  # fmt: skip
        assert await tool._all_birth_dates(db, ERIKA_UUID) == {"1970-02-03"}
    await patient_context.save_to_db(db.db, {"date_of_birth": DOB}, patient_id=ERIKA_UUID)
    await db.db.commit()
    assert await tool._all_birth_dates(db, ERIKA_UUID) == {"1970-02-03", DOB}


# ── migration 086 ───────────────────────────────────────────────────────


async def test_migration_086_columns_code_and_view(db):
    async def cols(table):
        async with db.db.execute(f"PRAGMA table_info({table})") as cur:
            return {r[1] for r in await cur.fetchall()}

    for table in ("conditions", "cancer_stages", "treatment_courses", "treatment_cycles",
                  "medication_events", "procedures", "specimens", "reports"):  # fmt: skip
        assert "metadata_json" in await cols(table), table
    assert await cols("treatment_cycles") >= {"status", "status_reason"}
    assert await cols("encounters") >= {"status", "source_document_id", "page_number",
                                        "evidence", "extracted_by", "verification"}  # fmt: skip
    assert await cols("timeline_v") >= {"page_number", "date_source", "entity", "entity_id"}
    row = next(r for r in await db.fact_code_vocabulary() if r["code"] == "LESION_SIZE")
    assert row["category"] == "imaging" and row["unit_canonical"] == "mm"
    assert row["aliases"] == [] and row["unit_conversions"] == {"cm": 10}
    await db.db.execute("DELETE FROM schema_migrations WHERE version = '086_hard_kinds_extraction'")
    await db.db.commit()
    await db.migrate()  # ADD COLUMN falls back to best-effort, the view is recreated
    assert "metadata_json" in await cols("conditions")
    async with db.db.execute("SELECT COUNT(*) FROM timeline_v") as cur:
        assert (await cur.fetchone())[0] == 0


# ── reasoning channel (2026-11-11 prod preview: content empty, answer in reasoning) ──


def _reasoning_chat(body_for):
    def chat(system, user, info=None, **kw):
        if info is not None:
            info["finish_reason"] = "stop"
            info["raw_content"] = ""
            info["reasoning_content"] = body_for(user.split("\n", 1)[1])
        return ""

    return chat


async def test_answer_only_in_reasoning_channel_is_read(db):
    one_json = _reasoning_chat(lambda page: json.dumps(ANSWERS[page], ensure_ascii=False))
    via_reasoning = await _extract(db, PAGES_A, chat=one_json)
    via_content = await _extract(db, PAGES_A)
    assert via_reasoning["page_errors"] == 0
    assert via_reasoning["pages_used"] == len(PAGES_A)
    total = sum(c["machine"] + c["unverified"] for c in via_content["counts"].values())
    assert total > 0
    # same items found, but nothing from the reasoning channel is machine-trusted
    assert {k: c["machine"] + c["unverified"] for k, c in via_reasoning["counts"].items()} == {
        k: c["machine"] + c["unverified"] for k, c in via_content["counts"].items()
    }
    assert all(c["machine"] == 0 for c in via_reasoning["counts"].values())
    assert via_reasoning["unverified_reasons"].get("llm_reasoning_channel") == total
    assert all(it["verification"] == "unverified" for it in via_reasoning["items"])
    assert via_reasoning["answer_sources"] == {"reasoning": len(PAGES_A)}
    assert via_content["answer_sources"] == {"content": len(PAGES_A)}


async def test_ambiguous_reasoning_channel_leaves_page_not_assessed(db):
    def draft_plus_final(page):
        answer = json.dumps(ANSWERS[page], ensure_ascii=False)
        return f"draft {answer} final {answer}"

    out = await _extract(db, PAGES_A[:1], chat=_reasoning_chat(draft_plus_final))
    assert out["pages_used"] == 0 and out["page_errors"] == 1
    assert out["error_kinds"] == {"reasoning_ambiguous": 1}
    assert sum(c["machine"] + c["unverified"] for c in out["counts"].values()) == 0


async def test_unparseable_answer_reports_a_coded_error_kind(db):
    out = await _extract(db, PAGES_A[:1], chat=lambda s, u, info=None, **kw: "no json here")
    assert out["page_errors"] == 1 and out["error_kinds"] == {"no_json": 1}


def test_structured_output_is_off_by_default_and_switchable(monkeypatch):
    from oncofiles import config

    seen = []

    def chat(system, user, info=None, **kw):
        seen.append("response_format" in kw)
        return json.dumps(ANSWERS[user.split("\n", 1)[1]], ensure_ascii=False)

    with local_only("test"):
        monkeypatch.setattr(config, "LLM_CLINICAL_STRUCTURED_OUTPUT", False)
        lx.extract_page(PAGES_A[0], page_number=1, chat=chat)
        monkeypatch.setattr(config, "LLM_CLINICAL_STRUCTURED_OUTPUT", True)
        lx.extract_page(PAGES_A[0], page_number=1, chat=chat)
    assert seen == [False, True]


# ── #767: per-job A/B overrides (dry run only) ──────────────────────────


async def test_overrides_are_refused_on_the_write_path(seeded, local_model):
    db = seeded
    before = await _snapshot(db)
    for kw in ({"thinking": "off"}, {"sampling": "qwen"}, {"timeout_s": 300},
               {"reasoning_marker": "on"}):  # fmt: skip
        with pytest.raises(ValueError, match="dry run"):
            await tool.curated_clinical_extract(_ctx(db), [DOC_A], dry_run=False, **kw)
    for kw in ({"thinking": "maybe"}, {"timeout_s": 10},
               {"timeout_s": 1000}, {"timeout_s": "300"}):  # fmt: skip
        with pytest.raises(ValueError):
            await tool.curated_clinical_extract_preview(_ctx(db), [DOC_A], **kw)
    # the prefill override was removed (#770): an unknown argument, on both tools
    with pytest.raises(TypeError, match="prefill"):
        await tool.curated_clinical_extract_preview(_ctx(db), [DOC_A], prefill="on")
    with pytest.raises(TypeError, match="prefill"):
        await tool.curated_clinical_extract(_ctx(db), [DOC_A], dry_run=False, prefill="off")
    assert ERIKA_UUID not in tool._reocr_jobs and local_model == []  # no pre-store copy
    assert await _snapshot(db) == before


async def test_preview_overrides_are_used_and_echoed(seeded, local_model, monkeypatch):
    from oncofiles import config

    db = seeded
    monkeypatch.setattr(config, "LLM_EXTRACT_THINKING", "on")
    monkeypatch.setattr(config, "LLM_EXTRACT_SAMPLING", "greedy")
    monkeypatch.setattr(config, "LLM_EXTRACT_ANSWER_BUDGET", 4000)
    monkeypatch.setattr(config, "LLM_EXTRACT_TIMEOUT_S", 300.0)
    seen = []
    keys = ("thinking", "temperature", "top_p", "top_k", "presence_penalty", "max_tokens",
            "timeout")  # fmt: skip

    def chat(system, user, **kw):
        seen.append({k: kw.get(k) for k in keys})
        return model_chat(system, user, **kw)

    with patch.object(llm, "chat", chat):
        started = json.loads(
            await tool.curated_clinical_extract_preview(
                _ctx(db), [DOC_A], thinking="off", sampling="qwen"
            )
        )
        assert started["overrides"] == {"thinking": "off", "sampling": "qwen", "timeout_s": None,
                                        "reasoning_marker": None}  # fmt: skip
        await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    report = tool._reocr_jobs[ERIKA_UUID]["report"]
    assert report["settings"]["overrides"] == {"thinking": "off", "sampling": "qwen",
                                               "timeout_s": None,
                                               "reasoning_marker": None}  # fmt: skip
    assert (report["settings"]["thinking"], report["settings"]["sampling"]) == ("off", "qwen")
    assert report["settings"]["max_tokens"] == 4000 and report["warnings"] == []
    expected = {"thinking": False, "temperature": 0.7, "top_p": 0.8, "top_k": 20,
                "presence_penalty": 1.5, "max_tokens": 4000, "timeout": 300.0}  # fmt: skip
    assert seen and all(s == expected for s in seen)
    doc = report["documents"][0]
    assert set(doc["page_diagnostics"]) == {"1", "2"} and doc["warnings"] == []
    assert all(p["sampling"] == "qwen" for p in doc["page_diagnostics"].values())
    # a later preview without overrides is back on the env settings
    tool._reocr_jobs.clear()
    seen.clear()
    with patch.object(llm, "chat", chat):
        await tool.curated_clinical_extract_preview(_ctx(db), [DOC_A])
        await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    assert {s["temperature"] for s in seen} == {0.0} and {s["thinking"] for s in seen} == {True}


# ── #770: timeout override (dry run only); no prefill is ever sent ─────


async def test_preview_timeout_is_used_and_echoed(seeded, local_model, monkeypatch):
    from oncofiles import config

    db = seeded
    monkeypatch.setattr(config, "LLM_EXTRACT_THINKING", "on")
    monkeypatch.setattr(config, "LLM_EXTRACT_TIMEOUT_S", 300.0)
    seen = []

    def chat(system, user, **kw):
        assert "prefill_assistant" not in kw
        seen.append({k: kw.get(k) for k in ("timeout", "thinking")})
        return model_chat(system, user, **kw)

    with patch.object(llm, "chat", chat):
        started = json.loads(
            await tool.curated_clinical_extract_preview(
                _ctx(db), [DOC_A], thinking="off", sampling="greedy", timeout_s=600
            )
        )
        assert started["overrides"] == {"thinking": "off", "sampling": "greedy",
                                        "timeout_s": 600,
                                        "reasoning_marker": None}  # fmt: skip
        await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    report = tool._reocr_jobs[ERIKA_UUID]["report"]
    assert report["extractor"] == lx.EXTRACTOR_VERSION
    assert report["settings"]["timeout_s"] == 600.0 and "prefill" not in report["settings"]
    assert seen and all(s == {"timeout": 600.0, "thinking": False} for s in seen)
    diags = report["documents"][0]["page_diagnostics"].values()
    assert diags and all("prefill" not in p for p in diags)


# ── #770: reasoning-off marker override (dry run only) ───────────────


async def test_reasoning_marker_override_refused_on_write_used_and_echoed(
    seeded, local_model, monkeypatch
):
    from oncofiles import config

    db = seeded
    before = await _snapshot(db)
    for value in ("on", "off"):
        with pytest.raises(ValueError, match="dry run"):
            await tool.curated_clinical_extract(
                _ctx(db), [DOC_A], dry_run=False, reasoning_marker=value
            )
    with pytest.raises(ValueError):
        await tool.curated_clinical_extract_preview(_ctx(db), [DOC_A], reasoning_marker="yes")
    assert ERIKA_UUID not in tool._reocr_jobs and local_model == []  # no pre-store copy
    assert await _snapshot(db) == before

    monkeypatch.setattr(config, "LLM_REASONING_MARKER", False)
    seen = []

    def chat(system, user, **kw):
        seen.append((kw.get("reasoning_marker"), kw.get("thinking")))
        return model_chat(system, user, **kw)

    with patch.object(llm, "chat", chat):
        started = json.loads(
            await tool.curated_clinical_extract(
                _ctx(db), [DOC_A], dry_run=True, thinking="off", reasoning_marker="on"
            )
        )
        assert started["overrides"]["reasoning_marker"] == "on"
        await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    report = tool._reocr_jobs[ERIKA_UUID]["report"]
    assert report["dry_run"] is True and report["extractor"] == lx.EXTRACTOR_VERSION
    assert report["settings"]["reasoning_marker"] == "on"
    assert report["settings"]["overrides"]["reasoning_marker"] == "on"
    assert seen and all(s == (True, False) for s in seen)
    diags = report["documents"][0]["page_diagnostics"].values()
    assert diags and all(p["reasoning_marker"] is True for p in diags)
    assert await _snapshot(db) == before


async def test_printed_date_forms_must_still_be_inside_the_evidence(db):
    """#773: new date forms parse, but the date-in-evidence rule is unchanged."""
    vocab = await _vocab(db)
    page = "Operácia 5. marca 2027: resekcia pečene.\nKontrola 12.4.28, resekcia bez komplikácií."
    lines = page.splitlines()

    def check(date_as_printed, evidence):
        cand = {"kind": "procedures", "type": "surgery", "description_as_printed": "resekcia",
                "date_as_printed": date_as_printed, "end_date_as_printed": None,
                "evidence": evidence, "page_number": 1}  # fmt: skip
        return vc.verify_item(cand, page_text=page, vocab=vocab, report_date=DAY,
                              birth_dates={DOB})  # fmt: skip

    named = check("5. marca 2027", lines[0])
    assert named["verification"] == "machine", named["reasons"]
    assert named["dates"]["date"]["iso"] == "2027-03-05"
    assert check("12.4.28", lines[1])["verification"] == "machine"
    assert "date_not_in_evidence" in check("5. marca 2027", lines[1])["reasons"]
    assert "date_not_in_evidence" in check("12.4.28", lines[0])["reasons"]
    assert "date_not_printed" in check("15. marca 2027", lines[0])["reasons"]
    assert "date_not_printed" in check("2.4.28", lines[1])["reasons"]


def test_named_dates_are_not_doses():
    assert not vc._numbers_in("5", "Kapecitabín podaný 5. marca 2027")
    assert vc._numbers_in("2000", "Kapecitabín 2000 mg")
