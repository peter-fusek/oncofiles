"""Classification scorecard (#926, step 1) and the pure metadata verifier.

The scorecard runs the PRODUCTION ``classify.propose`` + ``validate`` on
stored sidecar texts, so deployed before any classification change it
measures today's baseline. Synthetic data only: the patient is the fixture
README's invented one, documents and answers are invented (dates 2027+). The
local model is always mocked.
"""

from __future__ import annotations

import ast
import asyncio
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oncofiles import config
from oncofiles.curated import classify, llm
from oncofiles.curated import classify_scorecard as csc
from oncofiles.curated.contract import parse_contract
from oncofiles.tools import curated as tool
from tests.helpers import PATIENT_A_UUID

SRC = Path(__file__).parent.parent / "src" / "oncofiles"
README = (Path(__file__).parent / "fixtures" / "curated" / "README_oncology.md").read_text()
# the fixture contract plus invented tokens of the documents below
CONTRACT = parse_contract(README.replace(
    "**Known doctors:** `Kovacova`, `Hruskova`.",
    "**Known doctors:** `Kovacova`, `Hruskova`, `Lipova`.\n\n"
    "**HospitalName:** `OnkologickaAmbulancia`, `Lucnica`.",
))  # fmt: skip
TODAY = date(2028, 1, 10)  # after every invented document date
URL = "http://llm.test/v1"

# invented documents and the model answers the mock gives for them
TEXT_REPORT = (
    "Vzorová nemocnica Lúčnica\nOnkologická ambulancia\nAMBULANTNÁ SPRÁVA\n"
    "Vyšetrená dňa: 14.03.2027\nDg.: C50.4\nMUDr. Eva Lipová"
)
TEXT_LABS = (
    "Laboratórium Brezina s.r.o.\nVÝSLEDKY VYŠETRENIA\nDátum odberu: 02.06.2027\n"
    "Krvný obraz bez odchýlok.\nVyšetril: MUDr. Tomáš Javor"
)
TEXT_NO_DATE = (
    "Vzorová nemocnica Lúčnica\nKONTROLNÉ VYŠETRENIE\n"
    "Pacientka sledovaná od jari 2027.\nMUDr. Eva Lipová"
)
_BASE = {"subject": None, "language": "sk", "multiple_documents": False, "confidence": 0.9}
ANSWERS = {
    TEXT_REPORT: {**_BASE, "doc_date": "2027-03-14", "date_evidence": "Vyšetrená dňa: 14.03.2027",
                  "doc_type": "Report", "institution": "Lucnica",
                  "institution_printed": "Vzorová nemocnica Lúčnica",
                  "doctor": "MUDr. Eva Lipová", "hospital": "OnkologickaAmbulancia",
                  "hospital_unit": "Onkologická ambulancia", "diagnoses": ["C50.4"],
                  "summary": "Kontrola."},
    TEXT_LABS: {**_BASE, "doc_date": "2027-06-02", "date_evidence": "Dátum odberu: 02.06.2027",
                "doc_type": "Labs", "institution": "LaboratoriumBrezina",
                "institution_printed": "Laboratórium Brezina", "doctor": "MUDr. Tomáš Javor",
                "hospital": "Unknown", "hospital_unit": None, "diagnoses": [],
                "summary": "Krvný obraz."},
    TEXT_NO_DATE: {**_BASE, "doc_date": None, "date_evidence": None, "doc_type": "Report",
                   "institution": "Lucnica", "doctor": "MUDr. Eva Lipová",
                   "hospital": "Unknown", "hospital_unit": None, "diagnoses": [],
                   "summary": "Kontrola."},
}  # fmt: skip


@pytest.fixture
def local_llm(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", URL)
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", False)
    monkeypatch.setattr(config, "CURATED_CLASSIFY_PROMPT", "v1")  # the production default


# ── verify_metadata (pure; not wired into the sidecar in step 1) ─────────

TEXT = ("Vzorová nemocnica Lúčnica\nInterné oddelenie\nSpráva zo dňa 21.04.2027\n"
        "Dg.: K21.0, I10\nMUDr. Eva Lipová")  # fmt: skip
RAW = {"doctor": "MUDr. Eva Lipová", "hospital_unit": "Interné oddelenie",
       "diagnoses": ["K21.0", "I10"], "date_evidence": "Správa zo dňa 21.04.2027"}  # fmt: skip


def test_verified_metadata_passes_when_printed():
    fields, codes = classify.verify_metadata(RAW, TEXT, "2027-04-21")
    assert codes == [] and fields == {**RAW}


@pytest.mark.parametrize(
    "patch,code,key,expected",
    [
        ({"diagnoses": ["K21.0", "C18.7"]}, "unverified_diagnoses", "diagnoses", ["K21.0"]),
        ({"doctor": "MUDr. Peter Javor"}, "unverified_doctor", "doctor", None),
        ({"hospital_unit": "Chirurgická klinika"}, "unverified_hospital_unit",
         "hospital_unit", None),
        ({"date_evidence": "Prijatá 22.04.2027"}, "unverified_date_evidence",
         "date_evidence", None),
        # printed, but not the chosen date
        ({"date_evidence": "Správa zo dňa 21.04.2027"}, None, "date_evidence", None),
    ],
)  # fmt: skip
def test_unprinted_fields_are_dropped_with_a_code(patch, code, key, expected):
    iso = "2027-04-21" if code else "2027-04-20"
    fields, codes = classify.verify_metadata({**RAW, **patch}, TEXT, iso)
    assert fields[key] == expected
    assert codes == [code or "unverified_date_evidence"]


def test_icd_code_with_or_without_its_dot_counts_as_printed():
    fields, codes = classify.verify_metadata({"diagnoses": ["K210"]}, TEXT, None)
    assert fields["diagnoses"] == ["K210"] and codes == []
    fields, codes = classify.verify_metadata({"diagnoses": ["I1"]}, TEXT, None)
    assert fields["diagnoses"] == [] and codes == ["unverified_diagnoses"]


def test_date_evidence_without_a_chosen_date_is_dropped():
    fields, codes = classify.verify_metadata(RAW, TEXT, None)
    assert fields["date_evidence"] is None and codes == ["unverified_date_evidence"]


def test_empty_fields_are_no_failure():
    fields, codes = classify.verify_metadata({}, TEXT, None)
    assert codes == []
    assert fields == {"diagnoses": [], "doctor": None, "hospital_unit": None,
                      "date_evidence": None}  # fmt: skip


# ── the scorecard ────────────────────────────────────────────────────────

DOC_A, DOC_B, DOC_C = 9261, 9262, 9263
FILED = {
    DOC_A: "20270314-JanaNovakova-Lucnica-Report-Lipova-OnkologickaAmbulancia.pdf",
    DOC_B: "20270602-JanaNovakova-LaboratoriumBrezina-Labs-Javor-Unknown.pdf",
    DOC_C: "kontrola_scan.pdf",  # outside the standard: not comparable
}
TEXT_OF = {DOC_A: TEXT_REPORT, DOC_B: TEXT_LABS, DOC_C: TEXT_NO_DATE}
REPEAT = 2
FACT_TABLES = ("clinical_records", "documents", "document_pages", "provenance", "conditions",
               "activity_log")  # fmt: skip
SEEN_KWARGS: list[dict] = []


def fake_chat(system, user, info=None, **kw):
    SEEN_KWARGS.append({k: v for k, v in kw.items() if k != "info"})
    if info is not None:
        info.update(finish_reason="stop", completion_tokens=300, reasoning_tokens=200,
                    raw_content="SECRET", reasoning_content="SECRET")  # fmt: skip
    return json.dumps(ANSWERS[user.split("\n", 1)[1]], ensure_ascii=False)


@pytest.fixture
async def seeded(db):
    for doc_id, text in TEXT_OF.items():
        await db.db.execute(
            "INSERT INTO documents (id, file_id, filename, original_filename, category, "
            "mime_type, size_bytes, patient_id, gdrive_id, structured_metadata) "
            "VALUES (?, ?, ?, ?, 'report', 'application/pdf', 1, ?, ?, '{}')",
            (doc_id, f"gdrive:g{doc_id}", FILED[doc_id], FILED[doc_id], PATIENT_A_UUID,
             f"g{doc_id}"),
        )  # fmt: skip
        await db.save_ocr_page(doc_id, 1, text, "curated-sidecar")
    await db.db.commit()
    return db


@pytest.fixture
def scorecard_env(monkeypatch, local_llm):
    SEEN_KWARGS.clear()
    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_runs", {})
    monkeypatch.setattr(csc, "_today", lambda: TODAY)
    monkeypatch.setenv("CURATED_AUTO_VOCAB", "true")

    async def contract(_db, _pid):
        return CONTRACT

    monkeypatch.setattr(tool, "_patient_contract", contract)


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


async def _snapshot(db):
    out = {}
    for table in FACT_TABLES:
        async with db.db.execute(f"SELECT * FROM {table} ORDER BY 1") as cur:  # noqa: S608
            out[table] = [tuple(dict(r).values()) for r in await cur.fetchall()]
    return out


async def _run(db, **kw):
    started = json.loads(await tool.curated_classify_scorecard(
        _ctx(db), [DOC_A, DOC_B, DOC_C, 999_999], **kw))  # fmt: skip
    assert started["kind"] == "classify_scorecard", started
    await asyncio.wait_for(tool._reocr_jobs[PATIENT_A_UUID]["task"], 10)
    return json.loads(await tool.curated_reocr_status(_ctx(db)))


async def _rows(db):
    async with db.db.execute(
        "SELECT * FROM curated_runs WHERE trigger = 'scorecard' ORDER BY id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def test_scorecard_counts_only_and_writes_nothing_but_its_row(seeded, scorecard_env):
    db = seeded
    before = await _snapshot(db)
    status = await _run(db, label="before-926", repeat=REPEAT)
    assert status["state"] == "done", status
    assert await _snapshot(db) == before
    report = status["report"]
    assert report["kind"] == "classification" and report["dry_run"] is True
    assert report["versions"]["prompt_version"] == csc.prompt_version()
    for key, value in csc.request_settings().items():
        assert report["settings"][key] == value
    assert report["settings"]["repeat"] == REPEAT
    t = report["totals"]
    assert t["documents"]["missing"] == 1 and t["documents"]["processed"] == 3
    runs, comparable = 3 * REPEAT, 2 * REPEAT  # DOC_C's name is outside the standard
    assert t["review"]["runs"] == runs
    assert t["review"]["review"] == REPEAT  # the no-date document, every run
    assert t["review"]["reasons"] == {"no_date": REPEAT}
    doc_type = t["agreement"]["doc_type"]
    assert doc_type["agree"] == comparable and doc_type["not_comparable"] == REPEAT
    assert doc_type["differ"] == doc_type["missing"] == 0
    assert doc_type["pct_agree"] == 100.0
    assert t["agreement"]["date"]["agree"] == comparable
    assert t["agreement"]["institution"]["agree"] == comparable
    assert t["repeat"] == {"documents": 3, "stable": 3, "pct_stable": 100.0}
    assert t["stability"]["truncations"] == 0 and t["speed"]["seconds_p50"] is not None
    assert t["metadata_unverified"] == {}
    # codes and counts only: no filename, text or answer anywhere
    dumped = json.dumps(report, ensure_ascii=False)
    for needle in ("Lúčnica", "Lucnica", "Javor", "SECRET", "JanaNovakova", "2027-", "202703",
                   "kontrola_scan"):  # fmt: skip
        assert needle not in dumped, needle
    (row,) = await _rows(db)
    assert row["dry_run"] and row["status"] == "ok"
    stored = json.loads(row["report_json"])
    assert set(stored) <= {"kind", "scorecard", "label", "settings", "versions", "totals",
                           "dry_run"}  # fmt: skip
    assert stored["kind"] == "classification"
    # an extraction scorecard never takes a classification row as its baseline
    assert await db.get_last_scorecard(patient_id=PATIENT_A_UUID) is None
    last = await db.get_last_scorecard(patient_id=PATIENT_A_UUID, kind="classification")
    assert last["id"] == row["id"]


async def test_scorecard_sends_todays_production_request(seeded, scorecard_env):
    """The scorecard's model calls are exactly what a curated run's
    ``propose`` sends; with CURATED_CLASSIFY_PROMPT unset that is the v1
    (pre-#926) request: no extra kwargs. v2: test_926_classification."""
    await _run(seeded, label="baseline")
    assert SEEN_KWARGS and all(kw == {} for kw in SEEN_KWARGS)
    assert csc.request_settings() == {"prompt_variant": "v1", "sampling": "legacy",
                                      "thinking": "default"}  # fmt: skip


async def test_second_scorecard_compares_with_the_first(seeded, scorecard_env):
    db = seeded
    await _run(db, label="before-926")
    status = await _run(db, label="after-926", compare_to_label="before")
    report = status["report"]
    assert report["compared_to"]["label"] == "before-926"
    assert report["baseline_comparable"] is True
    assert report["definition_changed"] == {}
    assert report["delta"]["metrics"]["review.pct_review"]["change"] == 0
    assert report["delta"]["regressions"] == []


def test_compare_withholds_the_delta_across_scorecard_versions():
    versions = csc.versions(sampling="legacy")
    old = {"versions": {**versions, "scorecard_version": "classify-scorecard-v0"}, "totals": {}}
    out = csc.compare(old, versions, {})
    assert out["baseline_comparable"] is False and out["delta"] is None
    changed = {**versions, "prompt_version": "0" * 16}
    out = csc.compare({"versions": changed, "totals": {}}, versions, {})
    assert out["baseline_comparable"] is True and "prompt_version" in out["definition_changed"]


async def test_scorecard_refusals(seeded, scorecard_env):
    ctx = _ctx(seeded)
    out = json.loads(await tool.curated_classify_scorecard(ctx, []))
    assert "required" in out["error"]
    out = json.loads(await tool.curated_classify_scorecard(ctx, list(range(1, 27))))
    assert "too many" in out["error"]
    out = json.loads(await tool.curated_classify_scorecard(ctx, [DOC_A], repeat=4))
    assert "repeat" in out["error"]
    out = json.loads(await tool.curated_classify_scorecard(ctx, [DOC_A], label="bad label"))
    assert "label" in out["error"]
    tool._reocr_jobs[PATIENT_A_UUID] = {"state": "running", "kind": "reocr"}
    out = json.loads(await tool.curated_classify_scorecard(ctx, [DOC_A]))
    assert "busy" in out["error"]
    assert await _rows(seeded) == []


def test_truncation_is_counted_not_raised(monkeypatch, local_llm):
    def truncated(system, user, info=None, **kw):
        raise llm.LocalLLMError("local LLM answer truncated: context overflow (HTTP 400)")

    monkeypatch.setattr(llm, "chat", truncated)
    run = csc.run_once(CONTRACT, TEXT_REPORT, FILED[DOC_A])
    assert run["truncated"] is True and run["review"] is True
    assert run["reasons"] == ["classification_failed"]
    card = csc.card(DOC_A, "report", [run])
    assert csc.totals([card])["stability"]["truncations"] == 1


def test_reason_codes_cover_validate_reasons():
    assert csc.reason_code("date 2027-03-14 does not appear in the document text") == (
        "date_not_printed")  # fmt: skip
    assert csc.reason_code("model confidence 0.30 < 0.6") == "low_confidence"
    assert csc.reason_code("something new") == "other"


# ── locks ────────────────────────────────────────────────────────────────


def test_scorecard_is_an_operator_tool_that_writes_nothing():
    from oncofiles.tool_surface import OPERATOR_TOOLS, USER_TOOLS

    assert "curated_classify_scorecard" in OPERATOR_TOOLS
    assert "curated_classify_scorecard" not in USER_TOOLS
    assert "classify_scorecard" in tool._NO_WRITE_KINDS


def _fn(mod: str, name: str) -> ast.AST:
    tree = ast.parse((SRC / mod).read_text())
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
    )


def _calls(node, attr: str) -> bool:
    return any(
        isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Attribute) and n.func.attr == attr)
            or (isinstance(n.func, ast.Name) and n.func.id == attr)
        )
        for n in ast.walk(node)
    )


def test_scorecard_text_comes_only_through_load_for_llm():
    job = _fn("tools/curated.py", "_classify_scorecard_job")
    assert _calls(job, "load_for_llm") and not _calls(job, "get_ocr_pages")
    for forbidden in ("upload", "update_text", "move_rename", "create_text", "insert_document",
                      "replace_document_facts", "store_document"):  # fmt: skip
        assert not _calls(job, forbidden), forbidden
    run = _fn("curated/classify_scorecard.py", "run_once")
    assert _calls(run, "propose") and _calls(run, "validate")


async def test_scorecard_reports_progress_counts_and_logs_per_document(
    seeded, scorecard_env, caplog
):
    """#942 follow-up: the job shows docs/calls done, and one INFO line per
    document carries ids, counts and seconds only (no filename, no text)."""
    caplog.set_level("INFO", logger="oncofiles")
    status = await _run(seeded, label="progress", repeat=REPEAT)
    n_docs = len(TEXT_OF)
    want = {"docs_total": n_docs, "docs_done": n_docs, "docs_skipped": 0}
    want |= {"calls_total": n_docs * REPEAT, "calls_done": n_docs * REPEAT}
    assert status["progress"] == want
    lines = [
        r.getMessage() for r in caplog.records if "classification scorecard row=" in r.getMessage()
    ]
    assert len(lines) == n_docs
    assert lines[-1].split()[4] == f"{n_docs}/{n_docs}"
    for line in lines:
        for needle in ("JanaNovakova", "kontrola_scan", "Javor", "SECRET", ".pdf"):
            assert needle not in line, needle
