"""Dry re-OCR compare (#883): read again, compare with today's sidecar, write nothing.

Synthetic data only (invented values, names and ids, dates 2027+)."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import config
from oncofiles.curated import llm, reocr
from oncofiles.curated import reocr_compare as rc
from oncofiles.curated.facts_rules import Vocabulary
from oncofiles.curated.reocr import Reread
from oncofiles.tools import curated as tool
from tests.helpers import PRIMARY_TEST_UUID, TEST_PATIENT_UUID
from tests.test_admin_reread_console import _doc, _fact
from tests.test_curated_reocr import _scan_pdf

PAGE = """Odobrané: 21.05.2027 08:05    Prijaté: 21.05.2027 08:30
Test                                Hodnota    Jednotky    Ref. medze        Hodnotenie    SOP
markery v sére
CEA v sére                          41.7       ng/ml                                          /
CA 19-9 v sére                      812.4      U/ml      0.0 - 30.9        ****            /
Kreatínin v sére                    40.8       µmol/l    44.0 - 71.0       *               /"""
SIDECAR = (
    "---\ndoc_date: 2027-05-21\ndoc_type: Labs\nocr_engine: qwen-local\n---\n\n"
    f"<!-- page 1 -->\n{PAGE}\n"
)
ONE_DIGIT = PAGE.replace("812.4", "817.4")
CA = "CA19_9"


@pytest.fixture
async def vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


class _Drive:
    """Download only; any other attribute is recorded as an attempted call."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.other: list[str] = []

    def download(self, file_id: str) -> bytes:
        return self.files[file_id]

    def __getattr__(self, name):
        self.other.append(name)
        raise AssertionError(f"compare must never call Drive.{name}")


def _reader(pages: list[str]):
    def read(name, content, mime="", **kw):
        assert kw["passes"] == 2 and kw["purpose"] == "reocr"
        return Reread(list(pages), "vision2", "qwen-local", dpi=[150, 220],
                      llm_passes=[{"page": 1, "pass": 1, "finish": "stop"}])  # fmt: skip

    return read


def _row(doc_id=501, sidecar="side", source="src"):
    curated = {"sidecar_id": sidecar, "sidecar_sha": "x"}
    return {"id": doc_id, "filename": f"20270521_Synth_{doc_id}.pdf", "gdrive_id": source,
            "mime_type": "application/pdf", "category": "labs",
            "structured_metadata": {"curated": curated}}  # fmt: skip


def _files(sidecar=SIDECAR):
    return {"side": sidecar.encode(), "src": b"%PDF-synthetic"}


# ── the comparison ──────────────────────────────────────────────────────


def test_a_reading_that_confirms_every_number_agrees_everywhere(vocab):
    drive = _Drive(_files())
    res = rc.compare_one(drive, _row(), vocab, reader=_reader([PAGE]))
    assert res["result"] == "compared" and drive.other == []
    t = res["totals"]
    assert t["numbers_today"] > 0 and t["numbers_confirmed"] == t["numbers_today"]
    assert t["numbers_not_found"] == t["numbers_uncertain"] == t["numbers_only_new"] == 0
    assert t["dates_today"] == t["dates_same"] == 1 and t["dates_not_found"] == 0
    f = res["facts"]
    assert f["agree"] == 3 and f["value_differs"] == f["missing_in_new"] == 0
    assert f["missing_in_today"] == 0
    assert res["sidecar_matches_index"] is False  # sha "x" is not today's sidecar
    assert res["llm_passes"] == [{"page": 1, "pass": 1, "finish": "stop"}]


def test_one_changed_digit_is_value_differs_for_that_code_only(vocab):
    res = rc.compare_one(_Drive(_files()), _row(), vocab, reader=_reader([ONE_DIGIT]))
    f = res["facts"]
    assert (f["agree"], f["value_differs"]) == (2, 1)
    assert f["codes"]["value_differs"] == {CA: 1}
    assert f["missing_in_new"] == f["missing_in_today"] == 0
    assert res["totals"]["numbers_not_found"] == 1 and res["totals"]["numbers_only_new"] == 1


def test_a_number_found_only_on_a_marked_line_is_uncertain(vocab):
    marked = PAGE.replace("ng/ml ", "ng/ml [?]")
    res = rc.compare_one(_Drive(_files()), _row(), vocab, reader=_reader([marked]))
    t = res["totals"]
    assert t["numbers_uncertain"] == 1 and t["numbers_not_found"] == 0
    assert t["numbers_confirmed"] == t["numbers_today"] - 1
    assert t["uncertain_lines_new"] == 1 and t["uncertain_lines_today"] == 0


def test_the_report_holds_counts_and_codes_never_values(vocab):
    res = rc.compare_one(_Drive(_files()), _row(), vocab, reader=_reader([ONE_DIGIT]))
    text = json.dumps(res)
    for token in ("812", "817", "41.7", "40.8", "2027-05-21", "21.05", "Kreat", "CEA v"):
        assert token not in text


def test_a_failed_reading_is_failed_with_its_usage(vocab):
    def read(*a, **kw):
        return Reread([], "none", "qwen-local", error="local LLM timeout",
                      llm_passes=[{"page": 1, "pass": 1, "error": "LocalLLMError"}])  # fmt: skip

    res = rc.compare_one(_Drive(_files()), _row(), vocab, reader=read)
    assert res["result"] == "failed" and res["error"] == "local LLM timeout"
    assert len(res["llm_passes"]) == 1 and "facts" not in res


def test_pages_missing_on_one_side_compare_with_an_empty_page():
    first, second = "a 7", "b 8"
    pages = rc.compare_pages([first, second], [first])
    assert [p["page"] for p in pages] == [1, 2]
    last = pages[-1]
    assert last["numbers_not_found"] == last["numbers_today"] == 1


def test_end_to_end_with_a_fake_model_and_independent_halves(vocab, monkeypatch):
    """The real ``reread`` (pass 1 + two pass-2 halves + reconcile) on a
    synthetic scan, a fake model reading the page we expect."""
    monkeypatch.setattr(llm, "model_id", lambda: "qwen-local")
    monkeypatch.setattr(llm, "chat", lambda system, content, **kw: PAGE)
    drive = _Drive({"side": SIDECAR.encode(), "src": _scan_pdf(1)})
    res = rc.compare_one(drive, _row(), vocab, reader=reocr.reread)
    assert res["result"] == "compared" and res["method"] == "vision2"
    passes = [r["pass"] for r in res["llm_passes"]]
    assert passes[0] == 1
    assert len(passes) == 3
    assert set(passes[1:]) == {2}
    assert [r.get("tile") for r in res["llm_passes"]] == [None, 1, 2]
    assert res["facts"]["agree"] == 3 and res["facts"]["value_differs"] == 0
    assert res["totals"]["numbers_not_found"] == 0


# ── the job ─────────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_runs", {})
    tool._stopping.clear()
    drive = _Drive({"side501": SIDECAR.encode(), "g501": b"%PDF-a",
                    "side502": SIDECAR.encode(), "g502": b"%PDF-b"})  # fmt: skip
    gdrive = MagicMock()
    monkeypatch.setattr(reocr, "reread_isolated", _reader([ONE_DIGIT]))
    with (
        patch("oncofiles.server._create_patient_clients",
              AsyncMock(return_value=(gdrive, None, None, "folder1"))),
        patch("oncofiles.curated.drive_ops.DriveOps", lambda own: drive),
        patch("oncofiles.memory.get_rss_mb", return_value=1.0),
    ):  # fmt: skip
        yield drive, gdrive


async def _seed(db):
    await db.db.execute(
        "INSERT OR IGNORE INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
        (TEST_PATIENT_UUID, "synth-other", "Synthetic Other"),
    )
    await _doc(db, 501, PRIMARY_TEST_UUID)
    await _doc(db, 502, PRIMARY_TEST_UUID)
    await _doc(db, 601, TEST_PATIENT_UUID)  # another patient's document
    await _fact(db, 501, PRIMARY_TEST_UUID, "CEA", verification="machine", meta={})
    for code in ("CEA", "CA19_9", "CREA"):
        await _fact(db, 502, PRIMARY_TEST_UUID, code, verification="machine", meta={})
    await _fact(db, 502, PRIMARY_TEST_UUID, "WBC")
    await db.db.commit()


async def _run(db, ids):
    out = await tool.start_reocr_compare(db, PRIMARY_TEST_UUID, ids)
    assert out.get("kind") == "reocr_compare", out
    await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    return tool._reocr_jobs[PRIMARY_TEST_UUID]


async def test_the_job_compares_most_machine_facts_first_and_writes_nothing(db, env):
    drive, gdrive = env
    await _seed(db)
    changes = db.db.total_changes
    job = await _run(db, [501, 502, 601])
    assert job["state"] == "done", job
    docs = job["report"]["documents"]
    assert [d["document_id"] for d in docs] == [502, 501, 601]
    top = docs[0]
    assert top["machine_facts"] == 3
    assert top["unverified_facts"] == 1
    assert docs[1]["machine_facts"] == 1
    assert top["facts"]["codes"]["value_differs"] == {CA: 1}
    assert docs[2] == {"document_id": 601, "result": "skipped", "error": "not_found"}
    assert job["report"]["totals"]["facts_value_differs"] == 2
    # nothing written: no DB change (not even a run row), no Drive call but downloads
    assert db.db.total_changes == changes
    assert drive.other == []
    assert [c[0] for c in gdrive.dedicated_copy.return_value.method_calls] == ["close", "close"]
    assert not tool.run_active()  # a deploy may cut it
    status = await tool.reocr_status(db, PRIMARY_TEST_UUID)
    assert status["kind"] == "reocr_compare" and status["dry_run"] is True


async def test_more_than_ten_ids_are_refused(db, env):
    out = await tool.start_reocr_compare(db, PRIMARY_TEST_UUID, list(range(700, 711)))
    assert out["error"].startswith("too many")
    assert tool._reocr_jobs == {}


async def test_no_ids_is_refused(db, env):
    assert "required" in (await tool.start_reocr_compare(db, PRIMARY_TEST_UUID, []))["error"]


@pytest.mark.parametrize("kind", ["reocr", "reread", "llm_shadow", "reocr_compare"])
async def test_refused_while_another_job_runs_for_the_patient(db, env, kind):
    await _seed(db)
    tool._reocr_jobs[PRIMARY_TEST_UUID] = {"state": "running", "kind": kind}
    out = await tool.start_reocr_compare(db, PRIMARY_TEST_UUID, [501])
    assert out["error"].startswith("busy")
    assert tool._reocr_jobs[PRIMARY_TEST_UUID] == {"state": "running", "kind": kind}


async def test_refused_while_the_patient_lock_is_held(db, env):
    await _seed(db)
    async with tool.patient_lock(PRIMARY_TEST_UUID):
        out = await tool.start_reocr_compare(db, PRIMARY_TEST_UUID, [501])
    assert out["error"].startswith("busy")


async def test_refused_while_a_curated_run_is_active(db, env):
    await _seed(db)
    tool._runs[PRIMARY_TEST_UUID] = {"state": "running"}
    assert (await tool.start_reocr_compare(db, PRIMARY_TEST_UUID, [501]))["error"].startswith(
        "busy"
    )


async def test_a_running_compare_never_blocks_a_curated_run(db, env):
    tool._reocr_jobs[PRIMARY_TEST_UUID] = {"state": "running", "kind": "reocr_compare"}
    assert tool.run_block_reason(PRIMARY_TEST_UUID) is None


async def test_another_patients_document_is_never_read(db, env):
    drive, _ = env
    await _seed(db)
    out = await tool.start_reocr_compare(db, PRIMARY_TEST_UUID, [601])
    not_found = {"document_id": 601, "result": "skipped", "error": "not_found"}
    assert out == {"status": "nothing to do", "skipped": [not_found]}
    assert tool._reocr_jobs == {}


class _Ctx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


@contextmanager
def _as_caller(*, admin: bool, pid: str = PRIMARY_TEST_UUID):
    """The verified caller (admin flag, bound patient) inside the test's context."""
    from oncofiles.persistent_oauth import (
        _verified_caller_email,
        _verified_caller_is_admin,
        _verified_patient_id,
    )

    tokens = [(_verified_caller_is_admin, _verified_caller_is_admin.set(admin)),
              (_verified_caller_email, _verified_caller_email.set("")),
              (_verified_patient_id, _verified_patient_id.set(pid))]  # fmt: skip
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


async def _primary_slug(db) -> str:
    async with db.db.execute(
        "SELECT slug FROM patients WHERE patient_id = ?", (PRIMARY_TEST_UUID,)
    ) as cur:
        return (await cur.fetchone())[0]


async def test_a_non_admin_is_refused_even_for_the_own_patient(db, env):
    await _seed(db)
    slug = await _primary_slug(db)
    with _as_caller(admin=False):
        for kw in ({}, {"patient_slug": slug}, {"patient_slug": "synth-other"}):
            out = json.loads(await tool.curated_reocr_compare(_Ctx(db), [501], **kw))
            assert "requires admin scope" in out["error"]
    assert tool._reocr_jobs == {}


async def test_an_admin_without_a_patient_slug_is_refused(db, env):
    await _seed(db)
    with _as_caller(admin=True):
        for slug in (None, "", "  "):
            out = json.loads(await tool.curated_reocr_compare(_Ctx(db), [501], patient_slug=slug))
            assert out == {"error": "patient_slug is required"}
    assert tool._reocr_jobs == {}


async def test_an_admin_with_a_patient_slug_starts_a_job(db, env):
    await _seed(db)
    slug = await _primary_slug(db)
    with _as_caller(admin=True, pid=""):
        out = json.loads(await tool.curated_reocr_compare(_Ctx(db), [501], patient_slug=slug))
    assert out["kind"] == "reocr_compare" and out["dry_run"] is True
    await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    assert tool._reocr_jobs[PRIMARY_TEST_UUID]["state"] == "done"


def test_the_tool_is_registered_read_only():
    from fastmcp import FastMCP

    mcp = FastMCP("t")
    tool.register(mcp)
    found = asyncio.run(mcp.get_tool("curated_reocr_compare"))
    assert found.annotations.readOnlyHint is True
