"""#999 job 2: the inbound queue sweep runs without APScheduler.

Production runs ``SYNC_ENABLED=false`` (#784), so the #952 ``inbound_queue``
job (claimed-row DKIM verification, the durable queue, then the #950
quarantine expiry / purge / digests), registered in APScheduler, only ran
through the kick after an enqueue. It is now a lifespan loop
(``oncofiles.inbound_loop`` on ``oncofiles.interval_job``), its only periodic
owner. Covered:

* the loop starts with ``SYNC_ENABLED=false``, is not an APScheduler job with
  true (never twice), runs in the ``inbound_queue`` local-only zone under
  ``_sync_semaphore``;
* an iteration that raises is recorded (class only) and the loop goes on;
* operator ``/readiness.jobs.inbound_queue`` and the admin job list carry its
  telemetry, counts only;
* ``oncofiles.job_dryrun inbound_maintenance`` gives the same counts as one
  real pass on the same fixture (fake mailer; the payloads never reach the
  network), opens the DB read-only, writes nothing, imports no config, cannot
  open a socket and never creates the quarantine directory.

Synthetic only: invented ids, hashes and addresses, timestamps in 2027.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from oncofiles import (
    inbound_email,
    inbound_loop,
    inbound_quarantine,
    inbound_queue,
    job_dryrun,
    scheduled_jobs,
    scheduler_singleton,
)
from oncofiles.cloud_guard import local_only_job
from oncofiles.database import Database
from tests.test_856_erase_retention_loop import lifespan_env  # noqa: F401 - fixture

NOW = "2027-04-01T10:00:00Z"
NOW_DT = datetime.strptime(NOW, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
PA = "00000000-0000-4000-8000-000000009911"
PB = "00000000-0000-4000-8000-000000009912"
CARER_A = "synthcarer9911@example.invalid"
CARER_B = "synthcarer9912@example.invalid"
JOBS_SECRET = "synthetic-inbound-jobs-secret-999"
HOLD_SECRET = "synthetic-holding-secret-999"


def ago(**kw) -> str:
    return (NOW_DT - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ahead(**kw) -> str:
    return (NOW_DT + timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


# Named instants and counts keep each fixture line short (fixture guard).
T_MIN_FIVE = ago(minutes=5)
T_MIN_TWENTY = ago(minutes=20)
T_MIN_THIRTY = ago(minutes=30)
T_MIN_FORTY = ago(minutes=40)
T_MIN_FIFTY = ago(minutes=50)
T_HOUR = ago(hours=1)
T_TWO_DAYS = ago(days=2)
T_FIVE_DAYS = ago(days=5)
T_EIGHT_DAYS = ago(days=8)
T_TEN_DAYS = ago(days=10)
T_TWELVE_DAYS = ago(days=12)
T_MONTH_PLUS = ago(days=31)
T_FORTY_DAYS = ago(days=40)
T_SEVENTY_DAYS = ago(days=70)
LATER = ahead(hours=1)
MAXA = inbound_queue.MAX_ATTEMPTS
GARBAGE = "not-a-payload"  # undecryptable
ZERO, ONE, TWO = range(3)


def _uid(n: int) -> str:
    return str(uuid.UUID(int=0x9990 * 2**64 + n))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    scheduler_singleton._reset_for_tests()
    inbound_loop._reset_for_tests()
    inbound_email.reset_state()
    inbound_quarantine._last_failed.clear()
    inbound_quarantine._purge_last.clear()
    monkeypatch.delenv("QUARANTINE_PURGE_APPROVED", raising=False)
    monkeypatch.delenv("QUARANTINE_PURGE_APPROVED_AFTER_DAYS", raising=False)
    yield
    sched = scheduler_singleton._scheduler
    if scheduler_singleton._scheduler_alive(sched):
        sched.shutdown(wait=False)
    scheduler_singleton._reset_for_tests()
    inbound_loop._reset_for_tests()
    inbound_email.reset_state()
    inbound_quarantine._last_failed.clear()
    inbound_quarantine._purge_last.clear()


@pytest.fixture
def secrets_env(monkeypatch, tmp_path):
    """Both secrets set, the data dir in tmp, the clocks frozen at NOW."""
    monkeypatch.setenv("INBOUND_JOBS_SECRET", JOBS_SECRET)
    monkeypatch.setenv("HOLDING_STORE_SECRET", HOLD_SECRET)
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "synthetic-bearer-999")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("oncofiles.config.DATA_DIR", str(data))
    monkeypatch.setattr(inbound_queue, "_now", lambda: NOW_DT)
    monkeypatch.setattr(inbound_quarantine, "_now", lambda: NOW_DT)
    return data


# ── a synthetic DB file ──────────────────────────────────────────────────


def _enc(payload: dict, *, scope: str, token_hash: str, job_uuid: str) -> str:
    return inbound_queue.encrypt_payload(
        {"v": inbound_queue.PAYLOAD_VERSION, **payload},
        patient_id=scope,
        token_hash=token_hash,
        job_uuid=job_uuid,
    )


async def _seed(path: Path, data: Path) -> dict:
    """Every branch the sweep counts, each once; returns the bytes on disk."""
    db = Database(str(path))
    await db.connect()
    await db.migrate()
    c = db.db
    for pid, slug, carer in ((PA, "synth-qa", CARER_A), (PB, "synth-qb", CARER_B)):
        await c.execute(
            "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
            "VALUES (?, ?, 'Synthetic', ?)",
            (pid, slug, carer),
        )
    # inbound_jobs: three claimable (max_attempts, undecryptable,
    # download_expired), one not due yet, one done
    th = "synthtokenhash1"
    jobs = [
        ("pending", MAXA, T_MIN_FIVE, T_HOUR, GARBAGE),
        ("pending", ZERO, T_MIN_FIVE, T_HOUR, GARBAGE),
        ("pending", ONE, T_MIN_FIVE, T_TWO_DAYS, {"attachments": [{"token": "t"}]}),
        ("pending", ZERO, LATER, T_HOUR, GARBAGE),
        ("done", ONE, T_HOUR, T_HOUR, GARBAGE),
    ]
    for n, (state, attempts, due, created, payload) in enumerate(jobs):
        ju = _uid(100 + n)
        blob = (
            payload
            if isinstance(payload, str)
            else _enc(payload, scope=PA, token_hash=th, job_uuid=ju)
        )
        await c.execute(
            "INSERT INTO inbound_jobs (job_uuid, patient_id, token_hash, message_id_hash, "
            "sender_hash, trusted, payload_enc, state, attempts, next_attempt_at, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, 'synthsender', 1, ?, ?, ?, ?, ?, ?)",
            (ju, PA, th, f"mid{n}", blob, state, attempts, due, created, created),
        )
    # claimed rows: one deferred too old (expires), one deferred promotable
    # (then undecryptable), one claimable too old, one at max attempts
    claimed = [
        ("deferred", ZERO, T_TWO_DAYS, GARBAGE),
        ("deferred", ZERO, T_MIN_TWENTY, GARBAGE),
        ("pending", ONE, T_TWO_DAYS, {}),
        ("pending", MAXA, T_HOUR, GARBAGE),
    ]
    for n, (state, attempts, created, payload) in enumerate(claimed):
        ju = _uid(200 + n)
        blob = (
            payload
            if isinstance(payload, str)
            else _enc(payload, scope="claimed", token_hash="synthbucket", job_uuid=ju)
        )
        await c.execute(
            "INSERT INTO inbound_claimed_jobs (job_uuid, bucket, message_id_hash, sender_hash, "
            "domain_hash, delivery_hash, payload_enc, state, attempts, next_attempt_at, "
            "created_at, updated_at) VALUES (?, 'synthbucket', ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (ju, f"cmid{n}", f"csender{n}", f"cdomain{n}", f"dlv{n}", blob, state, attempts,
             created, created, created),
        )  # fmt: skip
    # quarantine
    qroot = data / "quarantine"
    on_disk = 0
    items = [
        # (pid, sender, state, received, notified, decided, files, filed, write files)
        (PA, "sa", "pending", T_MONTH_PLUS, None, None, ONE, ZERO, False),  # expires
        (PA, "sb", "pending", T_HOUR, None, None, ONE, ZERO, False),  # notice
        (PA, "sb", "pending", T_MIN_FIFTY, None, None, ONE, ZERO, False),  # same notice
        (PB, "sc", "pending", T_MIN_THIRTY, None, None, ONE, ZERO, False),  # throttled
        (PB, "sc", "pending", T_MIN_FORTY, T_MIN_TWENTY, None, ONE, ZERO, False),
        (PA, "sd", "rejected", T_FORTY_DAYS, T_FORTY_DAYS, T_MONTH_PLUS, TWO, ZERO, True),  # purge
        (
            PA,
            "se",
            "expired",
            T_SEVENTY_DAYS,
            None,
            T_FORTY_DAYS,
            ONE,
            ZERO,
            False,
        ),  # purge, missing
        (PA, "sf", "approved", T_TWELVE_DAYS, T_TWELVE_DAYS, T_TEN_DAYS, ONE, ONE, True),
        (
            PA,
            "sg",
            "rejected",
            T_EIGHT_DAYS,
            T_EIGHT_DAYS,
            T_FIVE_DAYS,
            ONE,
            ZERO,
            True,
        ),  # in trash
    ]
    for n, (pid, sender, state, received, notified, decided, files, filed, write) in enumerate(
        items
    ):
        iu = _uid(300 + n)
        await c.execute(
            "INSERT INTO inbound_quarantine (patient_id, item_uuid, source_hash, sender_hash, "
            "sender_enc, files_enc, refusal_reason, file_count, total_bytes, state, "
            "received_at, notified_at, notice_key, decided_at, filed_count) "
            "VALUES (?, ?, ?, ?, 'x', 'x', 'unknown_sender', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pid,
                iu,
                f"src{n}",
                sender,
                files,
                100 * files,
                state,
                received,
                notified,
                f"key{n}" if notified else None,
                decided,
                filed,
            ),
        )
        if write:
            (qroot / iu).mkdir(parents=True)
            for i in range(files):
                blob = b"q1" + bytes(40 + 10 * i + n)
                (qroot / iu / f"{i}.bin").write_bytes(blob)
                if state == "rejected" and decided < ago(days=30):
                    on_disk += len(blob)
    await c.commit()
    await db.close()
    return {"bytes": on_disk}


@pytest.fixture
async def seeded(tmp_path, secrets_env):
    path = tmp_path / "synth999q.db"
    info = await _seed(path, secrets_env)
    return path, secrets_env, info


def _dry(path: Path, **kw) -> dict:
    return job_dryrun.run("inbound_maintenance", path, now=NOW, **kw)


@pytest.fixture
def mails(monkeypatch):
    sent: list[dict] = []

    async def _send(to, subject, text, **kw):
        sent.append({"to": list(to), **kw})
        return True

    monkeypatch.setattr("oncofiles.mailer.send_mail", _send)
    return sent


async def _one_real_pass(path: Path) -> dict:
    db = Database(str(path))
    await db.connect()
    try:
        return await inbound_loop.INBOUND.run_once(
            lambda: inbound_loop.inbound_iteration(db, asyncio.Semaphore(1))
        )
    finally:
        await db.close()


# ── the dry run equals one real pass ─────────────────────────────────────


async def test_dry_run_counts_match_one_real_pass(seeded, mails, monkeypatch):
    path, data, info = seeded

    async def no_network(*a, **kw):  # a verification would need the EML + DNS
        raise AssertionError("no verification expected on this fixture")

    from oncofiles import inbound_dkim

    monkeypatch.setattr(inbound_dkim, "verify_message", no_network)
    dry = _dry(path, data_dir=data)
    claimed, jobs, quar = dry["claimed"], dry["jobs"], dry["quarantine"]
    assert claimed["deferred_would_expire"] == 1 and claimed["deferred_would_promote"] == 1
    assert claimed["claimable"] == 3
    assert claimed["would_fail_max_attempts"] == claimed["would_fail_download_expired"] == 1
    assert jobs["claimable"] == 3 and jobs["by_state"] == {"pending": 3}
    assert jobs["would_fail_max_attempts"] == jobs["older_than_attachment_retention"] == 1
    assert quar["would_expire"] == 1
    notices = quar["notices"]
    assert notices["would_send"] == 1 and notices["items_announced"] == 2
    assert notices["patients_throttled"] == 1 and notices["items_waiting_for_digest"] == 1
    purge = quar["purge"]
    assert purge["items"] == 2 and purge["approved_items"] == 0
    assert purge["files"] == 2 and purge["files_missing"] == 1
    assert purge["refused"] == 0
    assert purge["bytes"] == info["bytes"]

    counts = await _one_real_pass(path)
    summary = inbound_loop.INBOUND.status()["last_summary"]
    assert summary["claimed_failed"] == claimed["claimable"]
    assert counts["claimed_failed_max_attempts"] == claimed["would_fail_max_attempts"]
    assert counts["claimed_failed_download_expired"] == claimed["would_fail_download_expired"]
    assert summary["jobs_processed"] == summary["jobs_failed"] == jobs["claimable"]
    assert counts["job_failed_max_attempts"] == jobs["would_fail_max_attempts"]
    assert counts["job_failed_download_expired"] == jobs["older_than_attachment_retention"]
    assert summary["quarantine_expired"] == quar["would_expire"]
    assert summary["notices_sent"] == notices["would_send"] == len(mails)
    assert summary["purged_items"] == purge["items"]
    assert summary["purged_bytes"] == purge["bytes"]
    assert counts["quarantine_files_removed"] == purge["files"]
    assert counts["quarantine_files_missing"] == purge["files_missing"]
    assert [m["to"] for m in mails] == [[CARER_A]]

    after = _dry(path, data_dir=data)  # the pass did its work
    assert after["claimed"]["claimable"] == 0 and after["jobs"]["claimable"] == 0
    assert after["quarantine"]["would_expire"] == 0
    assert after["quarantine"]["notices"]["would_send"] == 0
    assert after["quarantine"]["purge"]["items"] == 0


async def test_deferred_promotion_respects_the_caps_like_production(tmp_path):
    """Two open rows of one From domain fill its cap: the deferred row of that
    domain stays, the other one is promoted (dry run == the DB helper)."""
    path = tmp_path / "caps.db"
    db = Database(str(path))
    await db.connect()
    await db.migrate()
    rows = [
        ("pending", "cdomainA", ago(minutes=30)),
        ("processing", "cdomainA", ago(minutes=1)),
        ("deferred", "cdomainA", ago(minutes=20)),
        ("deferred", "cdomainB", ago(minutes=10)),
    ]
    for n, (state, domain, created) in enumerate(rows):
        await db.db.execute(
            "INSERT INTO inbound_claimed_jobs (job_uuid, bucket, message_id_hash, sender_hash, "
            "domain_hash, delivery_hash, payload_enc, state, next_attempt_at, created_at, "
            "updated_at) VALUES (?, 'synthbucket', ?, ?, ?, ?, 'x', ?, ?, ?, ?)",
            (_uid(400 + n), f"m{n}", f"s{n}", domain, f"d{n}", state, created, created, created),
        )
    await db.db.commit()
    await db.close()
    claimed = _dry(path)["claimed"]
    assert claimed["deferred_would_promote"] == 1
    assert claimed["deferred_held_by_cap"] == {"domain": 1}
    db = Database(str(path))
    await db.connect()
    try:
        moved = await db.promote_deferred_claimed(
            caps=inbound_queue._claimed_caps(),
            now=NOW,
            expired_before=ago(seconds=inbound_queue.BREVO_ATTACHMENT_RETENTION_S),
        )
    finally:
        await db.close()
    assert moved == {"promoted": 1, "expired": 0}


async def test_purge_approved_flag_mirrors_the_env(seeded, mails, monkeypatch):
    path, data, _info = seeded
    assert _dry(path, data_dir=data)["quarantine"]["purge"]["approved_items"] == 0
    dry = _dry(path, data_dir=data, purge_approved=True)["quarantine"]["purge"]
    assert dry["approved_switch"] is True and dry["approved_items"] == 1
    monkeypatch.setenv("QUARANTINE_PURGE_APPROVED", "true")
    counts = await _one_real_pass(path)
    assert counts["quarantine_approved_purged_items"] == dry["approved_items"]
    assert inbound_loop.INBOUND.status()["last_summary"]["purged_items"] == (
        dry["items"] + dry["approved_items"]
    )


async def test_without_data_dir_files_are_not_checked(seeded):
    path, _data, _info = seeded
    purge = _dry(path)["quarantine"]["purge"]
    assert purge["files_missing"] is None and purge["bytes"] is None and purge["refused"] is None
    assert purge["files"] == 3  # the rows' file counts (two items)
    assert _dry(path)["quarantine_dir"] == {"given": False, "exists": None}


async def test_dry_run_is_read_only_and_leaves_no_file(seeded):
    path, data, _info = seeded
    before = sorted(p.name for p in path.parent.iterdir())
    blob = path.read_bytes()
    tree = sorted(str(p) for p in data.rglob("*"))
    _dry(path, data_dir=data, purge_approved=True)
    assert sorted(p.name for p in path.parent.iterdir()) == before
    assert path.read_bytes() == blob
    assert sorted(str(p) for p in data.rglob("*")) == tree  # nothing removed
    conn = job_dryrun.open_snapshot(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE inbound_quarantine SET state = 'purged'")
    finally:
        conn.close()


async def test_dry_run_counts_only_never_ids_or_addresses(seeded):
    path, data, _info = seeded
    text = json.dumps(_dry(path, data_dir=data, purge_approved=True))
    for secret in (
        PA,
        PB,
        "example.invalid",
        "synthtokenhash1",
        "synthbucket",
        "csender",
        _uid(305),
        ".bin",
        str(data),
    ):
        assert secret not in text


def test_cli_offline_no_config_no_socket(seeded, tmp_path):
    """The CLI as infra runs it: no env, sockets blocked, no config import."""
    path, _data, _info = seeded
    absent = tmp_path / "absent-data"
    script = (
        "import json, socket, sys\n"
        "from oncofiles import job_dryrun\n"
        f"rc = job_dryrun.main(['inbound_maintenance', '--db', {str(path)!r}, "
        f"'--data-dir', {str(absent)!r}, '--now', {NOW!r}, '--purge-approved'])\n"
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
    assert report["job"] == "inbound_maintenance"
    assert report["quarantine_dir"] == {"given": True, "exists": False}
    assert report["quarantine"]["purge"]["approved_switch"] is True
    assert report["quarantine"]["purge"]["files"] == 0  # no files there
    assert report["quarantine"]["purge"]["files_missing"] == 4
    assert not absent.exists()  # never created


# ── a missing quarantine directory (production today) ───────────────────


async def test_missing_dir_dry_run_zero_and_not_created(tmp_path):
    empty = tmp_path / "empty.db"
    db = Database(str(empty))
    await db.connect()
    await db.migrate()
    await db.close()
    data = tmp_path / "data"
    data.mkdir()
    report = job_dryrun.run("inbound_maintenance", empty, data_dir=data, now=NOW)
    assert report["quarantine_dir"] == {"given": True, "exists": False}
    assert report["claimed"]["claimable"] == 0 and report["jobs"]["claimable"] == 0
    assert report["quarantine"]["would_expire"] == 0
    assert report["quarantine"]["notices"]["would_send"] == 0
    purge = report["quarantine"]["purge"]
    assert purge["items"] == purge["files"] == purge["bytes"] == purge["files_missing"] == 0
    assert list(data.iterdir()) == []


async def test_missing_dir_loop_iteration_zero_and_not_created(db, secrets_env):
    assert not inbound_quarantine.quarantine_dir().exists()
    counts = await inbound_loop.INBOUND.run_once(
        lambda: inbound_loop.inbound_iteration(db, asyncio.Semaphore(1))
    )
    assert counts == {"quarantine_expired": 0, "quarantine_notices_sent": 0}
    st = inbound_loop.INBOUND.status()
    assert st["last_ok"] and st["last_error"] is None
    assert st["last_summary"]["purged_items"] == 0
    assert not inbound_quarantine.quarantine_dir().exists()
    assert list(secrets_env.iterdir()) == []


async def test_without_jobs_secret_a_pass_touches_nothing(db, monkeypatch):
    monkeypatch.delenv("INBOUND_JOBS_SECRET", raising=False)
    sweep = AsyncMock()
    monkeypatch.setattr(inbound_quarantine, "run_maintenance", sweep)
    assert await inbound_queue.run_cycle(db) == Counter()
    sweep.assert_not_called()


# ── the loop ─────────────────────────────────────────────────────────────


class _Sleeps:
    def __init__(self, stop_after: int):
        self.calls: list[float] = []
        self.stop_after = stop_after

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) > self.stop_after:
            raise asyncio.CancelledError


async def test_loop_survives_an_exception_and_logs_the_class_only(db, monkeypatch, caplog):
    calls: list[int] = []

    async def cycle(db_):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("synthetic secret detail")
        return Counter({"job_done_filed": 2, "quarantine_notices_sent": 1})

    monkeypatch.setattr(inbound_queue, "run_cycle", cycle)
    sleeps = _Sleeps(stop_after=2)
    caplog.set_level("ERROR", logger="oncofiles.interval_job")
    with pytest.raises(asyncio.CancelledError):
        await inbound_loop.INBOUND.loop(
            lambda: inbound_loop.inbound_iteration(db, asyncio.Semaphore(1)),
            grace_s=300,
            sleep=sleeps,
        )
    interval = inbound_queue.WORKER_INTERVAL_MIN * 60
    assert sleeps.calls == [300, interval, interval]
    st = inbound_loop.INBOUND.status()
    assert st["runs"] == 2 and st["errors"] == 1
    assert st["last_error_type"] == "OperationalError"
    assert st["last_summary"]["jobs_done"] == 2 and st["last_summary"]["notices_sent"] == 1
    assert "inbound_queue: iteration failed (OperationalError)" in caplog.text
    assert "secret" not in caplog.text


async def test_iteration_holds_the_semaphore(db, monkeypatch):
    sem = asyncio.Semaphore(1)
    seen: dict = {}

    async def cycle(db_):
        seen["locked"] = sem.locked()
        return Counter(job_done_filed=1)

    monkeypatch.setattr(inbound_queue, "run_cycle", cycle)
    assert await inbound_loop.inbound_iteration(db, sem) == {"job_done_filed": 1}
    assert seen["locked"] is True


def test_interval_and_id_come_from_the_queue_module():
    assert inbound_loop.INBOUND.interval_s == inbound_queue.WORKER_INTERVAL_MIN * 60
    assert inbound_loop.JOB_ID == "inbound_queue"


async def test_run_cycle_keys_are_codes(db, monkeypatch):
    monkeypatch.setenv("INBOUND_JOBS_SECRET", JOBS_SECRET)

    async def fake_run(db_, *, limit):
        return Counter(
            {
                "claimed:done:verified": 1,
                "claimed:done:dkim_none": 2,
                "claimed:pending:dns_failed": 1,
                "done:filed": 3,
                "pending:error:OSError": 1,
                "failed:max_attempts": 1,
                "lease_lost": 1,
            }
        ), {
            "expired": 2,
            "notices": 1,
            "purge": {"purged_items": 1, "purged_bytes": 64},
        }

    monkeypatch.setattr(inbound_queue, "_run", fake_run)
    out = await inbound_queue.run_cycle(db)
    assert set(out) == {
        "claimed_done_verified",
        "claimed_done_dkim_none",
        "claimed_pending_dns_failed",
        "job_done_filed",
        "job_pending_error_OSError",
        "job_failed_max_attempts",
        "job_lease_lost",
        "quarantine_expired",
        "quarantine_notices_sent",
        "quarantine_purged_items",
        "quarantine_purged_bytes",
    }
    summary = inbound_loop.summarize(dict(out))
    assert summary == {
        "jobs_processed": 6,
        "jobs_done": 3,
        "jobs_failed": 1,
        "jobs_retried": 1,
        "claimed_verified": 1,
        "claimed_refused": 2,
        "claimed_failed": 0,
        "claimed_retried": 1,
        "quarantine_expired": 2,
        "notices_sent": 1,
        "purged_items": 1,
        "purged_bytes": 64,
    }


# ── lifespan: started whatever SYNC_ENABLED says, never twice ────────────


async def test_loop_starts_with_sync_disabled(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    async with server.lifespan(MagicMock()):
        assert scheduler_singleton.current_scheduler() is None
        assert scheduler_singleton.background_loop_running(inbound_loop.JOB_ID)
        assert inbound_loop.JOB_ID in scheduler_singleton.status()["background_loops"]
        task = scheduler_singleton._loops[inbound_loop.JOB_ID]
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert not scheduler_singleton.background_loop_running(inbound_loop.JOB_ID)


async def test_not_an_apscheduler_job_with_sync_enabled(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", True)
    monkeypatch.setattr(server, "AI_RUN_ON_STARTUP", False)
    async with server.lifespan(MagicMock()):
        sched, _ = scheduler_singleton.current_scheduler()
        assert inbound_loop.JOB_ID not in {j.id for j in sched.get_jobs()}
        async with server.lifespan(MagicMock()):  # a second lifespan reuses it
            assert scheduler_singleton._loop_refs[inbound_loop.JOB_ID] == 2
        assert scheduler_singleton.background_loop_running(inbound_loop.JOB_ID)
    await asyncio.sleep(0)
    assert not scheduler_singleton.background_loop_running(inbound_loop.JOB_ID)


async def test_loop_runs_in_its_local_only_zone(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    seen: dict = {}

    async def fake_loop(db, semaphore):
        seen["zone"] = (local_only_job(), semaphore)

    monkeypatch.setattr(inbound_loop, "inbound_queue_loop", fake_loop)
    async with server.lifespan(MagicMock()):
        await asyncio.sleep(0)
    assert seen["zone"] == ("inbound_queue", server._sync_semaphore)


def test_the_734_scan_discovers_the_loop():
    from tests.test_no_automatic_deletion import _job_registrations, _load_modules

    roots, ids = _job_registrations(_load_modules()["oncofiles.server"])
    assert "inbound_queue_loop" in roots
    assert "inbound_queue" not in ids  # no add_job registration left


# ── telemetry: operator /readiness + admin job list ──────────────────────


async def test_readiness_carries_loop_telemetry_counts_only(monkeypatch):
    import oncofiles.server as server
    from tests.test_one_scheduler_per_process import OPERATOR_TOKEN, _request

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)

    async def one():
        return Counter(
            {
                "job_done_filed": 2,
                "job_failed_download_expired": 1,
                "claimed_done_verified": 1,
                "claimed_done_dkim_none": 1,
                "quarantine_expired": 1,
                "quarantine_notices_sent": 1,
                "quarantine_purged_items": 1,
                "quarantine_purged_bytes": 4096,
            }
        )

    await inbound_loop.INBOUND.run_once(one)
    request, _ = _request(operator=True)
    body = json.loads((await server.readiness(request)).body)
    job = body["jobs"]["inbound_queue"]
    assert job["loop_running"] is False
    assert job["last_ok"] and job["last_error"] is None and job["runs"] == 1
    assert job["schedule"] == "every 2 min"
    s = job["last_summary"]
    assert s["jobs_processed"] == 3
    assert (s["jobs_done"], s["jobs_failed"]) == (2, 1)
    assert (s["claimed_verified"], s["claimed_refused"]) == (1, 1)
    assert (s["quarantine_expired"], s["notices_sent"]) == (1, 1)
    assert (s["purged_items"], s["purged_bytes"]) == (1, 4096)
    public, _ = _request(operator=False)
    assert "jobs" not in json.loads((await server.readiness(public)).body)


def test_admin_job_list_shows_the_loop():
    assert inbound_loop.JOB_ID in scheduled_jobs.BACKGROUND_LOOP_JOBS
    entry = scheduled_jobs.background_loop_entry(
        inbound_loop.JOB_ID,
        running=True,
        state=scheduled_jobs.background_loop_state(inbound_loop.JOB_ID),
    )
    assert entry["registered"] is True and entry["known"] is True
    assert entry["trigger"] == {
        "kind": "interval",
        "seconds": int(inbound_loop.INBOUND.interval_s),
        "text": "every 2 min",
    }
    off = scheduled_jobs.background_loop_entry(inbound_loop.JOB_ID, running=False, state={})
    assert off["reason"] == "loop_not_running"


def test_helper_shares_the_production_sql():
    """The dry run and the DB helpers run the same statements (no drift)."""
    from oncofiles import inbound_sql
    from oncofiles.database import _inbound_claimed, _inbound_jobs, _inbound_quarantine

    sources = {
        "_inbound_jobs": Path(_inbound_jobs.__file__).read_text(),
        "_inbound_claimed": Path(_inbound_claimed.__file__).read_text(),
        "_inbound_quarantine": Path(_inbound_quarantine.__file__).read_text(),
    }
    used = {
        "JOB_CLAIMABLE_WHERE": "_inbound_jobs",
        "CLAIMED_CLAIMABLE_WHERE": "_inbound_claimed",
        "CLAIMED_DEFERRED_EXPIRED_WHERE": "_inbound_claimed",
        "CLAIMED_DEFERRED_SQL": "_inbound_claimed",
        "claimed_checks": "_inbound_claimed",
        "claimed_count_sql": "_inbound_claimed",
        "QUARANTINE_EXPIRE_WHERE": "_inbound_quarantine",
        "QUARANTINE_PATIENTS_DUE_SQL": "_inbound_quarantine",
        "QUARANTINE_UNNOTIFIED_FROM": "_inbound_quarantine",
        "QUARANTINE_LAST_NOTICE_SQL": "_inbound_quarantine",
        "QUARANTINE_NOTICES_SINCE_SQL": "_inbound_quarantine",
        "QUARANTINE_PURGE_CANDIDATES_SQL": "_inbound_quarantine",
    }
    for name, module in used.items():
        assert getattr(inbound_sql, name)
        assert f"q.{name}" in sources[module], name
    dry = Path(job_dryrun.__file__).read_text()
    for name in used:
        assert f"q.{name}" in dry, name
    src = Path(inbound_quarantine.__file__).read_text()
    assert "inbound_sql.due_for_notice(" in src and "q.due_for_notice(" in dry
    assert "purge_plan(quarantine_dir()" in src and "iqr.purge_plan(" in dry
