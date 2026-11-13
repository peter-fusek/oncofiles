"""Bounded document-level parallelism of the LLM extraction jobs (#773).

``LLM_EXTRACT_PARALLEL_DOCS`` (default 1) / per-job ``parallel_docs`` (1-3,
dry run only): up to N documents run their model calls at once, each
document's pages one after another; every DB call stays on the event loop,
before or after the model phase, never overlapping another DB call.

Synthetic data only: the clinical pages are the invented worked examples of
the prompt (``llm_extract_clinical.EXAMPLES``), the lab page is the invented
page of ``tests/test_llm_extract.py``; every date is 2027+. The local model is
always mocked (with a sleep, to measure wall time).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import threading
import time
from unittest.mock import MagicMock

import pytest

from oncofiles import cloud_guard, config, pre_store
from oncofiles.curated import llm, llm_extract_call
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID
from tests.test_extraction_scorecard import DAY, DOB, PAGES_A, PAGES_B
from tests.test_extraction_scorecard import chat as model_chat
from tests.test_llm_extract import PAGE as LAB_PAGE

DOCS = [7781, 7782, 7783, 7784]  # clinical, two pages each
DOC_LAB = 7785
DELAY = 0.15  # seconds per mocked model call
_TIMING = {"seconds", "seconds_p50", "seconds_p95", "seconds_total", "wall_seconds",
           "page_seconds_total", "speedup", "started_at", "finished_at"}  # fmt: skip


class SlowModel:
    """The mocked local model with a sleep; records how many calls overlap."""

    def __init__(self, delay: float = DELAY):
        self.delay = delay
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0
        self.jobs: set[str | None] = set()

    def __call__(self, system, user, info=None, **kw):
        with self.lock:
            self.in_flight += 1
            self.calls += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.jobs.add(cloud_guard.local_only_job())
        try:
            time.sleep(self.delay)
            return model_chat(system, user, info=info, **kw)
        finally:
            with self.lock:
                self.in_flight -= 1


class GuardedDb:
    """Wraps the Database: fails the test when two DB awaits overlap, or when a
    DB call starts while a model call is in flight."""

    def __init__(self, inner, model: SlowModel):
        self._inner = inner
        self._model = model
        self._active = 0
        self.overlaps: list[str] = []
        self.during_model: list[str] = []
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        async def guarded(*args, **kw):
            self.calls.append(name)
            if self._active:
                self.overlaps.append(name)
            if self._model.in_flight:
                self.during_model.append(name)
            self._active += 1
            try:
                return await attr(*args, **kw)
            finally:
                self._active -= 1

        return guarded


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
    for doc_id in DOCS:
        await _doc(db, doc_id, PAGES_A, "report")
    await _doc(db, DOC_LAB, [LAB_PAGE], "labs")
    await db.db.commit()
    return db


@pytest.fixture
def model(monkeypatch):
    slow = SlowModel()
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(config, "LLM_CLINICAL_EXTRACT_DISABLED", False)
    monkeypatch.setattr(config, "LLM_EXTRACT_SHADOW_DISABLED", False)
    monkeypatch.setattr(config, "LLM_EXTRACT_PARALLEL_DOCS", 1)
    monkeypatch.setattr(llm, "chat", slow)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_runs", {})
    monkeypatch.setattr(tool, "_readme_births", {ERIKA_UUID: {DOB}})
    monkeypatch.setattr(tool, "_stopping", threading.Event())
    monkeypatch.setattr(pre_store, "make_copy", lambda db, job: f"oncofiles-copy-{job}.db")
    return slow


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


async def _wait(db):
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 30)
    return json.loads(await tool.curated_reocr_status(_ctx(db)))


def _untimed(obj):
    """The report without wall-clock numbers (they differ run to run)."""
    if isinstance(obj, dict):
        return {k: _untimed(v) for k, v in obj.items() if k not in _TIMING}
    if isinstance(obj, list):
        return [_untimed(v) for v in obj]
    return obj


async def _scorecard(db, **kw):
    tool._reocr_jobs.clear()
    started = json.loads(await tool.curated_extraction_scorecard(
        _ctx(db), DOCS, lab_document_ids=[DOC_LAB], **kw
    ))  # fmt: skip
    assert started["kind"] == "extraction_scorecard", started
    t0 = time.monotonic()
    status = await _wait(db)
    return status, time.monotonic() - t0


# ── speed-up, identical results, deterministic order ────────────────────


async def test_two_documents_at_once_halve_the_wall_time_with_identical_results(seeded, model):
    db = seeded
    seq, seq_wall = await _scorecard(db, label="seq")
    assert seq["state"] == "done", seq
    assert model.max_in_flight == 1
    model.max_in_flight = 0
    par, par_wall = await _scorecard(db, label="par", parallel_docs=2)
    assert par["state"] == "done", par
    assert model.max_in_flight == 2
    # nine model calls: two documents at a time take about half the wall time
    assert par_wall < 0.75 * seq_wall, (par_wall, seq_wall)
    a, b = seq["report"], par["report"]
    assert [d["document_id"] for d in b["documents"]] == DOCS + [DOC_LAB]
    assert _untimed(a["documents"]) == _untimed(b["documents"])
    assert _untimed(a["totals"]) == _untimed(b["totals"])
    # settings + diagnostics: parallel_docs, wall time next to the summed page seconds
    assert a["settings"]["parallel_docs"] == 1 and "parallel_docs" not in a["settings"]["overrides"]
    assert b["settings"]["parallel_docs"] == 2 and b["settings"]["overrides"]["parallel_docs"] == 2
    speed = b["totals"]["speed"]
    assert speed["wall_seconds"] > 0 and speed["seconds_total"] > speed["wall_seconds"]
    assert a["totals"]["speed"]["seconds_total"] >= a["totals"]["speed"]["wall_seconds"] * 0.9
    # every model call ran local-only, in its worker thread
    assert model.jobs <= {"curated clinical extraction", "curated LLM extract shadow"}


async def test_clinical_preview_parallel_matches_sequential_and_reports_timing(seeded, model):
    db = seeded
    reports = []
    for parallel in (None, 3):
        tool._reocr_jobs.clear()
        started = json.loads(await tool.curated_clinical_extract_preview(
            _ctx(db), DOCS, parallel_docs=parallel
        ))  # fmt: skip
        assert started["parallel_docs"] == (parallel or 1)
        status = await _wait(db)
        assert status["state"] == "done", status
        reports.append(status["report"])
    seq, par = reports
    assert [d["document_id"] for d in par["documents"]] == DOCS
    assert _untimed(seq["documents"]) == _untimed(par["documents"])
    assert _untimed(seq["totals"]) == _untimed(par["totals"])
    for report, n in ((seq, 1), (par, 3)):
        timing = report["timing"]
        assert timing["parallel_docs"] == n == report["settings"]["parallel_docs"]
        assert timing["wall_seconds"] > 0 and timing["page_seconds_total"] > 0
    assert par["timing"]["speedup"] > seq["timing"]["speedup"]


async def test_lab_shadow_preview_takes_parallel_docs(seeded, model):
    db = seeded
    started = json.loads(await tool.curated_llm_extract_preview(
        _ctx(db), [DOC_LAB, DOCS[0]], parallel_docs=2
    ))  # fmt: skip
    assert started["parallel_docs"] == 2 and started["overrides"]["parallel_docs"] == 2
    status = await _wait(db)
    assert status["state"] == "done", status
    report = status["report"]
    assert report["settings"]["parallel_docs"] == 2 and report["timing"]["parallel_docs"] == 2
    # the tool keeps the candidate order (as before), the job keeps that order
    assert [d["document_id"] for d in report["documents"]] == [DOCS[0], DOC_LAB]


# ── the single DB connection: no DB await overlaps, none during model calls ──


async def test_no_db_call_overlaps_or_runs_during_a_model_call(seeded, model):
    guarded = GuardedDb(seeded, model)
    status, _ = await _scorecard(guarded, label="guard", parallel_docs=3)
    assert status["state"] == "done", status
    assert model.max_in_flight == 3
    assert "get_ocr_pages" in guarded.calls and "finish_curated_run" in guarded.calls
    assert guarded.overlaps == [] and guarded.during_model == []


async def test_a_write_with_parallel_env_stores_in_order_without_db_overlap(
    seeded, model, monkeypatch
):
    monkeypatch.setattr(config, "LLM_EXTRACT_PARALLEL_DOCS", 2)
    guarded = GuardedDb(seeded, model)
    started = json.loads(await tool.curated_clinical_extract(_ctx(guarded), DOCS, dry_run=False))
    assert started["kind"] == "clinical_extract" and started["parallel_docs"] == 2
    status = await _wait(guarded)
    assert status["state"] == "done", status
    assert model.max_in_flight == 2
    docs = status["report"]["documents"]
    assert [d["document_id"] for d in docs] == DOCS
    assert {d["result"] for d in docs} == {"stored"}
    # a write streams its stores: a store may run while another document's
    # model call is in flight, but two DB awaits never overlap
    assert guarded.overlaps == []


# ── a write streams its stores (review of #793) ─────────────────────────

DOC_X, DOC_Y = 7791, 7792  # X: two pages, Y: the other worked examples


async def _provenance(db, doc_id):
    async with db.db.execute(
        "SELECT COUNT(*) FROM provenance WHERE source_document_id = ?", (doc_id,)
    ) as cur:
        return (await cur.fetchone())[0]


def _spy_store(monkeypatch, on_store):
    from oncofiles.curated import clinical_store

    store = clinical_store.store_document

    async def spy(db, **kw):
        res = await store(db, **kw)
        on_store(kw["document_id"])
        return res

    monkeypatch.setattr(clinical_store, "store_document", spy)


@pytest.mark.parametrize("ending", ["model_fails", "job_killed"])
async def test_a_write_stores_document_zero_while_document_one_still_extracts(
    seeded, model, monkeypatch, ending
):
    db = seeded
    await _doc(db, DOC_X, PAGES_A, "report")
    await _doc(db, DOC_Y, PAGES_B, "discharge")
    await db.db.commit()
    monkeypatch.setattr(config, "LLM_EXTRACT_PARALLEL_DOCS", 2)
    x_stored, release = threading.Event(), threading.Event()
    waited: list[bool] = []
    _spy_store(monkeypatch, lambda doc_id: doc_id == DOC_X and x_stored.set())

    def chat(system, user, info=None, **kw):
        if user.split("\n", 1)[1] in PAGES_B:  # document Y: wait until X is stored
            waited.append(x_stored.wait(timeout=10))
            if ending == "model_fails":
                raise RuntimeError("model gone")
            release.wait(timeout=10)
        return model_chat(system, user, info=info, **kw)

    monkeypatch.setattr(llm, "chat", chat)
    await tool.curated_clinical_extract(_ctx(db), [DOC_X, DOC_Y], dry_run=False)
    task = tool._reocr_jobs[ERIKA_UUID]["task"]
    if ending == "job_killed":
        for _ in range(200):  # until X is stored, then kill the job mid-Y
            if x_stored.is_set():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await asyncio.wait_for(task, 30)
        docs = {d["document_id"]: d for d in tool._reocr_jobs[ERIKA_UUID]["report"]["documents"]}
        assert docs[DOC_X]["result"] == "stored" and docs[DOC_Y]["result"] != "stored"
    assert waited and waited[0] is True  # X was stored before Y's model work ended
    assert await _provenance(db, DOC_X) > 0
    assert await _provenance(db, DOC_Y) == 0


async def test_a_write_with_one_slot_keeps_extract_store_next_order(seeded, model, monkeypatch):
    db = seeded
    events: list[str] = []
    _spy_store(monkeypatch, lambda doc_id: events.append(f"store:{doc_id}"))

    def chat(system, user, info=None, **kw):
        events.append("chat")
        return model_chat(system, user, info=info, **kw)

    monkeypatch.setattr(llm, "chat", chat)
    started = json.loads(await tool.curated_clinical_extract(_ctx(db), DOCS, dry_run=False))
    assert started["parallel_docs"] == 1
    status = await _wait(db)
    assert status["state"] == "done", status
    expected = []
    for doc_id in DOCS:
        expected += ["chat"] * len(PAGES_A) + [f"store:{doc_id}"]
    assert events == expected


# ── overrides: dry run only, validated ──────────────────────────────────


async def test_parallel_docs_override_is_refused_on_write_paths(seeded, model):
    db = seeded
    with pytest.raises(ValueError, match="dry run"):
        await tool.curated_clinical_extract(_ctx(db), DOCS, dry_run=False, parallel_docs=2)
    with pytest.raises(ValueError, match="dry run"):
        await tool.curated_llm_extract_preview(_ctx(db), [DOC_LAB], dry_run=False,
                                               parallel_docs=2)  # fmt: skip
    with pytest.raises(ValueError, match="dry run"):
        await tool._clinical_job(db, ERIKA_UUID, [], write=True, parallel_docs=2)
    assert ERIKA_UUID not in tool._reocr_jobs
    with pytest.raises(ValueError, match="dry run"):
        llm_extract_call.refuse_overrides_on_write(write=True, parallel_docs=1)
    llm_extract_call.refuse_overrides_on_write(write=False, parallel_docs=3)


@pytest.mark.parametrize("bad", [0, 4, -1, True, 2.0, "2"])
async def test_parallel_docs_bounds(seeded, model, bad):
    with pytest.raises(ValueError, match="parallel_docs"):
        llm_extract_call.parallel_docs(bad)
    with pytest.raises(ValueError, match="parallel_docs"):
        await tool.curated_extraction_scorecard(_ctx(seeded), DOCS, parallel_docs=bad)
    with pytest.raises(ValueError, match="parallel_docs"):
        await tool.curated_clinical_extract_preview(_ctx(seeded), DOCS, parallel_docs=bad)


def test_env_is_clamped_and_defaults_to_one(monkeypatch):
    if "LLM_EXTRACT_PARALLEL_DOCS" not in os.environ:
        assert config.LLM_EXTRACT_PARALLEL_DOCS == 1  # merging changes nothing
    monkeypatch.setattr(config, "LLM_EXTRACT_PARALLEL_DOCS", 7)
    assert llm_extract_call.parallel_docs() == 3
    monkeypatch.setattr(config, "LLM_EXTRACT_PARALLEL_DOCS", 0)
    assert llm_extract_call.parallel_docs() == 1
    assert llm_extract_call.parallel_docs(2) == 2


# ── default 1: today's behaviour ────────────────────────────────────────


async def test_default_is_one_document_at_a_time(seeded, model):
    db = seeded
    tool._reocr_jobs.clear()
    started = json.loads(await tool.curated_clinical_extract_preview(_ctx(db), DOCS))
    assert started["parallel_docs"] == 1 and "parallel_docs" not in started["overrides"]
    status = await _wait(db)
    assert status["state"] == "done", status
    assert model.max_in_flight == 1 and model.calls == len(DOCS) * len(PAGES_A)
    assert status["report"]["settings"]["parallel_docs"] == 1
    assert [d["document_id"] for d in status["report"]["documents"]] == DOCS


# ── stop: in-flight documents finish their current page, the rest never start ──


async def test_stop_mid_job_finishes_in_flight_pages_and_starts_no_more_documents(
    seeded, model, monkeypatch
):
    db = seeded
    both_started = threading.Barrier(2, timeout=10)

    def chat(system, user, info=None, **kw):
        if model.calls < 2:  # the first page of the first two documents
            with model.lock:
                model.calls += 1
            both_started.wait()
            tool.request_stop()
        else:  # pragma: no cover - would mean a page started after the stop
            with model.lock:
                model.calls += 1
        time.sleep(DELAY)
        return model_chat(system, user, info=info, **kw)

    monkeypatch.setattr(llm, "chat", chat)
    json.loads(await tool.curated_clinical_extract_preview(_ctx(db), DOCS, parallel_docs=2))
    status = await _wait(db)
    assert status["state"] == "done", status
    docs = status["report"]["documents"]
    assert [d["document_id"] for d in docs] == DOCS
    # the two in-flight documents finished page 1; page 2 was never sent
    for d in docs[:2]:
        assert d["result"] == "previewed" and d["pages_not_assessed"] == [2], d
    # the other two never started
    for d in docs[2:]:
        assert (d["result"], d["error"]) == ("skipped", "stopping"), d
    assert model.calls == 2


async def test_the_db_guard_itself_catches_an_overlap(seeded):
    """The guard is not vacuous: two DB awaits in flight together are caught."""
    model = SlowModel()
    guarded = GuardedDb(seeded, model)
    one = asyncio.create_task(guarded.get_ocr_pages(DOCS[0], patient_id=ERIKA_UUID))
    two = asyncio.create_task(guarded.get_ocr_pages(DOCS[1], patient_id=ERIKA_UUID))
    await one
    await two
    assert guarded.overlaps == ["get_ocr_pages"]
    model.in_flight = 1
    await guarded.get_ocr_pages(DOCS[0], patient_id=ERIKA_UUID)
    assert guarded.during_model == ["get_ocr_pages"]
