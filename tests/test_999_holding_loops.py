"""#999 job 1: holding_move + holding_extract run without APScheduler.

Production runs ``SYNC_ENABLED=false`` (#784), so the two #950 holding jobs,
registered in APScheduler, never ran there. They are now lifespan loops
(``oncofiles.holding_loops`` on ``oncofiles.interval_job``), their only
periodic owner. Covered:

* both loops start with ``SYNC_ENABLED=false``, are not APScheduler jobs with
  true (never twice), run in a local-only zone named after the job, the mover
  under ``_sync_semaphore``;
* an iteration that raises is recorded (class only) and the loop goes on;
* operator ``/readiness.jobs`` carries their telemetry, counts only;
* a missing holding directory: an iteration reports zero counts and never
  creates it; the dry run neither;
* ``oncofiles.job_dryrun holding_move`` (the "check before" tool) gives the
  same counts as one real pass on the same fixture, opens the DB read-only,
  writes nothing, imports no config and cannot open a socket.

Synthetic only: invented ids, md5s and addresses, dates in 2027.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from oncofiles import (
    holding_extract,
    holding_loops,
    holding_move,
    holding_store,
    job_dryrun,
    scheduled_jobs,
    scheduler_singleton,
    trial_funnel,
)
from oncofiles.cloud_guard import local_only_job
from oncofiles.database import Database
from oncofiles.interval_job import IntervalJob, safe_counts
from tests.test_856_erase_retention_loop import lifespan_env  # noqa: F401 - fixture

NOW = "2027-03-01T10:00:00Z"
ENDS = datetime(
    2027,
    12,
    31,
    tzinfo=UTC,
).strftime("%Y-%m-%dT%H:%M:%SZ")
P_MOVE = "00000000-0000-4000-8000-000000009901"  # trial with Drive connected
P_READ = "00000000-0000-4000-8000-000000009902"  # trial, documents waiting to be read
P_READY = "00000000-0000-4000-8000-000000009903"  # trial, ready mail due
ROOT = "idSynthRoot9990001"


def _md5(tag: str) -> str:
    return hashlib.md5(tag.encode(), usedforsecurity=False).hexdigest()


@pytest.fixture(autouse=True)
def _clean():
    scheduler_singleton._reset_for_tests()
    holding_loops._reset_for_tests()
    trial_funnel.reset_state()
    holding_extract.reset_state()
    holding_move._drain_task = None
    yield
    sched = scheduler_singleton._scheduler
    if scheduler_singleton._scheduler_alive(sched):
        sched.shutdown(wait=False)
    scheduler_singleton._reset_for_tests()
    holding_loops._reset_for_tests()
    trial_funnel.reset_state()
    holding_extract.reset_state()


# ── a synthetic DB file ──────────────────────────────────────────────────


async def _seed(path: Path) -> None:
    db = Database(str(path))
    await db.connect()
    await db.migrate()
    c = db.db
    for n, pid in enumerate((P_MOVE, P_READ, P_READY), start=1):
        await c.execute(
            "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
            "VALUES (?, ?, 'Trial', ?)",
            (pid, f"trial-synth{n}", f"synthcarer{n}@example.invalid"),
        )
        await c.execute(
            "INSERT INTO trial_accounts (patient_id, sender_hash, source, trial_ends_at) "
            "VALUES (?, ?, 'welcome_email', ?)",
            (pid, f"synthhash{n}", ENDS),
        )
    await c.execute(
        "INSERT INTO oauth_tokens (patient_id, provider, access_token, refresh_token, "
        "gdrive_folder_id) VALUES (?, 'google', 'synthetic-a', 'synthetic-r', ?)",
        (P_MOVE, ROOT),
    )
    docs = [
        # P_MOVE: one held row already filed (md5 in the curated tree), two to copy,
        # one already moved whose curated mirror exists (superseded candidate)
        (P_MOVE, "holding:11111111-1111-4111-8111-111111111111", {"md5": _md5("a")}),
        (P_MOVE, "holding:22222222-2222-4222-8222-222222222222", {"md5": _md5("b")}),
        (P_MOVE, "holding:33333333-3333-4333-8333-333333333333", {"md5": _md5("c")}),
        (
            P_MOVE,
            "holding:44444444-4444-4444-8444-444444444444",
            {"md5": _md5("d"), "moved_to_drive": {"drive_id": "idSynthMoved9990", "via": "x"}},
        ),
        # P_READ: two documents waiting for the reader, one already read
        (P_READ, "holding:55555555-5555-4555-8555-555555555555", {"extract": "pending"}),
        (P_READ, "holding:66666666-6666-4666-8666-666666666666", {"extract": "pending"}),
        (P_READ, "holding:77777777-7777-4777-8777-777777777777", {"extract": "done"}),
        # P_READY: everything read -> the ready mail is due
        (P_READY, "holding:88888888-8888-4888-8888-888888888888", {"extract": "done"}),
    ]
    for n, (pid, file_id, held) in enumerate(docs):
        held = {"extract": "done", **held}
        await c.execute(
            "INSERT INTO documents (patient_id, file_id, filename, original_filename, "
            "mime_type, structured_metadata) VALUES (?, ?, ?, ?, 'application/pdf', ?)",
            (pid, file_id, f"synth{n}.pdf", f"synth{n}.pdf", json.dumps({"holding": held})),
        )
    for n, (drive_id, md5) in enumerate(
        [("idSynthFiled9991", _md5("a")), ("idSynthMoved9990", _md5("zz"))]
    ):
        await c.execute(
            "INSERT INTO documents (patient_id, file_id, gdrive_id, filename, original_filename, "
            "mime_type, structured_metadata) VALUES (?, ?, ?, ?, ?, 'application/pdf', ?)",
            (
                P_MOVE,
                f"gdrive:{drive_id}",
                drive_id,
                f"filed{n}.pdf",
                f"filed{n}.pdf",
                json.dumps({"curated": {"source_md5": md5}}),
            ),
        )
    await c.commit()
    await db.close()


@pytest.fixture
async def seeded(tmp_path):
    path = tmp_path / "synth999.db"
    await _seed(path)
    return path


def _dry(path: Path, **kw) -> dict:
    return job_dryrun.run("holding_move", path, now=NOW, **kw)


# ── the dry run equals one real pass ─────────────────────────────────────


@pytest.fixture
def fake_drive(monkeypatch):
    from oncofiles import dashboard_upload, mailer

    copies: list[str] = []

    async def _inbox(db, patient_id, log_pid=None):
        return dashboard_upload._Target("curated_inbox", object(), ROOT), {}

    async def _copy(target, name, data, mime):
        copies.append(name)
        return {"status": "uploaded", "id": f"idSynthCopy999{len(copies):03d}"}

    monkeypatch.setattr(holding_store, "configured", lambda: True)
    monkeypatch.setattr(holding_move, "_read_held", lambda file_id, pid: b"synthetic bytes")
    monkeypatch.setattr(dashboard_upload, "stored_inbox_md5_ids", _inbox)
    monkeypatch.setattr(holding_move, "_copy_one", _copy)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(mailer, "send_mail", sent)
    return SimpleNamespace(copies=copies, sent=sent)


async def test_dry_run_counts_match_one_real_mover_pass(seeded, fake_drive):
    dry = _dry(seeded)
    move = dry["move"]
    assert move["trials_eligible"] == 1
    assert move["held_rows_to_move"] == 3
    assert move["held_rows_present_in_filed_tree"] == 1
    assert move["held_rows_would_copy"] == move["held_rows_would_copy_this_run"] == 2
    assert move["held_rows_already_moved"] == 1
    assert move["trials_would_convert"] == 1
    assert dry["ready_mail"]["trials_due"] == 2  # P_MOVE and P_READY
    assert dry["extract"]["held_docs_pending"] == 2

    db = Database(str(seeded))
    await db.connect()
    try:
        counts = await holding_move.run_pending(db)
    finally:
        await db.close()
    assert counts["copied"] == move["held_rows_would_copy_this_run"] == len(fake_drive.copies)
    assert counts["present"] == move["held_rows_present_in_filed_tree"]
    assert counts["converted"] == move["trials_would_convert"]
    assert counts["superseded"] == dry["superseded_would_flag"]
    assert counts["ready_mails"] == dry["ready_mail"]["would_send_now"] == 2

    after = _dry(seeded)  # the pass did its work: nothing left
    assert after["move"]["held_rows_to_move"] == 0
    assert after["move"]["trials_eligible"] == 0  # converted
    assert after["ready_mail"]["trials_due"] == 0


async def test_dry_run_reader_backlog_matches_one_real_reader_pass(seeded, monkeypatch):
    from oncofiles.curated import llm

    monkeypatch.setattr(holding_store, "configured", lambda: True)
    monkeypatch.setattr(llm, "configured", lambda: True)
    monkeypatch.setattr(holding_extract, "lm_busy", lambda now_mono=None: None)
    monkeypatch.setattr(
        holding_extract, "_decrypt_and_split", lambda file_id, pid, mime: ([b"x"], 1)
    )
    monkeypatch.setattr(holding_extract, "_budgets", AsyncMock(return_value=None))
    monkeypatch.setattr(
        holding_extract,
        "_extract",
        lambda data, mime: SimpleNamespace(
            excluded=None, ok=True, text="synthetic text", pages=1, method="text", error=None
        ),
    )
    monkeypatch.setattr(trial_funnel, "send_due_ready_mails", AsyncMock(return_value=0))
    pending = _dry(seeded)["extract"]["held_docs_pending"]
    db = Database(str(seeded))
    await db.connect()
    try:
        counts = await holding_extract.run_pending(db)
    finally:
        await db.close()
    assert counts["done"] == pending == 2
    assert _dry(seeded)["extract"]["held_docs_pending"] == 0


async def test_dry_run_is_read_only_and_leaves_no_file(seeded, tmp_path):
    before = sorted(p.name for p in seeded.parent.iterdir())
    blob = seeded.read_bytes()
    _dry(seeded, data_dir=tmp_path / "nodata")
    assert sorted(p.name for p in seeded.parent.iterdir()) == before
    assert seeded.read_bytes() == blob
    conn = job_dryrun.open_snapshot(seeded)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE trial_accounts SET converted_at = 'x'")
    finally:
        conn.close()
    assert sorted(p.name for p in seeded.parent.iterdir()) == before


def test_dry_run_refuses_a_copy_with_a_wal_file(seeded):
    wal = seeded.with_name(seeded.name + "-wal")
    wal.write_bytes(b"synthetic wal")
    with pytest.raises(job_dryrun.UnsafeCopyError):
        _dry(seeded)


async def test_dry_run_counts_only_never_ids_or_addresses(seeded):
    text = json.dumps(_dry(seeded))
    for secret in (P_MOVE, P_READ, P_READY, ROOT, "example.invalid", "synth0.pdf", _md5("a")):
        assert secret not in text


async def test_dry_run_counts_held_files_missing(seeded, tmp_path):
    data = tmp_path / "data"
    (data / "holding").mkdir(parents=True)
    (data / "holding" / "22222222-2222-4222-8222-222222222222.bin").write_bytes(b"v1synthetic")
    move = _dry(seeded, data_dir=data)["move"]
    assert move["held_files_missing"] == 2  # three rows to move, one file present
    assert _dry(seeded)["move"]["held_files_missing"] is None  # no --data-dir: not checked


def test_cli_offline_no_config_no_socket(seeded, tmp_path):
    """The CLI as infra runs it: no env, sockets blocked, no config import."""
    data = tmp_path / "absent-data"
    script = (
        "import json, socket, sys\n"
        "from oncofiles import job_dryrun\n"
        f"rc = job_dryrun.main(['holding_move', '--db', {str(seeded)!r}, "
        f"'--data-dir', {str(data)!r}, '--now', {NOW!r}])\n"
        "assert rc == 0\n"
        "assert 'oncofiles.config' not in sys.modules, 'config imported'\n"
        "assert 'oncofiles.server' not in sys.modules\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9))\n"
        "except Exception as exc:\n"
        "    assert type(exc).__name__ == 'NetworkForbiddenError', type(exc).__name__\n"
        "else:\n"
        "    raise AssertionError('socket was not blocked')\n"
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    out = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    report = json.loads(out.stdout)
    assert report["job"] == "holding_move" and report["move"]["trials_eligible"] == 1
    assert report["holding_dir"] == {"given": True, "exists": False}
    assert not data.exists()  # never created


# ── a missing holding directory (production today) ──────────────────────


async def test_missing_holding_dir_dry_run_zero_and_not_created(tmp_path):
    empty = tmp_path / "empty.db"
    db = Database(str(empty))
    await db.connect()
    await db.migrate()
    await db.close()
    data = tmp_path / "data"
    data.mkdir()
    report = job_dryrun.run("holding_move", empty, data_dir=data, now=NOW)
    assert report["holding_dir"] == {"given": True, "exists": False}
    move = report["move"]
    assert all(v == 0 for k, v in move.items() if k != "max_files_per_run")
    assert report["ready_mail"]["trials_due"] == 0
    assert report["extract"]["held_docs_pending"] == 0
    assert report["superseded_would_flag"] == 0
    assert not (data / "holding").exists()
    assert list(data.iterdir()) == []


async def test_missing_holding_dir_loop_iterations_zero_and_not_created(db, tmp_path, monkeypatch):
    from oncofiles import config
    from oncofiles.curated import llm

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(holding_store, "configured", lambda: True)
    monkeypatch.setattr(llm, "configured", lambda: True)
    monkeypatch.setattr(holding_extract, "lm_busy", lambda now_mono=None: None)
    assert not holding_store.holding_dir().exists()
    sem = asyncio.Semaphore(1)
    moved = await holding_loops.MOVE.run_once(lambda: holding_loops.move_iteration(db, sem))
    read = await holding_loops.EXTRACT.run_once(lambda: holding_loops.extract_iteration(db))
    assert moved == {} and read == {}
    assert holding_loops.MOVE.status()["last_ok"] and holding_loops.EXTRACT.status()["last_ok"]
    assert holding_loops.MOVE.status()["last_summary"]["moved"] == 0
    assert holding_loops.EXTRACT.status()["last_summary"]["docs_read"] == 0
    assert holding_loops.MOVE.status()["last_error"] is None
    assert not holding_store.holding_dir().exists()
    assert list(tmp_path.iterdir()) == []


# ── the loop ─────────────────────────────────────────────────────────────


class _Sleeps:
    def __init__(self, stop_after: int):
        self.calls: list[float] = []
        self.stop_after = stop_after

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) > self.stop_after:
            raise asyncio.CancelledError


async def test_loop_waits_the_grace_then_runs_every_interval_and_survives_errors(caplog):
    job = IntervalJob("synthetic_job", 600, summarize=lambda c: {"n": c.get("done", 0)})
    calls: list[int] = []

    async def fn():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("synthetic /secret/path/name.pdf")
        return Counter(done=2, **{"bad key with spaces": 5})

    sleeps = _Sleeps(stop_after=3)
    caplog.set_level("ERROR", logger="oncofiles.interval_job")
    with pytest.raises(asyncio.CancelledError):
        await job.loop(fn, grace_s=300, sleep=sleeps)
    assert sleeps.calls == [300, 600, 600, 600]
    assert len(calls) == 3  # the failure did not end the loop
    st = job.status()
    assert st["runs"] == 3 and st["errors"] == 1
    assert st["last_error_type"] == "OSError"
    assert st["last_counts"] == {"done": 2}  # code-like keys only
    assert st["last_summary"] == {"n": 2}
    assert st["running"] is False
    assert "synthetic_job: iteration failed (OSError)" in caplog.text
    assert "secret" not in caplog.text  # the class only, never the message


def test_safe_counts_keeps_codes_and_integers_only():
    raw = {
        "copied": 2,
        "x y": 1,
        "ok": "3",
        5: 1,
        "a@b": 1,
    }
    assert safe_counts(raw) == {"copied": 2}


def test_intervals_and_ids_come_from_the_job_modules():
    assert holding_loops.MOVE.interval_s == holding_move.WORKER_INTERVAL_MIN * 60
    assert holding_loops.EXTRACT.interval_s == holding_extract.WORKER_INTERVAL_MIN * 60
    assert holding_loops.MOVE_JOB_ID == "holding_move"
    assert holding_loops.EXTRACT_JOB_ID == "holding_extract"


async def test_move_iteration_holds_the_semaphore(db, monkeypatch):
    sem = asyncio.Semaphore(1)
    seen: dict = {}

    async def fake(db_):
        seen["locked"] = sem.locked()
        return Counter(copied=1)

    monkeypatch.setattr(holding_move, "run_pending", fake)
    assert await holding_loops.move_iteration(db, sem) == {"copied": 1}
    assert seen["locked"] is True


# ── lifespan: started whatever SYNC_ENABLED says, never twice ────────────


async def test_loops_start_with_sync_disabled(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    async with server.lifespan(MagicMock()):
        assert scheduler_singleton.current_scheduler() is None
        for job_id in holding_loops.JOBS:
            assert scheduler_singleton.background_loop_running(job_id)
            assert job_id in scheduler_singleton.status()["background_loops"]
        tasks = [scheduler_singleton._loops[j] for j in holding_loops.JOBS]
    await asyncio.sleep(0)
    assert all(t.cancelled() or t.done() for t in tasks)
    for job_id in holding_loops.JOBS:
        assert not scheduler_singleton.background_loop_running(job_id)


async def test_not_an_apscheduler_job_with_sync_enabled(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", True)
    monkeypatch.setattr(server, "AI_RUN_ON_STARTUP", False)
    async with server.lifespan(MagicMock()):
        sched, _ = scheduler_singleton.current_scheduler()
        ids = {j.id for j in sched.get_jobs()}
        assert not ids & set(holding_loops.JOBS)
        async with server.lifespan(MagicMock()):  # a second lifespan reuses them
            for job_id in holding_loops.JOBS:
                assert scheduler_singleton._loop_refs[job_id] == 2
        for job_id in holding_loops.JOBS:
            assert scheduler_singleton.background_loop_running(job_id)
    await asyncio.sleep(0)
    for job_id in holding_loops.JOBS:
        assert not scheduler_singleton.background_loop_running(job_id)


async def test_loops_run_in_local_only_zones(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    seen: dict = {}

    async def fake_move(db, semaphore):
        seen["move"] = (local_only_job(), semaphore)

    async def fake_extract(db):
        seen["extract"] = local_only_job()

    monkeypatch.setattr(holding_loops, "holding_move_loop", fake_move)
    monkeypatch.setattr(holding_loops, "holding_extract_loop", fake_extract)
    async with server.lifespan(MagicMock()):
        await asyncio.sleep(0)
    assert seen["move"] == ("holding_move", server._sync_semaphore)
    assert seen["extract"] == "holding_extract"


def test_the_734_scan_discovers_the_loops():
    from tests.test_no_automatic_deletion import _job_registrations, _load_modules

    roots, _ = _job_registrations(_load_modules()["oncofiles.server"])
    assert {"holding_move_loop", "holding_extract_loop"} <= set(roots)


# ── telemetry: operator /readiness + admin job list ──────────────────────


async def test_readiness_carries_loop_telemetry_counts_only(monkeypatch):
    import oncofiles.server as server
    from tests.test_one_scheduler_per_process import OPERATOR_TOKEN, _request

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)

    async def one():
        return Counter(
            copied=2,
            present=1,
            ready_mails=1,
            listing_failed=1,
        )

    await holding_loops.MOVE.run_once(one)
    request, _ = _request(operator=True)
    body = json.loads((await server.readiness(request)).body)
    move = body["jobs"]["holding_move"]
    assert move["loop_running"] is False
    assert move["last_ok"] and move["last_error"] is None and move["runs"] == 1
    assert move["schedule"] == "every 10 min"
    assert move["last_summary"]["moved"] == 3
    assert move["last_summary"]["errors"] == 1
    assert move["last_summary"]["ready_mails_sent"] == 1
    extract = body["jobs"]["holding_extract"]
    assert extract["runs"] == 0 and extract["schedule"] == "every 5 min"
    # never on the public answer
    public, _ = _request(operator=False)
    assert "jobs" not in json.loads((await server.readiness(public)).body)


def test_admin_job_list_shows_the_loops():
    assert set(holding_loops.JOBS) <= scheduled_jobs.BACKGROUND_LOOP_JOBS
    for job_id, job in holding_loops.JOBS.items():
        entry = scheduled_jobs.background_loop_entry(
            job_id, running=True, state=scheduled_jobs.background_loop_state(job_id)
        )
        assert entry["registered"] is True and entry["known"] is True
        assert entry["trigger"] == {
            "kind": "interval",
            "seconds": int(job.interval_s),
            "text": job.schedule_text,
        }
        off = scheduled_jobs.background_loop_entry(job_id, running=False, state={})
        assert off["reason"] == "loop_not_running"


def test_helper_shares_the_production_sql():
    """The dry run and the DB helpers run the same statements (no drift)."""
    from oncofiles import holding_sql
    from oncofiles.database import _trials

    src = Path(_trials.__file__).read_text()
    for name in (
        "TRIALS_TO_CONVERT_SQL",
        "HOLDING_ROWS_TO_MOVE_SQL",
        "HOLDING_MOVED_COUNT_SQL",
        "HOLDING_DUE_FOR_READING_FROM",
        "SUPERSEDED_CANDIDATES_SQL",
        "TRIALS_AWAITING_READY_MAIL_SQL",
    ):
        assert getattr(holding_sql, name)
        assert src.count(name) >= 2, name  # imported and used
    dry = Path(job_dryrun.__file__).read_text()
    assert "q.TRIALS_TO_CONVERT_SQL" in dry and "q.TRIALS_AWAITING_READY_MAIL_SQL" in dry
