"""Pre-store safety copy before REAL re-read / re-OCR jobs (#573, infra review
of #701), the activity_log row of every real job, and the re-OCR guard.
Synthetic data only (invented values and filenames, dates 2027+)."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import config, pre_store, server
from oncofiles.database import Database
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID
from tests.test_admin_reread_console import _call, _request, _seed

EMAIL = "admin@example.com"


@pytest.fixture(autouse=True)
def _reset_jobs():
    tool._reocr_jobs.clear()
    tool._runs.clear()
    tool._stopping.clear()
    # the job skips documents above the RSS limit; the test process may be large
    with patch("oncofiles.memory.get_rss_mb", return_value=1.0):
        yield
    tool._reocr_jobs.clear()
    tool._runs.clear()


@pytest.fixture
async def file_db(tmp_path, monkeypatch):
    path = tmp_path / "data" / "oncofiles.db"
    path.parent.mkdir()
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    database = Database(path)
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


def _copies(directory) -> list[str]:
    if not directory.exists():
        return []
    return sorted(n for n in os.listdir(directory) if pre_store.NAME_RE.fullmatch(n))


def _quick_check(path) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA quick_check").fetchall()
    finally:
        conn.close()


async def _activity(db) -> list[dict]:
    async with db.db.execute(
        "SELECT patient_id, agent_id, tool_name, input_summary, output_summary, status "
        "FROM activity_log ORDER BY id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _runs(db) -> int:
    async with db.db.execute("SELECT COUNT(*) FROM curated_runs") as cur:
        return (await cur.fetchone())[0]


# ── the copy itself ──────────────────────────────────────────────────


async def test_copy_is_consistent_private_and_next_to_the_db(file_db):
    await _seed(file_db)
    name = await asyncio.to_thread(pre_store.make_copy, file_db, "reread")
    directory = config.DATABASE_PATH.parent / "pre-store"
    assert pre_store.NAME_RE.fullmatch(name) and name.endswith("-reread.db")
    copy = directory / name
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert _quick_check(copy) == [("ok",)]
    conn = sqlite3.connect(copy)
    try:  # the copy holds the data, not just the schema
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 4
    finally:
        conn.close()


def test_retention_keeps_exactly_five_and_touches_nothing_else(tmp_path, monkeypatch):
    path = tmp_path / "oncofiles.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    directory = tmp_path / "pre-store"
    directory.mkdir()
    bystanders = ["notes.db", "oncofiles-manual.db", "oncofiles-20270101T000000000000Z-x.db"]
    for n in bystanders:
        (directory / n).write_text("keep me")
    db = MagicMock(path=str(path), _use_turso=False)
    start = datetime(2027, 3, 1, tzinfo=UTC)
    names = [
        pre_store.make_copy(db, "reocr" if i % 2 else "reread", now=start + timedelta(minutes=i))
        for i in range(8)
    ]
    assert _copies(directory) == sorted(names)[-5:]
    assert all((directory / n).exists() for n in bystanders)


@pytest.mark.parametrize("raw", [":memory:", ""])
def test_no_local_file_means_no_copy(raw):
    with pytest.raises(pre_store.PreStoreError):
        pre_store.make_copy(MagicMock(path=raw, _use_turso=False), "reread")


def test_a_failed_quick_check_leaves_no_copy(tmp_path, monkeypatch):
    path = tmp_path / "oncofiles.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    with (
        patch.object(pre_store, "_quick_check", return_value=[("*** corrupt page",)]),
        pytest.raises(pre_store.PreStoreError, match="quick_check"),
    ):
        pre_store.make_copy(MagicMock(path=str(path), _use_turso=False), "reocr")
    assert _copies(tmp_path / "pre-store") == []


# ── a real job: copy first, refuse without it ────────────────────────


async def test_the_copy_exists_and_verifies_before_the_first_write(file_db):
    await _seed(file_db)
    seen = {}

    async def fake_job(db, pid, docs, *, dry_run, fingerprint=False):  # first write: run row
        directory = config.DATABASE_PATH.parent / "pre-store"
        seen["copies"] = _copies(directory)
        seen["check"] = _quick_check(directory / seen["copies"][0])
        seen["runs"] = await _runs(db)

    with patch.object(tool, "_reread_job", fake_job):
        out = await tool.start_reread(file_db, ERIKA_UUID, [501], dry_run=False, actor=EMAIL)
        await tool._reocr_jobs[ERIKA_UUID]["task"]
    assert seen == {"copies": [out["pre_store"]], "check": [("ok",)], "runs": 0}


@pytest.mark.parametrize("name", ["api_admin_reread", "api_admin_reocr"])
async def test_backup_failure_is_409_and_writes_nothing(file_db, name):
    await _seed(file_db)
    body = {"patient_slug": ERIKA_UUID, "document_ids": [501], "dry_run": False}
    with (
        patch.object(tool, "_reread_job", AsyncMock()) as rr,
        patch.object(tool, "_reocr_job", AsyncMock()) as ro,
        patch.object(
            pre_store.sqlite3, "connect", side_effect=sqlite3.OperationalError("disk I/O error")
        ),
    ):
        resp, data = await _call(
            getattr(server, name), _request(file_db, body=body), admin=True, email=EMAIL
        )
    assert resp.status_code == 409 and "pre-store copy failed" in data["error"]
    assert not rr.called and not ro.called and tool._reocr_jobs == {}
    assert await _runs(file_db) == 0 and await _activity(file_db) == []
    assert _copies(config.DATABASE_PATH.parent / "pre-store") == []


async def test_in_memory_db_refuses_a_real_store(db):
    await _seed(db)
    body = {"patient_slug": ERIKA_UUID, "document_ids": [501], "dry_run": False}
    with patch.object(tool, "_reread_job", AsyncMock()) as rr:
        resp, _ = await _call(server.api_admin_reread, _request(db, body=body), admin=True)
    assert resp.status_code == 409 and not rr.called


async def test_a_failed_copy_keeps_the_previous_job_status(file_db):
    await _seed(file_db)
    tool._reocr_jobs[ERIKA_UUID] = {"state": "done", "kind": "reocr", "dry_run": True}
    with patch("oncofiles.pre_store.make_copy", side_effect=pre_store.PreStoreError("x")):
        out = await tool.start_reread(file_db, ERIKA_UUID, [501], dry_run=False)
    assert "nothing was written" in out["error"]
    assert tool._reocr_jobs[ERIKA_UUID] == {"state": "done", "kind": "reocr", "dry_run": True}


async def test_the_slot_is_reserved_while_the_copy_is_made(file_db):
    await _seed(file_db)
    gate = asyncio.Event()

    def slow_copy(db, job):
        asyncio.run_coroutine_threadsafe(gate.wait(), loop).result(timeout=5)
        return "copy.db"

    loop = asyncio.get_running_loop()
    with (
        patch("oncofiles.pre_store.make_copy", side_effect=slow_copy),
        patch.object(tool, "_reread_job", AsyncMock()),
        patch.object(tool, "_reocr_job", AsyncMock()),
    ):
        first = asyncio.create_task(tool.start_reread(file_db, ERIKA_UUID, [501], dry_run=False))
        await asyncio.sleep(0.05)
        second = await tool.start_reocr(file_db, ERIKA_UUID, dry_run=True, document_ids=[501])
        gate.set()
        out = await first
        await tool._reocr_jobs[ERIKA_UUID]["task"]
    assert "busy" in second["error"] and out["pre_store"] == "copy.db"


# ── activity_log: every real job, attributed to the patient ──────────


def _clients():
    gdrive = MagicMock()
    return AsyncMock(return_value=(gdrive, None, None, "folder1"))


async def test_a_real_reread_writes_one_attributed_activity_row(file_db):
    await _seed(file_db)
    result = {
        "document_id": 501,
        "confirmed": 2,
        "date_unconfirmed": 0,
        "unconfirmed": 0,
        "candidates": 2,
        "result": "previewed", "error": None, "engine": "qwen-local",
        "sidecar_sha": "sha", "source_md5": "md",
        "stored": {"confirmed": [], "date_unconfirmed": []},
    }  # fmt: skip
    with (
        patch("oncofiles.server._create_patient_clients", _clients()),
        patch.object(tool, "_reread_one", return_value=result),
        patch.object(tool, "_corroborate_and_measure", AsyncMock(return_value={"coded": 2})),
    ):
        out = await tool.start_reread(file_db, ERIKA_UUID, [501], dry_run=False, actor=EMAIL)
        await tool._reocr_jobs[ERIKA_UUID]["task"]
    [row] = await _activity(file_db)
    assert row["patient_id"] == ERIKA_UUID and row["agent_id"] == EMAIL
    assert row["tool_name"] == "curated_reread_verify" and row["status"] == "ok"
    inp, outp = json.loads(row["input_summary"]), json.loads(row["output_summary"])
    assert inp["document_ids"] == [501] and inp["pre_store"] == out["pre_store"]
    assert inp["by"] == EMAIL and inp["dry_run"] is False
    assert outp["state"] == "done" and outp["results"] == {"stored": 1}
    assert outp["confirmed"] == outp["candidates"] == 2
    assert outp["date_unconfirmed"] == outp["unconfirmed"] == 0
    assert set(outp) == {"state", "results", "confirmed", "date_unconfirmed", "unconfirmed",
                         "candidates"}  # fmt: skip
    last = await file_db.get_last_reocr_job(patient_id=ERIKA_UUID)
    assert last["report"]["pre_store"] == out["pre_store"]


async def test_a_dry_run_only_counts(file_db):
    await _seed(file_db)
    before = dict(tool._dry_runs)
    with (
        patch("oncofiles.server._create_patient_clients", _clients()),
        patch.object(tool, "_reread_one", return_value={"document_id": 501, "result": "previewed"}),
    ):
        out = await tool.start_reread(file_db, ERIKA_UUID, [501], dry_run=True, actor=EMAIL)
        await tool._reocr_jobs[ERIKA_UUID]["task"]
    assert "pre_store" not in out and await _activity(file_db) == []
    assert tool._dry_runs["reread"] == before["reread"] + 1
    assert _copies(config.DATABASE_PATH.parent / "pre-store") == []


# ── the console never overrides the re-OCR guard ─────────────────────


async def test_console_reocr_keeps_the_guards_review_outcome(file_db):
    from oncofiles.curated.reocr import DocResult, ReocrReport

    await _seed(file_db)
    report = ReocrReport(dry_run=False)
    report.documents.append(
        DocResult(
            501,
            "20270511_Synth_501.pdf",
            ["cowork_ocr"],
            result="review",
            lost_codes={"WBC": 1},
        )  # fmt: skip
    )
    body = {"patient_slug": ERIKA_UUID, "document_ids": [501], "dry_run": False,
            "force": True, "override": True}  # fmt: skip
    with (
        patch("oncofiles.server._create_patient_clients", _clients()),
        patch("oncofiles.curated.reocr.run_reocr", return_value=report) as run,
        patch.object(tool, "index_patient", AsyncMock()) as index,
    ):
        resp, _ = await _call(
            server.api_admin_reocr, _request(file_db, body=body), admin=True, email=EMAIL
        )
        await tool._reocr_jobs[ERIKA_UUID]["task"]
    assert resp.status_code == 202
    # same call as the MCP tool: no override knob reaches the guard
    assert set(run.call_args.kwargs) == {"dry_run", "passes", "now", "stop", "reader",
                                         "rss_limit_mb"}  # fmt: skip
    assert run.call_args.kwargs["dry_run"] is False
    job = tool._reocr_jobs[ERIKA_UUID]
    assert [d["result"] for d in job["report"]["documents"]] == ["review"]
    assert not index.called  # nothing rewritten, so nothing re-indexed
    [row] = await _activity(file_db)
    assert json.loads(row["output_summary"])["results"] == {"review": 1}
    assert "6.12" not in row["output_summary"] + row["input_summary"]
    view = tool.admin_job_view(await tool.reocr_status(file_db, ERIKA_UUID))
    [shown] = view["results"]
    assert shown["result"] == "review"
    assert shown["lost_count"] == 1
