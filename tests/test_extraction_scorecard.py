"""Extraction scorecard (#773): dry run, counts only, deltas between scorecards.

Synthetic data only: the clinical pages are the invented worked examples of
the prompt (``llm_extract_clinical.EXAMPLES``), the lab page is the invented
page of ``tests/test_llm_extract.py``; every date is 2027+. The local model
is always mocked.
"""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import MagicMock

import pytest

from oncofiles import cloud_guard
from oncofiles.cloud_guard import CloudForbiddenError
from oncofiles.curated import llm, llm_extract
from oncofiles.curated import llm_extract_clinical as lx
from oncofiles.curated import scorecard as sc
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID
from tests.test_llm_extract import PAGE as LAB_PAGE
from tests.test_llm_extract import model_answer as lab_answer

DOB = "2027-01-02"  # invented
DAY = "2027-07-12"
DOC_A = 7731
DOC_B = DOC_A + 1
DOC_LAB = DOC_A + 2
PAGES_A = [page for page, _ in lx.EXAMPLES[:2]]
PAGES_B = [page for page, _ in lx.EXAMPLES[2:]]
ANSWERS = {page: answer for page, answer in lx.EXAMPLES}
N_PAGES = len(PAGES_A) + len(PAGES_B) + 1
TOKENS = 120  # invented completion tokens per mocked answer
# Strings that would be patient content if they ever reached a report.
PRINTED = ["Oxaliplatina", "2400", "C18.7", "mFOLFOX6", "neutropéniu", "0,45", "0.45",
           "S-ALT", "S-GLU", "5,61", "Laboratórny"]  # fmt: skip
FORBIDDEN_KEYS = {"evidence", "text", "text_as_printed", "label_as_printed", "value",
                  "value_canonical", "answer", "masked", "raw", "page_diagnostics", "facts",
                  "items_raw", "rule_only_codes", "llm_only_codes"}  # fmt: skip
_TIMING_KEYS = {"started_at", "finished_at"}
_CODE = re.compile(r"[A-Za-z0-9_:.\-]{1,64}")
FACT_TABLES = ("clinical_records", "clinical_record_audit", "conditions", "treatment_courses",
               "treatment_cycles", "medication_events", "procedures", "provenance",
               "encounters", "document_pages")  # fmt: skip


def chat(system, user, info=None, **kw):
    """The mocked local model: clinical worked-example answers, the lab page answer."""
    if info is not None:
        info.update(finish_reason="stop", prompt_tokens=900, completion_tokens=TOKENS)
        info["reasoning_tokens"] = 0
    if system == llm_extract.SYSTEM_PROMPT:
        return lab_answer()
    assert system == lx.SYSTEM_PROMPT
    return json.dumps(ANSWERS[user.split("\n", 1)[1]], ensure_ascii=False)


async def _doc(db, doc_id, pages, category):
    meta = {"ocr_engine": "qwen-local", "curated": {"sidecar_id": f"s{doc_id}"}}
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, mime_type, "
        "size_bytes, patient_id, gdrive_id, structured_metadata, document_date) "
        "VALUES (?, ?, ?, ?, ?, 'application/pdf', 1, ?, ?, ?, ?)",
        (doc_id, f"gdrive:g{doc_id}", f"d{doc_id}.pdf", f"d{doc_id}.pdf", category, ERIKA_UUID,
         f"g{doc_id}", json.dumps(meta), DAY),
    )  # fmt: skip
    for n, text in enumerate(pages, 1):
        await db.save_ocr_page(doc_id, n, text, "curated-sidecar")


@pytest.fixture
async def seeded(db):
    await _doc(db, DOC_A, PAGES_A, "report")
    await _doc(db, DOC_B, PAGES_B, "discharge")
    await _doc(db, DOC_LAB, [LAB_PAGE], "labs")
    await db.db.execute(
        "INSERT INTO clinical_records (patient_id, record_type, source_document_id, param, "
        "code, occurred_at, value_canonical, verification, metadata_json, page_number, "
        "source) VALUES (?, 'lab', ?, 'ALT', 'ALT', '2027-06-14', ?, 'machine', '{}', 1, 't')",
        (ERIKA_UUID, DOC_LAB, 0.45),
    )
    await db.db.commit()
    return db


@pytest.fixture
def local_model(monkeypatch):
    from oncofiles import config

    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(config, "LLM_CLINICAL_EXTRACT_DISABLED", False)
    monkeypatch.setattr(config, "LLM_EXTRACT_SHADOW_DISABLED", False)
    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_runs", {})
    monkeypatch.setattr(tool, "_readme_births", {ERIKA_UUID: {DOB}})


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


async def _run(db, **kw):
    kw.setdefault("lab_document_ids", [DOC_LAB])
    started = json.loads(await tool.curated_extraction_scorecard(_ctx(db), [DOC_A, DOC_B], **kw))
    assert started["kind"] == "extraction_scorecard", started
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    return json.loads(await tool.curated_reocr_status(_ctx(db)))


async def _snapshot(db):
    out = {}
    for table in FACT_TABLES:
        async with db.db.execute(f"SELECT * FROM {table} ORDER BY 1") as cur:  # noqa: S608
            out[table] = [tuple(dict(r).values()) for r in await cur.fetchall()]
    return out


async def _scorecard_rows(db):
    async with db.db.execute(
        "SELECT * FROM curated_runs WHERE trigger = 'scorecard' ORDER BY id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


def _walk(obj, path=""):
    """Yield (path, key, value) for every key and leaf."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k, v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def _assert_counts_only(report):
    """No text-bearing key anywhere; every string leaf is a short code."""
    for path, key, value in _walk(report):
        assert key not in FORBIDDEN_KEYS, f"{path}.{key}"
        if isinstance(value, str) and key not in _TIMING_KEYS:
            assert _CODE.fullmatch(value), f"{path}.{key}={value!r}"
    dumped = json.dumps(report, ensure_ascii=False)
    for token in PRINTED:
        assert token not in dumped, token


# ── the job: report shape, no values, overrides ─────────────────────────


async def test_scorecard_report_shape_counts_only_and_writes_no_facts(seeded, local_model):
    db = seeded
    seen = []

    def spy(system, user, **kw):
        seen.append((cloud_guard.local_only_job(), {k: kw.get(k) for k in (
            "thinking", "temperature", "top_p", "top_k", "presence_penalty", "timeout",
            "reasoning_marker")}))  # fmt: skip
        with pytest.raises(CloudForbiddenError):
            cloud_guard.check_cloud_allowed("anthropic")
        return chat(system, user, **kw)

    before = await _snapshot(db)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(llm, "chat", spy)
        status = await _run(db, label="v1-baseline", thinking="off", sampling="qwen",
                            timeout_s=300, reasoning_marker="on")  # fmt: skip
    assert status["state"] == "done", status
    assert await _snapshot(db) == before  # no fact, page or provenance row
    report = status["report"]
    assert report["dry_run"] is True and report["label"] == "v1-baseline"
    assert report["scorecard"] == sc.SCORECARD_VERSION and report["partial"] is False
    assert report["extractors"]["clinical"] == lx.EXTRACTOR_VERSION
    assert report["extractors"]["lab"] == llm_extract.EXTRACTOR_VERSION
    # overrides passed through to every model call, and echoed
    assert len(seen) == N_PAGES
    assert {job for job, _ in seen} <= {"curated clinical extraction",
                                        "curated LLM extract shadow"}  # fmt: skip
    expected = {"thinking": False, "temperature": 0.7, "top_p": 0.8, "top_k": 20,
                "presence_penalty": 1.5, "timeout": 300.0, "reasoning_marker": True}  # fmt: skip
    assert all(kw == expected for _, kw in seen)
    overrides = {"thinking": "off", "sampling": "qwen", "timeout_s": 300, "reasoning_marker": "on"}
    assert report["settings"]["overrides"] == overrides
    assert (report["settings"]["thinking"], report["settings"]["sampling"]) == ("off", "qwen")
    # per document
    docs = {d["document_id"]: d for d in report["documents"]}
    assert set(docs) == {DOC_A, DOC_B, DOC_LAB}
    a = docs[DOC_A]
    assert a["extractor"] == "clinical" and a["pages"] == len(PAGES_A)
    assert set(a["page"]) == {"1", "2"}
    assert a["page"]["1"]["finish_reason"] == "stop"
    first_page = a["page"]["1"]
    assert first_page["completion_tokens"] == TOKENS
    assert first_page["reasoning_tokens"] == 0
    assert set(a["items"]["lesions"]) == {"machine", "unverified", "dropped"}
    lab = docs[DOC_LAB]
    assert lab["extractor"] == "lab"
    assert lab["lab"]["agree"] == 1 and lab["lab"]["llm_only"] == 1
    assert lab["lab"]["rule_only"] == 0 and lab["lab"]["value_mismatch"] == 0
    assert lab["unverified_reasons"] == {"label_unmapped": 1}
    # totals
    t = report["totals"]
    assert t["stability"]["pages"] == N_PAGES and t["stability"]["pct_stop"] == 100.0
    assert t["stability"]["timeouts"] == 0 and t["stability"]["truncations"] == 0
    assert t["stability"]["pct_reasoning_over_max"] == 0.0
    assert t["speed"]["completion_tokens_p50"] == TOKENS
    assert t["speed"]["completion_tokens_p95"] == TOKENS
    assert t["speed"]["seconds_p50"] is not None
    assert t["trust"]["machine"] > 0 and 0 < t["trust"]["pct_machine"] <= 100
    assert t["trust"]["by_kind"]["lab"]["machine"] == lab["items"]["lab"]["machine"]
    assert t["lab"]["comparable"] == 1 and t["lab"]["pct_agree"] == 100.0
    n_docs = len(docs)
    assert t["documents"]["requested"] == n_docs and t["documents"]["processed"] == n_docs
    assert t["documents"]["missing"] == 0
    assert t["documents"]["results"] == {"compared": 1, "previewed": len([DOC_A, DOC_B])}
    assert report["delta"] is None and report["compared_to"] is None  # first scorecard
    _assert_counts_only(report)
    _assert_counts_only({k: v for k, v in status.items() if k != "report"})


async def test_scorecard_counts_timeouts_and_reasoning_tokens(seeded, local_model):
    db = seeded

    def flaky(system, user, info=None, **kw):
        if system == llm_extract.SYSTEM_PROMPT:
            raise llm.LocalLLMError("local LLM unreachable: Timeout")
        out = chat(system, user, info=info, **kw)
        if info is not None:
            info["reasoning_tokens"] = 400
        return out

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(llm, "chat", flaky)
        status = await _run(db, label="v1-flaky")
    st = status["report"]["totals"]["stability"]
    for key in ("timeouts", "page_errors", "pages_not_assessed"):
        assert st[key] == 1, key
    assert st["pages_reasoning_over_max"] == N_PAGES - 1
    stopped = N_PAGES - 1
    assert st["pct_stop"] == round(100 * stopped / N_PAGES, 1)
    lab = next(d for d in status["report"]["documents"] if d["extractor"] == "lab")
    assert lab["page"]["1"]["error_kind"] == "timeout"
    _assert_counts_only(status["report"])


# ── persistence: aggregated counts only, never a run or a preview ───────


async def test_scorecard_row_holds_aggregates_only_and_never_passes_the_preview_gate(
    seeded, local_model
):
    db = seeded
    status = await _run(db, label="v1-baseline")
    rows = await _scorecard_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok" and row["dry_run"] == 1 and row["finished_at"]
    assert row["id"] == status["run_row_id"]
    stored = json.loads(row["report_json"])
    assert set(stored) == {"scorecard", "label", "settings", "extractors", "totals", "dry_run"}
    assert stored["totals"] == status["report"]["totals"]
    _assert_counts_only(stored)
    # never a curated run or preview
    assert await db.get_last_curated_run(patient_id=ERIKA_UUID) is None
    assert await db.get_last_curated_run(patient_id=ERIKA_UUID, dry_run=True) is None
    assert await db.list_curated_runs(patient_id=ERIKA_UUID) == []
    assert "no preview" in (await tool.preview_gate(db, ERIKA_UUID))
    assert await db.get_last_reocr_job(patient_id=ERIKA_UUID) is None


async def test_run_active_and_block_semantics(seeded, local_model):
    db = seeded
    gate = asyncio.Event()

    async def held(*a, **k):
        await gate.wait()
        return set()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tool, "_all_birth_dates", held)
        started = json.loads(
            await tool.curated_extraction_scorecard(_ctx(db), [DOC_A], label="v1-baseline")
        )
        assert started["state"] == "running"
        await asyncio.sleep(0)
        # a dry run: never blocks a deploy (/health.curated_run_active) ...
        assert tool.run_active() is False
        # ... but it holds the patient's one job slot, like the previews
        assert tool.run_block_reason(ERIKA_UUID) == "extraction_scorecard_in_progress"
        rows = await _scorecard_rows(db)
        assert [r["status"] for r in rows] == ["running"]
        gate.set()
        await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    assert tool.run_active() is False and tool.run_block_reason(ERIKA_UUID) is None
    assert [r["status"] for r in await _scorecard_rows(db)] == ["ok"]


async def test_a_cut_scorecard_is_marked_interrupted_and_is_no_baseline(seeded, local_model):
    db = seeded
    rid = await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=True, trigger="scorecard")
    assert await db.mark_interrupted_curated_runs() == 1
    assert await db.get_last_scorecard(patient_id=ERIKA_UUID) is None
    status = await _run(db, label="v1-baseline")
    assert status["report"]["compared_to"] is None and status["run_row_id"] > rid


# ── refusals ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["reocr", "reread", "llm_shadow", "clinical_preview",
                                  "clinical_extract", "extraction_scorecard"])  # fmt: skip
async def test_refused_while_another_job_is_active(seeded, local_model, kind):
    db = seeded
    tool._reocr_jobs[ERIKA_UUID] = {"state": "running", "kind": kind}
    out = json.loads(await tool.curated_extraction_scorecard(_ctx(db), [DOC_A]))
    assert out["error"].startswith("busy")
    assert tool._reocr_jobs[ERIKA_UUID] == {"state": "running", "kind": kind}
    assert await _scorecard_rows(db) == []


async def test_refused_while_a_curated_run_is_active(seeded, local_model):
    db = seeded
    tool._runs[ERIKA_UUID] = {"state": "running"}
    out = json.loads(await tool.curated_extraction_scorecard(_ctx(db), [DOC_A]))
    assert out["error"].startswith("busy") and await _scorecard_rows(db) == []


async def test_argument_validation(seeded, local_model, monkeypatch):
    from oncofiles import config

    db = seeded
    ctx = _ctx(db)
    assert "required" in json.loads(await tool.curated_extraction_scorecard(ctx, []))["error"]
    many = json.loads(
        await tool.curated_extraction_scorecard(
            ctx, list(range(20)), lab_document_ids=[*range(100, 106)]
        )  # fmt: skip
    )
    assert "at most 25" in many["error"]
    for bad in ("", "has space", "-lead", "x" * 61, "pct%"):
        out = json.loads(await tool.curated_extraction_scorecard(ctx, [DOC_A], label=bad))
        assert "label" in out["error"]
    with pytest.raises(ValueError):
        await tool.curated_extraction_scorecard(ctx, [DOC_A], sampling="hot")
    with pytest.raises(ValueError):
        await tool.curated_extraction_scorecard(ctx, [DOC_A], timeout_s=5)
    nothing = json.loads(await tool.curated_extraction_scorecard(ctx, [999_999]))
    assert nothing == {"status": "nothing to do", "missing_document_ids": [999_999]}
    monkeypatch.setattr(config, "LLM_EXTRACT_SHADOW_DISABLED", True)
    out = json.loads(await tool.curated_extraction_scorecard(ctx, [], lab_document_ids=[DOC_LAB]))
    assert "LLM_EXTRACT_SHADOW_DISABLED" in out["error"]
    assert await _scorecard_rows(db) == [] and ERIKA_UUID not in tool._reocr_jobs


# ── deltas ──────────────────────────────────────────────────────────────


async def test_delta_against_the_previous_scorecard_and_by_label_prefix(seeded, local_model):
    db = seeded
    first = await _run(db, label="v1-baseline")
    second = await _run(db, label="v1-baseline", lab_document_ids=[])
    assert second["report"]["compared_to"]["row_id"] == first["run_row_id"]
    assert second["report"]["compared_to"]["label"] == "v1-baseline"
    metrics = second["report"]["delta"]["metrics"]
    assert metrics["stability.pages"] == {"previous": N_PAGES, "current": N_PAGES - 1,
                                          "change": -1}  # fmt: skip
    assert metrics["lab.pct_agree"]["previous"] == 100.0
    assert (
        metrics["lab.pct_agree"]["current"] is None and metrics["lab.pct_agree"]["change"] is None
    )
    _assert_counts_only(second["report"])

    third = await _run(db, label="v2-qwen", compare_to_label="v1-b")
    assert third["report"]["compared_to"]["row_id"] == second["run_row_id"]
    fourth = await _run(db, label="v2-qwen-b", compare_to_label="v2")
    assert fourth["report"]["compared_to"]["row_id"] == third["run_row_id"]
    assert fourth["report"]["delta"]["regressions"] == []
    none = await _run(db, label="v3", compare_to_label="v9")
    assert none["report"]["compared_to"] is None and none["report"]["delta"] is None


def test_delta_is_metric_by_metric_and_flags_regressions():
    prev = {"stability": {"pages": 10, "pct_stop": 100.0, "timeouts": 0},
            "trust": {"pct_machine": 60.0, "unverified_reasons": {"date_not_printed": 3}},
            "lab": {"pct_agree": 80.0}}  # fmt: skip
    cur = {"stability": {"pages": 10, "pct_stop": 90.0, "timeouts": 1},
           "trust": {"pct_machine": 65.0, "unverified_reasons": {"label_unmapped": 2}},
           "lab": {"pct_agree": None}}  # fmt: skip
    out = sc.delta(prev, cur)
    m = out["metrics"]
    assert m["stability.pct_stop"] == {"previous": 100.0, "current": 90.0, "change": -10.0}
    assert m["stability.pages"]["change"] == 0
    assert m["trust.pct_machine"]["change"] == 5.0
    gone = m["trust.unverified_reasons.date_not_printed"]
    assert gone["previous"] == 3 and gone["current"] == 0
    assert gone["change"] == -3
    assert m["trust.unverified_reasons.label_unmapped"]["change"] == 2
    assert m["lab.pct_agree"]["change"] is None
    assert out["regressions"] == ["stability.pct_stop", "stability.timeouts"]
    assert sc.delta(None, cur) is None


# ── pure helpers ────────────────────────────────────────────────────────


def test_percentile_is_nearest_rank():
    assert sc.percentile([], 0.5) is None
    assert sc.percentile([3.0], 0.95) == 3.0
    vals = [float(v) for v in range(1, 21)]
    assert sc.percentile(vals, 0.5) == 10.0 and sc.percentile(vals, 0.95) == 19.0


def test_cards_keep_numbers_and_codes_only():
    entry = {
        "document_id": 1, "category": "labs", "result": "compared", "pages": 1,
        "pages_not_assessed": [], "seconds": 1.5, "llm_machine": 2, "llm_unverified": 1,
        "agree": 1, "value_mismatch": 0,
        "rule_only": 0, "llm_only": 2,
        "dropped": {"no_evidence": 1, "free text with spaces": 3},
        "unverified_reasons": {"label_unmapped": 1},
        "page_diagnostics": {"1": {"finish_reason": "stop", "seconds": 1.5,
                                   "completion_tokens": 10, "reasoning_tokens": 0,
                                   "answer": {"masked": "S-ALT 9,99"}}},
        "rule_only_codes": ["ALT"], "llm_only_codes": ["GLU"], "facts": [{"value": "0,45"}],
    }  # fmt: skip
    card = sc.lab_card(entry)
    assert card["page"]["1"]["finish_reason"] == "stop" and "answer" not in card["page"]["1"]
    assert card["items"]["lab"]["machine"] == 2
    assert card["items"]["lab"]["unverified"] == card["items"]["lab"]["dropped"] == 1
    assert card["dropped_reasons"] == {"no_evidence": 1}
    _assert_counts_only(card)
    skipped = sc.clinical_card({"document_id": 2, "result": "skipped", "error": "no page text"})
    assert skipped["error"] == "no_page_text" and skipped["items"] == {}
    clin = sc.clinical_card({
        "document_id": 3, "result": "previewed", "counts": {"lesions": {"machine": 2,
                                                                       "unverified": 1}},
        "dropped": {"lesions:no_evidence": 2, "cycles:missing_required": 1},
    })  # fmt: skip
    lesions, cycles = clin["items"]["lesions"], clin["items"]["cycles"]
    assert lesions["machine"] == lesions["dropped"] == 2 and lesions["unverified"] == 1
    assert cycles["machine"] == cycles["unverified"] == 0 and cycles["dropped"] == 1
