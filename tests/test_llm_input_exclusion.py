"""One chokepoint for document text on its way to the local model (#815 follow-up).

``curated.llm_input`` holds the raw-genotype rule. Every LLM path goes through
it: the lab shadow, the lab write, the clinical preview / write and the
extraction scorecard load page text only via ``load_for_llm`` (in
``tools.curated._doc_work``); the re-OCR and the verify-only re-read filter
their candidates with ``reocr_exclusion``; the inbox classification of a
curated run checks ``inbox_exclusion``. An excluded document gets no model
call, no rows, no Drive write, and the entry ``skipped`` /
``raw_genotype_excluded`` in its job report.

The AST locks at the end keep it that way: a new caller of the local model
that reads page text without going through the chokepoint fails here.

Synthetic data only: invented rsIDs, values and dates (2027+).
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import config, pre_store
from oncofiles.curated import llm, llm_input
from oncofiles.curated import llm_extract_clinical as lx
from oncofiles.curated.reocr import ReocrReport
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
DOB = "2027-01-02"  # invented
DAY = "2027-06-14"
REASON = "raw_genotype_excluded"

DOC_CSV = 8801  # raw genotype CSV; only its page text gives it away (rsIDs)
DOC_FLAG = DOC_CSV + 1  # a PDF carrying exclude_extract: true
DOC_PDF = DOC_CSV + 2  # a normal lab PDF
DOC_PLAIN_CSV = DOC_CSV + 3  # a CSV that is not genotype data
EXCLUDED = (DOC_CSV, DOC_FLAG)

# invented raw export lines (made-up rsIDs, no real data)
RAW_PAGE = "\n".join(["# rsid,chromosome,position,genotype", "rs970001,1,1000,AG",
                      "rs970002,2,2000,CC", "rs970003,3,3000,TT"])  # fmt: skip
PAGE = "\n".join(["Laboratórny nález", "Dátum odberu: 14.6.2027", "S-ALT 0,45 0,10-0,60 µkat/l"])
FLAG_PAGE = PAGE + "\nPoznámka: kópia na vyžiadanie"  # distinct, so a call would show
PLAIN_CSV_PAGE = "datum,poznamka\n2027-06-14,kontrola"
LAB_ANSWER = json.dumps({"facts": [{
    "label_as_printed": "S-ALT", "value": "0,45", "comparator": None,
    "unit_as_printed": "µkat/l", "ref_range_as_printed": "0,10-0,60", "flag_as_printed": None,
    "date_as_printed": "14.6.2027", "date_role": "sample",
    "evidence": "S-ALT 0,45 0,10-0,60 µkat/l",
}]}, ensure_ascii=False)  # fmt: skip


def _chat(calls: list):
    """The mocked local model; records every page it is sent."""

    def chat(system, user, info=None, **kw):
        page = user.split("\n", 1)[1] if isinstance(user, str) and "\n" in user else str(user)
        calls.append(page)
        clinical = next((p for p, s in lx.SYSTEM_PROMPTS.items() if s == system), None)
        answer = json.dumps({k: [] for k in lx.PROFILE_KINDS[clinical]}) if clinical else LAB_ANSWER
        if info is not None:
            info.update(finish_reason="stop", raw_content=answer)
        return answer

    return chat


async def _doc(db, doc_id, page, *, name, mime, category, extra=None):
    meta = {"ocr_engine": "claude-vision", "curated": {"sidecar_id": f"s{doc_id}"},
            **(extra or {})}  # fmt: skip
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, mime_type, "
        "size_bytes, patient_id, gdrive_id, structured_metadata, document_date) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (doc_id, f"gdrive:g{doc_id}", name, name, category, mime, ERIKA_UUID, f"g{doc_id}",
         json.dumps(meta), DAY),
    )  # fmt: skip
    await db.save_ocr_page(doc_id, 1, page, "curated-sidecar")
    await db.db.commit()


@pytest.fixture
async def seeded(db):
    await _doc(db, DOC_CSV, RAW_PAGE, name=f"d{DOC_CSV}.csv", mime="text/csv", category="other")
    await _doc(db, DOC_FLAG, FLAG_PAGE, name=f"d{DOC_FLAG}.pdf", mime="application/pdf",
               category="labs", extra={"exclude_extract": True})  # fmt: skip
    await _doc(db, DOC_PDF, PAGE, name=f"d{DOC_PDF}.pdf", mime="application/pdf", category="labs")
    await _doc(db, DOC_PLAIN_CSV, PLAIN_CSV_PAGE, name=f"d{DOC_PLAIN_CSV}.csv",
               mime="text/csv", category="other")  # fmt: skip
    return db


@pytest.fixture
def calls(monkeypatch) -> list:
    out: list = []
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(config, "LLM_LAB_WRITE_ENABLED", True)
    monkeypatch.setattr(config, "LLM_CLINICAL_EXTRACT_DISABLED", False)
    monkeypatch.setattr(llm, "chat", _chat(out))
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_readme_births", {ERIKA_UUID: {DOB}})
    monkeypatch.setattr(pre_store, "make_copy", lambda db, job: f"oncofiles-copy-{job}.db")
    return out


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


async def _rows(db) -> dict:
    """Every fact row of the excluded documents, and the totals per table."""
    out = {}
    for table in ("clinical_records", "conditions", "procedures", "medication_events",
                  "provenance", "encounters"):  # fmt: skip
        async with db.db.execute(f"SELECT COUNT(*) FROM {table}") as cur:  # noqa: S608
            out[table] = (await cur.fetchone())[0]
    return out


async def _job(db) -> dict:
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    job = tool._reocr_jobs[ERIKA_UUID]
    assert job["state"] == "done", job
    return job


def _assert_excluded(report_docs: list[dict]) -> dict:
    docs = {d["document_id"]: d for d in report_docs}
    for doc_id in EXCLUDED:
        assert docs[doc_id]["result"] == "skipped", docs[doc_id]
        assert docs[doc_id]["reason"] == REASON
    return docs


def _assert_never_sent(calls: list) -> None:
    assert calls, "the normal PDF must still reach the model"
    assert all(c == PAGE for c in calls), "an excluded document reached the model"


# ── the rule and the loader ─────────────────────────────────────────────


async def test_load_for_llm_checks_row_then_text(seeded):
    db = seeded
    rows = {r["id"]: r for r in await db.curated_reocr_candidates(patient_id=ERIKA_UUID)}
    assert await llm_input.load_for_llm(db, ERIKA_UUID, rows[DOC_CSV]) == llm_input.Excluded(REASON)
    assert await llm_input.load_for_llm(db, ERIKA_UUID, rows[DOC_FLAG]) == llm_input.Excluded(
        REASON
    )
    assert await llm_input.load_for_llm(db, ERIKA_UUID, rows[DOC_PDF]) == [(1, PAGE)]
    assert await llm_input.load_for_llm(db, ERIKA_UUID, rows[DOC_PLAIN_CSV]) == [
        (1, PLAIN_CSV_PAGE)
    ]


async def test_flagged_row_pages_are_never_read(seeded):
    db = seeded
    rows = {r["id"]: r for r in await db.curated_reocr_candidates(patient_id=ERIKA_UUID)}
    with patch.object(db, "get_ocr_pages", AsyncMock(side_effect=AssertionError("read"))):
        assert isinstance(
            await llm_input.load_for_llm(db, ERIKA_UUID, rows[DOC_FLAG]), llm_input.Excluded
        )


def test_clinical_module_reexports_the_one_rule():
    assert lx.extraction_exclusion is llm_input.extraction_exclusion
    assert lx.EXCLUDED_RAW_GENOTYPE == llm_input.EXCLUDED_RAW_GENOTYPE == REASON


# ── every LLM job: no model call, no rows, skipped with the reason ──────


async def test_clinical_preview(seeded, calls):
    db = seeded
    before = await _rows(db)
    await tool.curated_clinical_extract_preview(_ctx(db), [DOC_CSV, DOC_FLAG, DOC_PDF])
    docs = _assert_excluded((await _job(db))["report"]["documents"])
    assert docs[DOC_PDF]["result"] == "previewed"
    _assert_never_sent(calls)
    assert await _rows(db) == before


async def test_clinical_write(seeded, calls):
    db = seeded
    before = await _rows(db)
    await tool.curated_clinical_extract(_ctx(db), [DOC_CSV, DOC_FLAG], dry_run=False)
    _assert_excluded((await _job(db))["report"]["documents"])
    assert calls == []
    assert await _rows(db) == before


async def test_lab_shadow(seeded, calls):
    db = seeded
    before = await _rows(db)
    await tool.curated_llm_extract_preview(_ctx(db), [DOC_CSV, DOC_FLAG, DOC_PDF])
    docs = _assert_excluded((await _job(db))["report"]["documents"])
    assert docs[DOC_PDF]["result"] == "compared"
    _assert_never_sent(calls)
    assert await _rows(db) == before


async def test_lab_write(seeded, calls):
    db = seeded
    before = await _rows(db)
    await tool.curated_lab_extract(_ctx(db), [DOC_CSV, DOC_FLAG, DOC_PDF], dry_run=False)
    docs = _assert_excluded((await _job(db))["report"]["documents"])
    assert docs[DOC_PDF]["result"] == "stored"
    _assert_never_sent(calls)
    async with db.db.execute(
        "SELECT COUNT(*) FROM clinical_records WHERE source_document_id IN (?, ?)", EXCLUDED
    ) as cur:
        assert (await cur.fetchone())[0] == 0
    assert (await _rows(db))["clinical_records"] > before["clinical_records"]  # PDF stored


async def test_scorecard_clinical_and_lab_side(seeded, calls):
    db = seeded
    before = await _rows(db)
    out = json.loads(
        await tool.curated_extraction_scorecard(
            _ctx(db), document_ids=[DOC_CSV, DOC_FLAG, DOC_PDF],
            lab_document_ids=[DOC_CSV, DOC_FLAG, DOC_PDF], label="lock-test",
        )
    )  # fmt: skip
    assert "error" not in out, out
    cards = (await _job(db))["report"]["documents"]
    for extractor in ("clinical", "lab"):
        mine = {c["document_id"]: c for c in cards if c["extractor"] == extractor}
        for doc_id in EXCLUDED:
            assert (mine[doc_id]["result"], mine[doc_id]["reason"]) == ("skipped", REASON)
        assert mine[DOC_PDF]["result"] != "skipped"
    _assert_never_sent(calls)
    assert len(calls) == 2  # the normal PDF once per extractor
    assert await _rows(db) == before


# ── re-OCR and the verify-only re-read ─────────────────────────────────


async def test_reocr_never_selects_a_raw_export_or_a_text_source(seeded):
    db = seeded
    ids = [DOC_CSV, DOC_FLAG, DOC_PDF, DOC_PLAIN_CSV]
    excluded: list[dict] = []
    targets = await tool._reocr_targets(db, ERIKA_UUID, ids, excluded=excluded)
    assert [t["document_id"] for t in targets] == [DOC_PDF]
    assert {(e["document_id"], e["result"], e["reason"]) for e in excluded} == {
        (DOC_CSV, "skipped", REASON),
        (DOC_FLAG, "skipped", REASON),
    }  # the plain CSV is no candidate either (its own text), but not genotype data
    auto = await tool._reocr_targets(db, ERIKA_UUID, None, only_with_facts=False)
    assert DOC_CSV not in {t["document_id"] for t in auto}


@pytest.mark.parametrize("dry_run", [True, False])
async def test_reocr_job_skips_excluded_documents(seeded, calls, dry_run):
    db = seeded
    sent: list = []

    def fake_run(drive, folder_id, docs, vocab, **kw):
        sent.extend(d["document_id"] for d in docs)
        return ReocrReport(dry_run=kw["dry_run"])

    gdrive = MagicMock()
    with (
        patch("oncofiles.server._create_patient_clients",
              AsyncMock(return_value=(gdrive, None, None, "root"))),
        patch("oncofiles.curated.reocr.run_reocr", fake_run),
    ):  # fmt: skip
        started = await tool.start_reocr(
            db, ERIKA_UUID, dry_run=dry_run, document_ids=[DOC_CSV, DOC_FLAG, DOC_PDF],
            only_with_facts=False,
        )  # fmt: skip
        assert started.get("kind") == "reocr", started
        job = await _job(db)
    assert sent == [DOC_PDF]  # never downloaded, never a vision pass
    _assert_excluded(job["report"]["documents"])
    assert calls == []


async def test_reocr_with_only_raw_exports_has_nothing_to_do(seeded, calls):
    db = seeded
    out = await tool.start_reocr(db, ERIKA_UUID, dry_run=False, document_ids=[DOC_CSV, DOC_FLAG],
                                 only_with_facts=False)  # fmt: skip
    assert out["status"] == "nothing to do"
    _assert_excluded(out["skipped"])
    assert ERIKA_UUID not in tool._reocr_jobs and calls == []


async def test_reread_verify_skips_excluded_documents(seeded, calls):
    db = seeded
    out = await tool.start_reread(db, ERIKA_UUID, [DOC_CSV, DOC_FLAG], dry_run=True)
    assert out["status"] == "nothing to do"
    _assert_excluded(out["skipped"])
    assert calls == []


# ── inbox classification of a curated run ─────────────────────────────


def test_inbox_raw_export_is_never_classified():
    from oncofiles.curated.runner import RunOptions, run_curated
    from tests.test_curated_runner import INBOX, NOW, FakeDrive, fake_extract, fake_propose

    seen: list[str] = []

    def propose(contract, text):
        seen.append(text)
        return fake_propose(contract, text)

    d = FakeDrive()
    d.add("export.csv", d.folder[INBOX], RAW_PAGE.encode())
    d.add("scan_001.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026")
    r = run_curated(d, d.root, RunOptions(now=NOW, run_id="t"), extract_fn=fake_extract,
                    propose_fn=propose)  # fmt: skip
    assert seen == ["VYSLEDKY 05.08.2026"]
    raw = next(x for x in r.documents if x["source"] == "export.csv")
    assert raw["result"] == "review_needed"


# ── AST locks: the chokepoint cannot be bypassed ───────────────────────

# Every place that hands content to the local model (``curated.llm.chat``),
# with why it is safe. Keys: (module path under src/oncofiles, top-level function).
MODEL_CALLERS = {
    ("curated/llm_extract_call.py", "call_and_parse"):
        "the text extractors' one call path; reached only from the extract_document "
        "closures in _lab_shadow_work / _clinical_work, whose text comes from _doc_work "
        "→ llm_input.load_for_llm",
    ("curated/reocr.py", "_vision_passes"):
        "vision on the original file; reached only from start_reocr / start_reread, whose "
        "candidates pass llm_input.reocr_exclusion",
    ("curated/extract.py", "_vision_pages"):
        "inbox OCR of a scan or image (pdf / image types only; text files are decoded, "
        "never sent)",
    ("curated/orientation.py", "ask_rotation"):
        "page direction only (#609), on a page image rendered by extract._vision_pages or "
        "reocr._vision_passes, which hold the same guarantees; asks for no transcription",
    ("curated/classify.py", "propose"):
        "inbox classification; every propose_fn call in runner.py is guarded by "
        "llm_input.inbox_exclusion",
}  # fmt: skip


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), ast.parse(path.read_text())


def _top_level(tree: ast.Module):
    """(top-level function or method name, node) for every def in the module."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield f"{node.name}.{sub.name}", sub


def _attr_refs(node: ast.AST, attr: str) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == attr for n in ast.walk(node)) or any(
        isinstance(n, ast.Name) and n.id == attr for n in ast.walk(node)
    )


def _refs(attr: str, *, skip_module: str | None = None) -> set[tuple[str, str]]:
    """Every (module, top-level function) that references ``attr`` (as a name
    or an attribute); module-level references count as ``<module>``."""
    out = set()
    for mod, tree in _modules():
        if mod == skip_module:
            continue
        inside = set()
        for name, fn in _top_level(tree):
            if _attr_refs(fn, attr):
                out.add((mod, name))
                inside |= {id(n) for n in ast.walk(fn)}
        for n in ast.walk(tree):
            if id(n) in inside or isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if (isinstance(n, ast.Attribute) and n.attr == attr) or (
                isinstance(n, ast.Name) and n.id == attr
            ):
                out.add((mod, "<module>"))
    return out


def _fn(mod: str, name: str) -> ast.AST:
    tree = ast.parse((SRC / mod).read_text())
    return dict(_top_level(tree))[name]


def _calls(node: ast.AST, attr: str) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr == attr) or (
                isinstance(f, ast.Name) and f.id == attr
            ):
                return True
    return False


def test_local_model_is_called_only_from_known_places():
    """``llm.chat`` (and any import of ``chat`` from ``curated.llm``) appears
    only in the functions listed in MODEL_CALLERS. A new caller must route its
    document text through ``llm_input`` and be added there with its reason."""
    found = set()
    for mod, tree in _modules():
        for name, fn in _top_level(tree):
            for n in ast.walk(fn):
                if (isinstance(n, ast.Attribute) and n.attr == "chat"
                        and isinstance(n.value, ast.Name) and n.value.id == "llm"):  # fmt: skip
                    found.add((mod, name))
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "oncofiles.curated.llm":
                assert "chat" not in {a.name for a in n.names}, f"{mod}: import llm.chat"
    found = {f for f in found if f[0] != "curated/llm.py"}  # the client itself
    assert found == set(MODEL_CALLERS), (
        "a new local-model caller: load its text through curated.llm_input "
        f"and add it to MODEL_CALLERS: {sorted(found ^ set(MODEL_CALLERS))}"
    )


def test_text_extractors_get_text_only_from_load_for_llm():
    # the shared call path is used only by the two extractors
    for attr in ("call_page", "call_and_parse"):
        mods = {m for m, _ in _refs(attr)}
        assert mods <= {"curated/llm_extract_call.py", "curated/llm_extract.py",
                        "curated/llm_extract_clinical.py"}, (attr, mods)  # fmt: skip
    # the extractors' entry point is reached only from the two _doc_work builders
    callers = _refs("extract_document")
    callers = {c for c in callers if not c[0].startswith("curated/llm_extract")}
    callers -= {("curated/clinical_store.py", "<module>"), ("curated/lab_store.py", "<module>")}
    assert callers == {("tools/curated.py", "_lab_shadow_work"),
                       ("tools/curated.py", "_clinical_work")}, callers  # fmt: skip
    for builder in ("_lab_shadow_work", "_clinical_work"):
        assert _calls(_fn("tools/curated.py", builder), "_doc_work"), builder
    # _doc_work reads page text ONLY through the chokepoint
    doc_work = _fn("tools/curated.py", "_doc_work")
    assert _calls(doc_work, "load_for_llm")
    assert not _calls(doc_work, "get_ocr_pages")


def test_no_llm_module_reads_page_text_except_llm_input():
    """Modules that talk to the local model never read ``document_pages`` /
    ``get_ocr_pages`` themselves; only ``llm_input._pages`` does."""
    llm_mods = ("oncofiles.curated.llm", "oncofiles.curated.llm_extract",
                "oncofiles.curated.llm_extract_clinical", "oncofiles.curated.llm_extract_call",
                "oncofiles.curated.reocr", "oncofiles.curated.classify")  # fmt: skip
    offenders = []
    for mod, tree in _modules():
        imports = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                imports |= {n.module} | {f"{n.module}.{a.name}" for a in n.names}
            elif isinstance(n, ast.Import):
                imports |= {a.name for a in n.names}
        if not imports & set(llm_mods) or mod == "curated/llm_input.py":
            continue
        for name, fn in _top_level(tree):
            reads = _calls(fn, "get_ocr_pages") or any(
                isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "document_pages" in n.value
                for n in ast.walk(fn)
            )  # fmt: skip
            if reads:
                offenders.append((mod, name))
    assert offenders == [], f"page text read beside a model call, not via llm_input: {offenders}"
    assert _calls(_fn("curated/llm_input.py", "_pages"), "get_ocr_pages")


def test_reocr_vision_is_reached_only_through_filtered_candidates():
    # the re-read entry points are used only by the three job bodies (#883:
    # the dry compare reads through reocr_compare.compare_one)
    for attr in ("run_reocr", "reread_isolated"):
        callers = _refs(attr, skip_module="curated/reocr.py")
        assert {m for m, _ in callers} <= {"tools/curated.py", "curated/reocr_compare.py"}, (
            attr, callers)  # fmt: skip
    assert _refs("run_reocr", skip_module="curated/reocr.py") == {("tools/curated.py",
                                                                    "_reocr_job")}  # fmt: skip
    assert _refs("reread_isolated", skip_module="curated/reocr.py") == {
        ("tools/curated.py", "_reocr_job"), ("tools/curated.py", "_reread_one"),
        ("curated/reocr_compare.py", "compare_one")}  # fmt: skip
    assert _refs("compare_one") == {("tools/curated.py", "_reocr_compare_job")}
    assert _refs("_reocr_compare_job") == {("tools/curated.py", "start_reocr_compare")}
    assert _calls(_fn("tools/curated.py", "start_reocr_compare"), "_compare_targets")
    assert _calls(_fn("tools/curated.py", "_compare_targets"), "reocr_exclusion")
    # each job body is launched only by its filtered starter
    assert _refs("_reocr_job") == {("tools/curated.py", "start_reocr")}
    assert _refs("_reread_one") == {("tools/curated.py", "_reread_job")}
    assert _refs("_reread_job") == {("tools/curated.py", "start_reread")}
    # ... and the starters filter with the same rule
    assert _calls(_fn("tools/curated.py", "start_reocr"), "_reocr_targets")
    assert _calls(_fn("tools/curated.py", "_reocr_targets"), "reocr_exclusion")
    assert _calls(_fn("tools/curated.py", "start_reread"), "reocr_exclusion")


def test_every_inbox_classification_is_guarded():
    callers = {(m, f) for m, f in _refs("propose_fn") if m == "curated/runner.py"}
    calling = {f for _, f in callers if _calls(_fn("curated/runner.py", f), "propose_fn")}
    assert calling, "no propose_fn call found: update this lock"
    for f in calling:
        assert _calls(_fn("curated/runner.py", f), "inbox_exclusion"), f
