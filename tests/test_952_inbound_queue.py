# ruff: noqa: F811 - the #950 fixtures are imported and used as test arguments
"""#952: the durable queue of the Brevo inbound route.

Covers: the enqueue before the 202 (an enqueue failure is a 500), a job that
survives a simulated restart and is filed exactly once, a stale ``processing``
job retried without a duplicate file or receipt, max attempts, the Brevo
download retention (``download_expired``), the encrypted payload (no
plaintext address, subject or download token), DB work only on the event
loop, the scheduler registration and the admin counts.

Synthetic only: invented addresses and file names, bytes, dates in 2027.
Network: attachment downloads are served from a dict (``brevo`` fixture).
"""

from __future__ import annotations

import ast
import base64
import inspect
import json
import threading
from collections import Counter
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from oncofiles import inbound_email, inbound_queue, server
from tests.test_950_inbound_address import (  # noqa: F401 - fixtures
    CARER,
    FILENAMES,
    PID,
    SUBJECT,
    _env,
    _request,
    address_of,
    brevo,
    brevo_item,
    brevo_request,
    drain,
    drive,
    idb,
    jpeg,
    mails,
    pdf,
)


@pytest.fixture
def clock(monkeypatch):
    """A movable clock for the worker (created_at stays the DB's real time)."""
    state = {"offset": timedelta(0)}
    real = inbound_queue._now

    def _now():
        return real() + state["offset"]

    monkeypatch.setattr(inbound_queue, "_now", _now)

    def advance(**kw):
        state["offset"] += timedelta(**kw)

    return advance


@pytest.fixture(autouse=True)
def inbox_listing(monkeypatch, drive):
    """The read-only inbox listing of a retried job: md5s of what the stub
    Drive holds (the stub records every file written into the inbox)."""
    import hashlib

    from oncofiles.curated import inbox_upload

    calls = []

    def _md5s(gdrive, root_id):
        calls.append(root_id)
        return {hashlib.md5(f[1], usedforsecurity=False).hexdigest() for f in drive.files}

    monkeypatch.setattr(inbox_upload, "inbox_md5s", _md5s)
    return calls


@pytest.fixture
def no_kick(monkeypatch):
    """The process 'dies' right after the 202: nothing is processed in it."""
    kicks = []
    monkeypatch.setattr(inbound_queue, "kick", lambda db: kicks.append(db))
    return kicks


async def _jobs(db):
    async with db.db.execute(
        "SELECT id, job_uuid, patient_id, state, attempts, reason, payload_enc,"
        " next_attempt_at, updated_at FROM inbound_jobs ORDER BY id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


def _two_file_item(addr, store):
    store["tok-a"] = pdf(1)
    store["tok-b"] = jpeg()
    return brevo_item(
        addr,
        files=[
            (FILENAMES[0], "application/pdf", len(store["tok-a"]), "tok-a"),
            (FILENAMES[1], "image/jpeg", len(store["tok-b"]), "tok-b"),
        ],
    )


def _restart():
    """What a process restart loses: every in-memory inbound state."""
    inbound_email.reset_state()
    inbound_queue._drain_task = None


# ── enqueue before 202 ──────────────────────────────────────────────────


async def test_job_is_written_before_the_202(idb, drive, mails, brevo, no_kick):
    store, fetched = brevo
    addr = await address_of(idb)
    resp = await server.inbound_brevo_webhook(
        brevo_request(idb, {"items": [_two_file_item(addr, store)]})
    )
    assert resp.status_code == 202
    [job] = await _jobs(idb)
    assert job["state"] == "pending" and job["patient_id"] == PID and job["attempts"] == 0
    assert drive.files == [] and fetched == [] and mails == []
    assert len(no_kick) == 1


async def test_enqueue_failure_is_500_so_brevo_retries(idb, drive, brevo, no_kick, monkeypatch):
    store, _ = brevo
    addr = await address_of(idb)

    async def broken(rows):
        raise RuntimeError("synthetic disk full")

    monkeypatch.setattr(idb, "insert_inbound_jobs", broken)
    resp = await server.inbound_brevo_webhook(
        brevo_request(idb, {"items": [_two_file_item(addr, store)]})
    )
    assert resp.status_code == 500 and no_kick == []
    assert await _jobs(idb) == []


async def test_unknown_token_is_not_queued(idb, brevo, no_kick):
    item = brevo_item(f"x-{'2' * 10}@{inbound_email.INBOUND_DOMAIN}")
    resp = await server.inbound_brevo_webhook(brevo_request(idb, {"items": [item]}))
    assert resp.status_code == 202 and await _jobs(idb) == []
    assert inbound_email._state.counters["unknown_token"] == 1
    assert inbound_email.bad_tokens_overview()["last_hour"] == 1  # counted once


async def test_enqueue_is_one_transaction(idb, brevo, monkeypatch):
    """A failure on the second row leaves no first row behind."""
    addr = await address_of(idb)
    items = [brevo_item(addr), brevo_item(addr, sender="other.synthetic@example.test")]
    msgs = [inbound_email.parse_brevo_item(i) for i in items]
    real = inbound_queue.encrypt_payload
    calls = {"n": 0}

    def flaky(payload, **kw):
        calls["n"] += 1
        return real(payload, **kw) if calls["n"] == 1 else None  # NOT NULL fails

    monkeypatch.setattr(inbound_queue, "encrypt_payload", flaky)
    with pytest.raises(Exception):  # noqa: B017 - any DB error
        await inbound_queue.enqueue(idb, msgs)
    assert await _jobs(idb) == []


# ── restart: processed exactly once ─────────────────────────────────────


async def test_job_survives_restart_and_is_filed_exactly_once(idb, drive, mails, brevo, no_kick):
    store, fetched = brevo
    addr = await address_of(idb)
    payload = {"items": [_two_file_item(addr, store)]}
    await server.inbound_brevo_webhook(brevo_request(idb, payload))
    _restart()

    counts = await inbound_queue.run_pending(idb)
    assert counts == {"done:filed": 1}
    assert [f[0] for f in drive.files] == list(FILENAMES[:2])
    assert len(mails) == 1 and mails[0]["to"] == [CARER]

    # a second run, and a Brevo retry of the same mail, change nothing
    assert await inbound_queue.run_pending(idb) == {}
    await server.inbound_brevo_webhook(brevo_request(idb, payload))
    assert await inbound_queue.run_pending(idb) == {}
    [job] = await _jobs(idb)
    assert job["state"] == "done" and job["reason"] == "filed" and job["attempts"] == 1
    assert len(drive.files) == 2 and fetched == ["tok-a", "tok-b"] and len(mails) == 1


async def test_route_kick_processes_in_the_background(idb, drive, mails, brevo):
    store, _ = brevo
    addr = await address_of(idb)
    resp = await server.inbound_brevo_webhook(
        brevo_request(idb, {"items": [_two_file_item(addr, store)]})
    )
    assert resp.status_code == 202
    await drain()
    assert len(drive.files) == 2
    [job] = await _jobs(idb)
    assert job["state"] == "done"


# ── a crash mid-processing ──────────────────────────────────────────────


class _Killed(BaseException):
    """A kill mid-processing (not an Exception: nothing catches it)."""


async def test_stale_processing_job_is_retried_without_duplicates(
    idb, drive, mails, brevo, no_kick, clock, monkeypatch
):
    store, fetched = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    real_file_one = inbound_email._file_one
    filed = {"n": 0}

    async def dies_after_first(db, pid, name, data):
        if filed["n"] == 1:
            raise _Killed()
        filed["n"] += 1
        return await real_file_one(db, pid, name, data)

    monkeypatch.setattr(inbound_email, "_file_one", dies_after_first)
    with pytest.raises(_Killed):
        await inbound_queue.run_pending(idb)
    _restart()
    monkeypatch.setattr(inbound_email, "_file_one", real_file_one)
    [job] = await _jobs(idb)
    assert job["state"] == "processing" and job["attempts"] == 1
    assert [f[0] for f in drive.files] == [FILENAMES[0]]

    # not stale yet: nobody takes it
    assert await inbound_queue.run_pending(idb) == {}
    clock(minutes=inbound_queue.STALE_PROCESSING_MIN + 1)
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1}

    assert [f[0] for f in drive.files] == list(FILENAMES[:2])  # each file once
    assert fetched == ["tok-a", "tok-b", "tok-b"]  # the filed one is not downloaded again
    expected = inbound_email.receipt_text(len(FILENAMES[:2]), Counter())[1]
    assert len(mails) == 1 and mails[0]["text"] == expected  # one receipt, both files
    [job] = await _jobs(idb)
    assert job["state"] == "done" and job["attempts"] == 2


async def test_max_attempts_ends_failed(idb, brevo, no_kick, clock, monkeypatch):
    addr = await address_of(idb)
    item = brevo_item(addr, files=[("S.pdf", "application/pdf", 10, "tok-a")])
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(item)])

    async def boom(db, msg, progress=None):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(inbound_email, "_process_message", boom)
    outcomes = []
    for _ in range(inbound_queue.MAX_ATTEMPTS + 2):
        outcomes.append(dict(await inbound_queue.run_pending(idb)))
        clock(minutes=60)
    [job] = await _jobs(idb)
    assert job["state"] == "failed" and job["reason"] == "max_attempts"
    assert job["attempts"] == inbound_queue.MAX_ATTEMPTS
    assert outcomes.count({"pending:error:RuntimeError": 1}) == inbound_queue.MAX_ATTEMPTS - 1
    assert {"failed:max_attempts": 1} in outcomes


async def test_retry_waits_for_its_backoff(idb, brevo, no_kick, clock, monkeypatch):
    addr = await address_of(idb)
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(brevo_item(addr))])

    async def boom(db, msg, progress=None):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(inbound_email, "_process_message", boom)
    assert await inbound_queue.run_pending(idb) == {"pending:error:RuntimeError": 1}
    assert await inbound_queue.run_pending(idb) == {}  # not due yet
    clock(minutes=inbound_queue.RETRY_BASE_MIN + 1)
    assert await inbound_queue.run_pending(idb) == {"pending:error:RuntimeError": 1}


async def test_crash_loop_on_a_stale_job_ends_failed(idb, brevo, no_kick, clock):
    addr = await address_of(idb)
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(brevo_item(addr))])
    await idb.db.execute(
        "UPDATE inbound_jobs SET state = 'processing', attempts = ?",
        (inbound_queue.MAX_ATTEMPTS,),
    )
    await idb.db.commit()
    clock(minutes=inbound_queue.STALE_PROCESSING_MIN + 1)
    assert await inbound_queue.run_pending(idb) == {"failed:max_attempts": 1}


# ── Brevo retention ─────────────────────────────────────────────────────


async def test_download_expired(idb, drive, mails, brevo, no_kick, clock, monkeypatch):
    store, fetched = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    clock(seconds=inbound_queue.BREVO_ATTACHMENT_RETENTION_S + 60)
    assert await inbound_queue.run_pending(idb) == {"failed:download_expired": 1}
    assert fetched == [] and drive.files == [] and mails == []  # no receipt either
    [job] = await _jobs(idb)
    assert job["state"] == "failed" and job["reason"] == "download_expired"

    # visible in the admin panel
    monkeypatch.setattr(server, "_require_admin", lambda request: None)
    app = MagicMock()
    app.state.fastmcp_server._lifespan_result = {"db": idb}
    req = _request(idb, b"", {})
    req.scope["app"] = app
    resp = await server.api_inbound_unmatched(req)
    body = json.loads(resp.body)
    assert body["jobs"]["failed"] == 1
    assert body["jobs"]["failed_reasons"] == {"download_expired": 1}
    assert body["totals"]["job_failed_download_expired"] == 1


def test_retention_finding_is_cited():
    src = inspect.getsource(inbound_queue)
    assert "https://developers.brevo.com/reference/get-inbound-email-attachment" in src
    assert "NOT documented" in src


# ── encryption ──────────────────────────────────────────────────────────


async def test_payload_is_encrypted_without_plaintext(idb, brevo, no_kick):
    store, _ = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    [job] = await _jobs(idb)
    blob = job["payload_enc"]
    raw = base64.b64decode(blob.removeprefix("v1:"))
    token = inbound_email.token_from_address(addr)
    for secret in (CARER, addr, token, SUBJECT, "tok-a", FILENAMES[0], "carer950"):
        assert secret not in blob and secret.encode() not in raw
    async with idb.db.execute("SELECT * FROM inbound_jobs") as cur:
        row = dict(await cur.fetchone())
    for value in row.values():
        assert CARER not in str(value) and SUBJECT not in str(value)
        assert token not in str(value)

    th = inbound_email.token_hash(token)
    payload = inbound_queue.decrypt_payload(
        blob, patient_id=PID, token_hash=th, job_uuid=job["job_uuid"]
    )
    assert payload["sender"] == CARER and "Subject" not in json.dumps(payload)
    assert SUBJECT not in json.dumps(payload) and "never stored" not in json.dumps(payload)
    with pytest.raises(inbound_queue.UndecryptablePayloadError):  # bound to its row
        inbound_queue.decrypt_payload(blob, patient_id="other", token_hash="x", job_uuid="y")


async def test_undecryptable_payload_ends_failed(idb, brevo, no_kick, monkeypatch):
    addr = await address_of(idb)
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(brevo_item(addr))])
    monkeypatch.setenv("INBOUND_JOBS_SECRET", "a-rotated-synthetic-jobs-secret")
    assert await inbound_queue.run_pending(idb) == {"failed:undecryptable": 1}


def test_key_is_purpose_bound_and_never_from_the_bearer(monkeypatch):
    from oncofiles import secrets_keys

    key = secrets_keys.inbound_jobs_key()
    assert len(key) == 32
    assert key not in (secrets_keys.inbound_address_key(), secrets_keys.dashboard_session_key())
    others = {secrets_keys.inbound_message_id_hash_key(), secrets_keys.inbound_sender_hash_key()}
    assert key not in others and len(others) == 2
    # the bearer does not influence it
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "another-synthetic-bearer")
    assert secrets_keys.inbound_jobs_key() == key
    monkeypatch.setenv("INBOUND_JOBS_SECRET", "synthetic-other-secret")
    assert secrets_keys.inbound_jobs_key() != key and len(secrets_keys.inbound_jobs_key()) == 32
    # unset: fails closed, no derivation from MCP_BEARER_TOKEN
    monkeypatch.delenv("INBOUND_JOBS_SECRET")
    with pytest.raises(secrets_keys.InboundJobsSecretMissingError):
        secrets_keys.inbound_jobs_key()
    with pytest.raises(secrets_keys.InboundJobsSecretMissingError):
        secrets_keys.inbound_sender_hash_key()


# ── INBOUND_JOBS_SECRET is mandatory ────────────────────────────────────


async def test_secret_unset_answers_503_and_queues_nothing(
    idb, drive, brevo, no_kick, monkeypatch, caplog
):
    store, fetched = brevo
    addr = await address_of(idb)
    monkeypatch.delenv("INBOUND_JOBS_SECRET")
    monkeypatch.setattr(inbound_queue, "_warned_unconfigured", False)
    caplog.set_level("WARNING")
    resp = await server.inbound_brevo_webhook(
        brevo_request(idb, {"items": [_two_file_item(addr, store)]})
    )
    assert resp.status_code == 503 and resp.headers.get("retry-after")
    assert await _jobs(idb) == [] and no_kick == [] and fetched == []
    assert sum("INBOUND_JOBS_SECRET" in r.getMessage() for r in caplog.records) == 1


async def test_worker_leaves_jobs_untouched_without_the_secret(
    idb, drive, brevo, no_kick, monkeypatch, caplog
):
    store, fetched = brevo
    addr = await address_of(idb)
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(_two_file_item(addr, store))])
    before = await _jobs(idb)
    monkeypatch.delenv("INBOUND_JOBS_SECRET")
    monkeypatch.setattr(inbound_queue, "_warned_unconfigured", False)
    caplog.set_level("WARNING")
    assert await inbound_queue.run_pending(idb) == {}
    assert await inbound_queue.run_pending(idb) == {}
    assert await _jobs(idb) == before and drive.files == [] and fetched == []
    assert sum("INBOUND_JOBS_SECRET" in r.getMessage() for r in caplog.records) == 1


# ── dedupe bound to the sender (security review) ────────────────────────


async def test_forged_mail_with_a_stolen_message_id_does_not_block_the_real_one(
    idb, drive, mails, brevo, no_kick
):
    store, _ = brevo
    addr = await address_of(idb)
    mid = "<synthetic-real-2027@example.test>"
    real = _two_file_item(addr, store)
    real["MessageId"] = mid
    stranger = brevo_item(addr, sender="stranger952@example.test", files=[])
    stranger["MessageId"] = mid
    forged = brevo_item(addr, auth="fail", files=[])  # caregiver From, failed verdict
    forged["MessageId"] = mid
    for item in (stranger, forged, real):
        resp = await server.inbound_brevo_webhook(brevo_request(idb, {"items": [item]}))
        assert resp.status_code == 202
    assert len(await _jobs(idb)) == 3

    # a genuine Brevo retry of the real mail is still deduplicated
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [real]}))
    assert len(await _jobs(idb)) == 3

    counts = await inbound_queue.run_pending(idb)
    assert counts == {"done:unmatched": 2, "done:filed": 1}
    assert [f[0] for f in drive.files] == list(FILENAMES[:2])
    assert len(mails) == 1 and mails[0]["to"] == [CARER]


# ── the duplicate window after a Drive write (infra review) ─────────────


async def test_kill_after_the_drive_write_before_the_save_leaves_no_second_copy(
    idb, drive, mails, brevo, no_kick, clock, inbox_listing, monkeypatch
):
    store, fetched = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    real_update = idb.update_inbound_job_payload

    async def killed_on_save(*a, **kw):
        raise _Killed()  # Drive has the first file, its progress is never saved

    monkeypatch.setattr(idb, "update_inbound_job_payload", killed_on_save)
    with pytest.raises(_Killed):
        await inbound_queue.run_pending(idb)
    assert [f[0] for f in drive.files] == [FILENAMES[0]]
    _restart()
    monkeypatch.setattr(idb, "update_inbound_job_payload", real_update)

    clock(minutes=inbound_queue.STALE_PROCESSING_MIN + 1)
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1}
    names = [f[0] for f in drive.files]
    assert names == list(FILENAMES[:2])  # no second copy (no "-2" suffix) in the inbox
    assert inbox_listing  # the retry looked at the inbox, read-only
    expected = inbound_email.receipt_text(2, Counter())[1]
    assert len(mails) == 1 and mails[0]["text"] == expected
    [job] = await _jobs(idb)
    assert job["state"] == "done" and job["attempts"] == 2


async def test_retry_counts_a_file_the_pipeline_already_filed(
    idb, drive, mails, brevo, no_kick, clock, monkeypatch
):
    """The inbox file was moved into the curated tree before the retry: the
    curated mirror row's source_md5 recognises it."""
    import hashlib

    store, _ = brevo
    addr = await address_of(idb)
    item = brevo_item(addr, files=[(FILENAMES[0], "application/pdf", 100, "tok-a")])
    store["tok-a"] = pdf(7)
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(item)])
    await idb.db.execute("UPDATE inbound_jobs SET attempts = 1")  # an earlier attempt
    await idb.db.commit()
    md5 = hashlib.md5(store["tok-a"], usedforsecurity=False).hexdigest()

    mirror_row = {"id": PID.count("-"), "deleted": False}
    mirror_row["source_md5"] = md5  # the mirror row's Drive checksum

    async def state(*, patient_id):
        return {"gdrive-synthetic": mirror_row}

    monkeypatch.setattr(idb, "curated_index_state", state)
    clock(minutes=inbound_queue.RETRY_BASE_MIN + 1)  # past the retry backoff
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1}
    assert drive.files == []  # recognised, not uploaded again
    assert inbound_email._state.counters["retry_already_stored"] == 1


async def test_failed_inbox_listing_fails_the_attempt(
    idb, drive, brevo, no_kick, clock, monkeypatch
):
    from oncofiles.curated import inbox_upload

    store, _ = brevo
    addr = await address_of(idb)
    store["tok-a"] = pdf(8)
    item = brevo_item(addr, files=[(FILENAMES[0], "application/pdf", 100, "tok-a")])
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(item)])
    await idb.db.execute("UPDATE inbound_jobs SET attempts = 1")
    await idb.db.commit()

    def broken(gdrive, root_id):
        raise OSError("synthetic listing failure")

    monkeypatch.setattr(inbox_upload, "inbox_md5s", broken)
    clock(minutes=inbound_queue.RETRY_BASE_MIN + 1)
    assert await inbound_queue.run_pending(idb) == {"pending:error:OSError": 1}
    assert drive.files == []


# ── operator /readiness ─────────────────────────────────────────────────


async def test_readiness_shows_the_backlog_for_the_operator(
    idb, brevo, no_kick, clock, monkeypatch
):
    addr = await address_of(idb)
    store = brevo[0]
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(_two_file_item(addr, store))])
    clock(seconds=inbound_queue.BREVO_ATTACHMENT_RETENTION_S + 60)
    await inbound_queue.run_pending(idb)
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(brevo_item(addr))])
    monkeypatch.setattr(server, "_readiness_caller_is_operator", lambda request: True)
    app = MagicMock()
    app.state.fastmcp_server._lifespan_result = {"db": idb}
    req = _request(idb, b"", {})
    req.scope["app"] = app
    body = json.loads((await server.readiness(req)).body)
    jobs = body["inbound_jobs"]
    assert "done" not in jobs and jobs["pending"] == 1 and jobs["failed"] == 1
    assert jobs["pending_reasons"] == {"new": 1}
    assert jobs["failed_reasons"] == {"download_expired": 1}
    text = json.dumps(body)
    assert CARER not in text and "@" not in json.dumps(jobs)


# ── DB only on the event loop ───────────────────────────────────────────


async def test_worker_never_runs_db_work_in_a_thread(idb, drive, mails, brevo, no_kick):
    store, _ = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    loop_thread = threading.get_ident()
    seen: list[int] = []
    conn = idb.db
    real_execute, real_commit = conn.execute, conn.commit

    def execute(*a, **kw):
        seen.append(threading.get_ident())
        return real_execute(*a, **kw)

    async def commit():
        seen.append(threading.get_ident())
        return await real_commit()

    conn.execute, conn.commit = execute, commit
    try:
        assert await inbound_queue.run_pending(idb) == {"done:filed": 1}
    finally:
        conn.execute, conn.commit = real_execute, real_commit
    assert seen and set(seen) == {loop_thread}
    assert len(drive.files) == 2


def test_queue_module_has_no_thread_offloading():
    tree = ast.parse(inspect.getsource(inbound_queue))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"to_thread", "run_in_executor", "Thread", "gather"}


# ── scheduler + retention locks ─────────────────────────────────────────


def test_worker_is_a_job_of_the_one_scheduler():
    src = inspect.getsource(server._start_sync_scheduler)
    assert 'local_only_task("inbound_queue", _run_inbound_queue)' in src
    body = src[src.index("async def _run_inbound_queue") :]
    body = body[: body.index("scheduler.add_job")]
    assert "async with _sync_semaphore" in body and "run_pending(db)" in body
    # first run at the startup grace end: _defer_jobs_past_startup_grace runs after
    assert src.index('id="inbound_queue"') < src.index("_defer_jobs_past_startup_grace(scheduler)")


def test_queue_never_deletes():
    from oncofiles.database import _inbound_jobs

    for mod in (inbound_queue, _inbound_jobs):
        code = inspect.getsource(mod)
        assert "DELETE FROM" not in code.replace("before any\nDELETE", "")
        assert "TODO" in code  # the 30-day done-row removal waits for a maintainer decision


def test_mime_route_still_processes_synchronously():
    src = inspect.getsource(server.inbound_email_webhook)
    assert "process_message" in src and "inbound_queue" not in src


# ── infra review 2: temporary failures never end done ───────────────────


@pytest.fixture
def flaky_brevo(brevo, monkeypatch):
    """Like ``brevo``, but ``fail[token] = n`` makes the next n downloads of
    that token a temporary failure (a Brevo 5xx / timeout)."""
    store, fetched = brevo
    fail: dict[str, int] = {}

    def _fetcher(token, api_key, limit):
        async def fetch():
            fetched.append(token)
            if fail.get(token, 0) > 0:
                fail[token] -= 1
                raise inbound_email.TemporaryFetchError("fetch_failed")
            data = store.get(token)
            return data if data is not None and len(data) <= limit else None

        return fetch

    monkeypatch.setattr(inbound_email, "_brevo_fetcher", _fetcher)
    return store, fetched, fail


async def test_temporary_fetch_failure_goes_back_to_pending(
    idb, drive, mails, flaky_brevo, no_kick, clock
):
    store, fetched, fail = flaky_brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    fail["tok-b"] = 1
    assert await inbound_queue.run_pending(idb) == {"pending:fetch_failed": 1}
    [job] = await _jobs(idb)
    assert job["state"] == "pending" and job["reason"] == "fetch_failed"
    assert job["next_attempt_at"] > job["updated_at"]  # the backoff
    assert [f[0] for f in drive.files] == [FILENAMES[0]] and mails == []  # no receipt yet

    assert await inbound_queue.run_pending(idb) == {}  # not due yet
    clock(minutes=inbound_queue.RETRY_BASE_MIN + 1)
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1}
    assert [f[0] for f in drive.files] == list(FILENAMES[:2])  # each once
    assert fetched == ["tok-a", "tok-b", "tok-b"]
    expected = inbound_email.receipt_text(2, Counter())[1]
    assert len(mails) == 1 and mails[0]["text"] == expected


async def test_store_failure_goes_back_to_pending(
    idb, drive, mails, brevo, no_kick, clock, monkeypatch
):
    store, _ = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    real_file_one = inbound_email._file_one
    calls = {"n": 0}

    async def drive_down_once(db, pid, name, data):
        calls["n"] += 1
        if calls["n"] == 2:
            return "store_failed"
        return await real_file_one(db, pid, name, data)

    monkeypatch.setattr(inbound_email, "_file_one", drive_down_once)
    assert await inbound_queue.run_pending(idb) == {"pending:store_failed": 1}
    assert [f[0] for f in drive.files] == [FILENAMES[0]] and mails == []
    clock(minutes=inbound_queue.RETRY_BASE_MIN + 1)
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1}
    assert [f[0] for f in drive.files] == list(FILENAMES[:2])
    both_filed = inbound_email.receipt_text(len(FILENAMES[:2]), Counter())[1]
    assert [m["text"] for m in mails] == [both_filed]


async def test_rate_limited_goes_back_to_pending_and_counts_once(
    idb, drive, mails, brevo, no_kick, clock, monkeypatch
):
    store, fetched = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    monkeypatch.setattr(inbound_email, "_rate_limited", lambda th, now: True)
    assert await inbound_queue.run_pending(idb) == {"pending:rate_limited": 1}
    assert fetched == [] and drive.files == [] and mails == []
    seen = []
    monkeypatch.setattr(inbound_email, "_rate_limited", lambda th, now: seen.append(1) or False)
    # no counters behind the stub: the wait is "until the next full hour"
    clock(minutes=61)
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1}
    assert len(drive.files) == 2 and len(seen) == 1


async def test_admitted_mail_is_not_rate_limited_again_on_retry(
    idb, drive, mails, flaky_brevo, no_kick, clock, monkeypatch
):
    store, _, fail = flaky_brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    fail["tok-a"] = 1
    assert await inbound_queue.run_pending(idb) == {"pending:fetch_failed": 1}
    # the quota is now exhausted: the admitted mail still finishes
    monkeypatch.setattr(inbound_email, "_rate_limited", lambda th, now: True)
    clock(minutes=inbound_queue.RETRY_BASE_MIN + 1)
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1}
    assert len(drive.files) == 2


async def test_missing_brevo_api_key_goes_back_to_pending(
    idb, drive, mails, brevo, no_kick, clock, monkeypatch
):
    store, fetched = brevo
    addr = await address_of(idb)
    monkeypatch.delenv(inbound_email.BREVO_API_KEY_ENV)
    resp = await server.inbound_brevo_webhook(
        brevo_request(idb, {"items": [_two_file_item(addr, store)]})
    )
    assert resp.status_code == 202
    assert await inbound_queue.run_pending(idb) == {"pending:no_api_key": 1}
    assert fetched == [] and drive.files == [] and mails == []
    monkeypatch.setenv(inbound_email.BREVO_API_KEY_ENV, "synthetic-brevo-key-952")
    clock(minutes=inbound_queue.RETRY_BASE_MIN + 1)
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1}
    assert len(drive.files) == 2 and len(mails) == 1


async def test_temporary_failure_ends_failed_with_its_code_after_max_attempts(
    idb, drive, mails, flaky_brevo, no_kick, clock
):
    store, _, fail = flaky_brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    fail["tok-b"] = 99
    outcomes = []
    for _ in range(inbound_queue.MAX_ATTEMPTS + 1):
        outcomes.append(dict(await inbound_queue.run_pending(idb)))
        clock(minutes=60)
    assert outcomes.count({"pending:fetch_failed": 1}) == inbound_queue.MAX_ATTEMPTS - 1
    assert {"failed:fetch_failed": 1} in outcomes
    [job] = await _jobs(idb)
    assert job["state"] == "failed" and job["reason"] == "fetch_failed"
    assert job["attempts"] == inbound_queue.MAX_ATTEMPTS
    assert [f[0] for f in drive.files] == [FILENAMES[0]]  # filed once
    # the last attempt tells the caregiver: one accepted, one not downloaded
    filed_n, missing_n = len(drive.files), len(drive.files)
    one_missing = inbound_email.receipt_text(filed_n, Counter(fetch_failed=missing_n))[1]
    assert [m["text"] for m in mails] == [one_missing]


async def test_permanent_drops_still_end_done(idb, drive, mails, brevo, no_kick):
    addr = await address_of(idb)
    # a download token Brevo does not know (404): permanent, reported
    item = brevo_item(addr, files=[(FILENAMES[0], "application/pdf", 100, "tok-gone")])
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(item)])
    stranger = brevo_item(addr, sender="stranger952@example.test")
    stranger["MessageId"] = "<synthetic-stranger-2027@example.test>"
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(stranger)])
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1, "done:unmatched": 1}
    assert drive.files == []


class _FakeResponse:
    def __init__(self, status, body=b""):
        self.status_code = status
        self._body = body

    async def aiter_bytes(self):
        yield self._body


class _FakeClient:
    """Stands in for httpx.AsyncClient (no network): ``plan`` is a status code
    or an exception class for the next GET."""

    plan: object = 200

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None):
        plan = _FakeClient.plan

        class _Ctx:
            async def __aenter__(self):
                if isinstance(plan, type) and issubclass(plan, BaseException):
                    raise plan("synthetic")
                return _FakeResponse(plan, b"%PDF-1.7 synthetic")

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.parametrize(
    ("plan", "expect"),
    [
        (200, "bytes"),
        (404, None),
        (410, None),
        (500, "temporary"),
        (503, "temporary"),
        (429, "temporary"),
        (401, "temporary"),
        ("timeout", "temporary"),
        ("connect", "temporary"),
    ],
)
async def test_real_fetcher_separates_temporary_from_permanent(monkeypatch, plan, expect):
    import httpx

    errors = {"timeout": httpx.ReadTimeout, "connect": httpx.ConnectError}
    _FakeClient.plan = errors.get(plan, plan)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    fetch = inbound_email._brevo_fetcher("tok-synthetic", "synthetic-key", 10_000)
    if expect == "temporary":
        with pytest.raises(inbound_email.TemporaryFetchError) as exc:
            await fetch()
        assert exc.value.code == "fetch_failed"
    elif expect is None:
        assert await fetch() is None
    else:
        assert await fetch() == b"%PDF-1.7 synthetic"


def test_temporary_codes_are_the_reviewed_four():
    assert {
        "rate_limited",
        "fetch_failed",
        "store_failed",
        "no_api_key",
    } == inbound_email.TEMPORARY_CODES


# ── infra review 3: queue caps at enqueue ───────────────────────────────


def _distinct_item(addr, n):
    item = brevo_item(addr, files=[])
    item["MessageId"] = f"<synthetic-cap-{n}-2027@example.test>"
    return item


async def test_per_address_cap_answers_503_and_writes_nothing(idb, brevo, no_kick, monkeypatch):
    addr = await address_of(idb)
    monkeypatch.setattr(inbound_queue, "MAX_OPEN_JOBS_PER_TOKEN", 2)
    for n in range(2):
        resp = await server.inbound_brevo_webhook(
            brevo_request(idb, {"items": [_distinct_item(addr, n)]})
        )
        assert resp.status_code == 202
    third = brevo_request(idb, {"items": [_distinct_item(addr, 2)]})
    resp = await server.inbound_brevo_webhook(third)
    assert resp.status_code == 503 and resp.headers.get("retry-after")
    assert len(await _jobs(idb)) == 2 and len(no_kick) == 2
    assert inbound_email._state.counters["jobs_queue_full"] == 1
    # a genuine Brevo retry of a queued mail is still a no-op 202, not a 503
    retry = brevo_request(idb, {"items": [_distinct_item(addr, 0)]})
    resp = await server.inbound_brevo_webhook(retry)
    assert resp.status_code == 202 and len(await _jobs(idb)) == 2


async def test_a_batch_over_the_cap_is_refused_whole(idb, brevo, no_kick, monkeypatch):
    addr = await address_of(idb)
    monkeypatch.setattr(inbound_queue, "MAX_OPEN_JOBS_PER_TOKEN", 2)
    items = [_distinct_item(addr, n) for n in range(3)]
    resp = await server.inbound_brevo_webhook(brevo_request(idb, {"items": items}))
    assert resp.status_code == 503 and await _jobs(idb) == []


async def test_total_cap_refuses_the_enqueue(idb, brevo, no_kick, monkeypatch):
    addr = await address_of(idb)
    monkeypatch.setattr(inbound_queue, "MAX_OPEN_JOBS_TOTAL", 1)
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(_distinct_item(addr, 0))])
    with pytest.raises(inbound_queue.InboundQueueFullError):
        await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(_distinct_item(addr, 1))])
    assert len(await _jobs(idb)) == 1


async def test_finished_jobs_do_not_count_against_the_cap(
    idb, drive, mails, brevo, no_kick, monkeypatch
):
    addr = await address_of(idb)
    monkeypatch.setattr(inbound_queue, "MAX_OPEN_JOBS_PER_TOKEN", 1)
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(_distinct_item(addr, 0))])
    await inbound_queue.run_pending(idb)
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(_distinct_item(addr, 1))])
    assert [j["state"] for j in await _jobs(idb)] == ["done", "pending"]


def test_payload_keeps_at_most_max_files_per_mail():
    n = inbound_email.MAX_FILES_PER_MAIL + 3
    msg = inbound_email.InboundMessage(recipients=[], sender=CARER, auth_pass=True)
    msg.attachments = [
        inbound_email.InboundAttachment(
            name=f"S{i}.pdf", data=b"", content_type="application/pdf", ref=f"tok-{i}"
        )
        for i in range(n)
    ]
    payload = inbound_queue.to_payload(msg, "synthetictoken")
    assert len(payload["attachments"]) == inbound_email.MAX_FILES_PER_MAIL
    assert payload["parse_skips"] == {"too_many_files": 3}


# ── infra review 4: AAD includes a random job uuid ──────────────────────


async def test_payload_copied_to_another_row_of_the_same_address_does_not_decrypt(
    idb, drive, mails, brevo, no_kick
):
    addr = await address_of(idb)
    for n in range(2):
        await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(_distinct_item(addr, n))])
    a, b = await _jobs(idb)
    assert a["job_uuid"] != b["job_uuid"] and len(a["job_uuid"]) == 36
    await idb.db.execute(
        "UPDATE inbound_jobs SET payload_enc = ? WHERE id = ?", (a["payload_enc"], b["id"])
    )
    await idb.db.commit()
    counts = await inbound_queue.run_pending(idb)
    assert counts == {"done:filed": 1, "failed:undecryptable": 1}


def test_aad_is_patient_token_hash_and_job_uuid():
    assert inbound_queue._aad("p", "t", "u") == b"p|t|u"
    with pytest.raises(ValueError):
        inbound_queue._aad("p", "t", "")


# ── infra review 5: lease ───────────────────────────────────────────────


async def test_finish_needs_the_lease(idb, brevo, no_kick):
    addr = await address_of(idb)
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(_distinct_item(addr, 0))])
    [job] = await _jobs(idb)
    assert await idb.claim_inbound_job(job, now="2027-03-01T10:00:00Z")
    wrong = await idb.finish_inbound_job(
        job["id"],
        patient_id=PID,
        state="done",
        reason="filed",
        now="2027-03-01T10:00:05Z",
        lease="2027-03-01T09:59:59Z",
    )
    assert wrong is False
    [after] = await _jobs(idb)
    assert after["state"] == "processing" and after["reason"] is None
    right = await idb.finish_inbound_job(
        job["id"],
        patient_id=PID,
        state="done",
        reason="filed",
        now="2027-03-01T10:00:05Z",
        lease="2027-03-01T10:00:00Z",
    )
    assert right is True and (await _jobs(idb))[0]["state"] == "done"
    # a finished job is never finished again, even with its last updated_at
    again = await idb.finish_inbound_job(
        job["id"],
        patient_id=PID,
        state="failed",
        reason="synthetic",
        now="2027-03-01T10:00:09Z",
        lease="2027-03-01T10:00:05Z",
    )
    assert again is False and (await _jobs(idb))[0]["state"] == "done"


async def test_lease_is_refreshed_before_each_download(
    idb, drive, mails, brevo, no_kick, monkeypatch
):
    store, _ = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    touches = []
    real_touch = idb.touch_inbound_job

    async def spy(*a, **kw):
        touches.append(kw["lease"])
        return await real_touch(*a, **kw)

    monkeypatch.setattr(idb, "touch_inbound_job", spy)
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1}
    assert len(touches) == 2  # one per attachment download


async def test_a_taken_over_job_is_left_to_its_new_owner(
    idb, drive, mails, brevo, no_kick, monkeypatch
):
    """The job was taken over as stale while this worker downloaded: the next
    lease check stops it, no file is filed and nothing is finished."""
    store, fetched = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    real_fetcher = inbound_email._brevo_fetcher

    def _fetcher(token, api_key, limit):
        inner = real_fetcher(token, api_key, limit)

        async def fetch():
            data = await inner()
            if token == "tok-a":  # another container claims the job meanwhile
                await idb.db.execute("UPDATE inbound_jobs SET updated_at = '2098-07-23T11:17:43Z'")
                await idb.db.commit()
            return data

        return fetch

    monkeypatch.setattr(inbound_email, "_brevo_fetcher", _fetcher)
    assert await inbound_queue.run_pending(idb) == {"lease_lost": 1}
    [job] = await _jobs(idb)
    assert job["state"] == "processing" and job["updated_at"] == "2098-07-23T11:17:43Z"
    assert drive.files == [] and mails == [] and fetched == ["tok-a"]
    assert inbound_email._state.counters["jobs_lease_lost"] == 1


# ── infra review 6: no Message-ID, candidate order ──────────────────────


async def test_mail_without_message_id_is_deduplicated(idb, brevo, no_kick):
    store, _ = brevo
    addr = await address_of(idb)
    item = _two_file_item(addr, store)
    item["MessageId"] = None
    for _ in range(2):  # Brevo delivers the same mail twice
        resp = await server.inbound_brevo_webhook(brevo_request(idb, {"items": [item]}))
        assert resp.status_code == 202
    async with idb.db.execute("SELECT message_id_hash FROM inbound_jobs") as cur:
        rows = [r[0] for r in await cur.fetchall()]
    assert len(rows) == 1 and rows[0] and all(c in "0123456789abcdef" for c in rows[0])

    other = brevo_item(addr, files=[(FILENAMES[2], "application/pdf", 10, "tok-c")])
    other["MessageId"] = None
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [other]}))
    assert len(await _jobs(idb)) == 2  # a different mail is its own job


def test_message_id_hash_never_returns_none():
    payload = {"v": 1, "token": "t", "attachments": [], "progress": {"filed": ["x"]}}
    h = inbound_queue.message_id_hash("", payload)
    assert h and h != inbound_queue.message_id_hash("<a@example.test>", payload)
    # the progress does not change the dedupe key
    assert h == inbound_queue.message_id_hash("", {**payload, "progress": {}})


async def test_due_jobs_come_first_by_next_attempt_at(idb, drive, mails, brevo, no_kick):
    addr = await address_of(idb)
    for n in range(3):
        await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(_distinct_item(addr, n))])
    first, second, third = await _jobs(idb)
    # the two oldest jobs back off far into the future
    await idb.db.execute(
        "UPDATE inbound_jobs SET next_attempt_at = '2098-07-23T11:17:43Z' WHERE id IN (?, ?)",
        (first["id"], second["id"]),
    )
    await idb.db.commit()
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1}
    states = {j["id"]: j["state"] for j in await _jobs(idb)}
    assert states == {first["id"]: "pending", second["id"]: "pending", third["id"]: "done"}


def test_candidate_query_orders_by_next_attempt_at():
    from oncofiles.database import _inbound_jobs

    src = inspect.getsource(_inbound_jobs.InboundJobsMixin.next_inbound_job)
    assert "next_attempt_at <= ?" in src and "ORDER BY next_attempt_at" in src
    assert "ORDER BY id LIMIT" not in src


# ── security review: untrusted flood never blocks the caregiver ─────────


def _forged_item(addr, n, *, sender="stranger952@example.test", auth="pass"):
    item = brevo_item(addr, sender=sender, auth=auth, files=[])
    item["MessageId"] = f"<synthetic-flood-{n}-2027@example.test>"
    return item


async def test_untrusted_flood_does_not_block_the_caregiver(
    idb, drive, mails, brevo, no_kick, monkeypatch
):
    store, _ = brevo
    addr = await address_of(idb)
    # a stranger, and the caregiver's From with a failed verdict, both untrusted
    flood = [_forged_item(addr, n) for n in range(inbound_queue.MAX_OPEN_JOBS_PER_TOKEN + 5)]
    flood += [_forged_item(addr, 900 + n, sender=CARER, auth="fail") for n in range(5)]
    for item in flood:
        resp = await server.inbound_brevo_webhook(brevo_request(idb, {"items": [item]}))
        assert resp.status_code == 202  # never 503 for the address
    jobs = await _jobs(idb)
    assert len(jobs) == inbound_queue.MAX_UNTRUSTED_JOBS_PER_TOKEN
    dropped = len(flood) - inbound_queue.MAX_UNTRUSTED_JOBS_PER_TOKEN
    assert inbound_email._state.counters["jobs_untrusted_dropped"] == dropped
    assert inbound_email._state.counters["jobs_queue_full"] == 0

    real = _two_file_item(addr, store)
    resp = await server.inbound_brevo_webhook(brevo_request(idb, {"items": [real]}))
    assert resp.status_code == 202
    async with idb.db.execute("SELECT trusted, COUNT(*) FROM inbound_jobs GROUP BY trusted") as cur:
        by_class = {r[0]: r[1] for r in await cur.fetchall()}
    assert by_class[False] == inbound_queue.MAX_UNTRUSTED_JOBS_PER_TOKEN
    assert by_class[True] == len([real])

    counts = await inbound_queue.run_pending(idb)
    assert counts["done:filed"] == 1
    assert [f[0] for f in drive.files] == list(FILENAMES[:2])
    assert [m["to"] for m in mails] == [[CARER]]


async def test_untrusted_total_cap_drops_and_trusted_still_queues(idb, brevo, no_kick, monkeypatch):
    addr = await address_of(idb)
    monkeypatch.setattr(inbound_queue, "MAX_UNTRUSTED_JOBS_TOTAL", 2)
    out = await inbound_queue.enqueue(
        idb, [inbound_email.parse_brevo_item(_forged_item(addr, n)) for n in range(4)]
    )
    assert out["queued"] == 2 and out["flood_dropped"] == 2
    out = await inbound_queue.enqueue(
        idb, [inbound_email.parse_brevo_item(_distinct_item(addr, 0))]
    )
    assert out["queued"] == 1 and out["flood_dropped"] == 0


async def test_trusted_flag_uses_the_cores_sender_rule(idb, brevo, no_kick, monkeypatch):
    addr = await address_of(idb)
    calls = []
    real = inbound_email.sender_refusal

    async def spy(db, pid, msg):
        calls.append(pid)
        return await real(db, pid, msg)

    monkeypatch.setattr(inbound_email, "sender_refusal", spy)
    await inbound_queue.enqueue(
        idb,
        [
            inbound_email.parse_brevo_item(_distinct_item(addr, 0)),
            inbound_email.parse_brevo_item(_forged_item(addr, 1)),
        ],
    )
    assert calls == [PID, PID]
    async with idb.db.execute("SELECT trusted FROM inbound_jobs ORDER BY id") as cur:
        flags = [bool(r[0]) for r in await cur.fetchall()]
    assert flags == [True, False]
    src = inspect.getsource(inbound_queue.enqueue)
    assert "sender_refusal" in src and "_email_matches_caregiver" not in src


async def test_queue_full_never_rolls_back_the_shared_connection(monkeypatch):
    """A cap refusal only reads: it must not roll back the one shared connection."""
    import inspect

    from oncofiles.database import _inbound_jobs

    src = inspect.getsource(_inbound_jobs)
    body = src[src.index("async def insert_inbound_jobs") :]
    body = body[: body.index("\n    async def ", 10)]
    assert body.index('raise InboundQueueFullError("trusted")') < body.index("\n        try:\n")
    assert body.count("rollback()") == 1


# ── #952 follow-up: rate_limited waits for the quota window ─────────────


@pytest.fixture
def wallclock(monkeypatch):
    """One movable clock for both sides: the queue's ``_now`` and the core's
    ``time.time`` (the filing-quota counters)."""
    import time as real_time
    from types import SimpleNamespace

    state = {"offset": 0.0}
    real_now = inbound_queue._now

    def _time():
        return real_time.time() + state["offset"]

    fake = SimpleNamespace(time=_time)
    monkeypatch.setattr(inbound_email, "time", fake)
    monkeypatch.setattr(inbound_queue, "time", fake)
    monkeypatch.setattr(
        inbound_queue, "_now", lambda: real_now() + timedelta(seconds=state["offset"])
    )

    def advance(**kw):
        state["offset"] += timedelta(**kw).total_seconds()

    advance.time = _time
    return advance


def _th(addr):
    return inbound_email.token_hash(inbound_email.token_from_address(addr))


def _wait_of(job):
    return inbound_queue._parse_iso(job["next_attempt_at"]) - inbound_queue._parse_iso(
        job["updated_at"]
    )


async def test_rate_limited_waits_for_the_window_without_using_attempts(
    idb, drive, mails, brevo, no_kick, wallclock
):
    store, fetched = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    # the hourly quota is full with mail sent 30 minutes ago: it frees in 30 min
    inbound_email._state.mails[_th(addr)] = [
        wallclock.time() - 1800
    ] * inbound_email.MAX_MAILS_PER_HOUR
    assert await inbound_queue.run_pending(idb) == {"pending:rate_limited": 1}
    [job] = await _jobs(idb)
    assert job["state"] == "pending" and job["reason"] == "rate_limited"
    assert job["attempts"] == 0  # a wait, not an attempt
    assert timedelta(minutes=29) < _wait_of(job) <= timedelta(minutes=31)
    assert fetched == [] and drive.files == [] and mails == []

    wallclock(minutes=inbound_queue.RETRY_BASE_MIN + 1)  # the normal backoff is not enough
    assert await inbound_queue.run_pending(idb) == {}
    wallclock(minutes=28)
    assert await inbound_queue.run_pending(idb) == {"done:filed": 1}
    [job] = await _jobs(idb)
    assert job["attempts"] == 1  # only the claim that filed counted
    assert [f[0] for f in drive.files] == list(FILENAMES[:2])
    assert len(mails) == 1  # one receipt


async def test_rate_limited_retries_never_reach_max_attempts(
    idb, drive, mails, brevo, no_kick, wallclock
):
    store, _ = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    for _ in range(inbound_queue.MAX_ATTEMPTS + 2):
        # the quota is full again at every wake-up (frees in 10 min each time)
        inbound_email._state.mails[_th(addr)] = [
            wallclock.time() - 3000
        ] * inbound_email.MAX_MAILS_PER_HOUR
        assert await inbound_queue.run_pending(idb) == {"pending:rate_limited": 1}
        wallclock(minutes=11)
    [job] = await _jobs(idb)
    assert job["state"] == "pending" and job["attempts"] == 0


async def test_daily_window_sets_the_wait(idb, brevo, no_kick, wallclock):
    addr = await address_of(idb)
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(_distinct_item(addr, 1))])
    th = _th(addr)
    now = wallclock.time()
    # the daily cap is full, the oldest mail 20 hours ago; the hour is not full
    inbound_email._state.mails[th] = [now - 20 * 3600] * inbound_email.MAX_MAILS_PER_DAY
    assert inbound_email.quota_frees_at(th, now) == (now + 4 * 3600 + 1, "window")
    assert await inbound_queue.run_pending(idb) == {"pending:rate_limited": 1}
    [job] = await _jobs(idb)
    assert timedelta(hours=3, minutes=59) < _wait_of(job) <= timedelta(hours=4, minutes=1)


def test_unknown_window_falls_back_to_the_next_full_hour():
    inbound_email.reset_state()
    now = 1_830_000_000.0 + 1234  # 2027, mid-hour
    at, basis = inbound_email.quota_frees_at("synthetic-th", now)
    assert basis == "next_hour" and at % 3600 == 0 and 0 < at - now <= 3600


async def test_rate_limited_ends_failed_after_48_hours(
    idb, drive, mails, brevo, no_kick, wallclock
):
    addr = await address_of(idb)
    # a mail without attachments: only the 48 h bound applies
    await inbound_queue.enqueue(idb, [inbound_email.parse_brevo_item(_distinct_item(addr, 2))])
    th = _th(addr)
    outcomes = []
    for _ in range(10):
        # every day the daily quota is full again (frees 24 h later)
        inbound_email._state.mails[th] = [wallclock.time()] * inbound_email.MAX_MAILS_PER_DAY
        outcomes.append(dict(await inbound_queue.run_pending(idb)))
        if outcomes[-1] == {"failed:rate_limited": 1}:
            break
        wallclock(hours=24, minutes=1)
    # day 1 waits; on day 2 the next window would pass the 48 h bound
    assert outcomes == [{"pending:rate_limited": 1}, {"failed:rate_limited": 1}]
    [job] = await _jobs(idb)
    assert job["state"] == "failed" and job["reason"] == "rate_limited"
    assert job["attempts"] == 1  # only the final claim counted
    assert drive.files == [] and mails == []


async def test_rate_limited_with_attachments_stops_at_the_download_retention(
    idb, drive, mails, brevo, no_kick, wallclock
):
    store, fetched = brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    wallclock(hours=2)
    # the daily quota frees only in 24 h: past the attachments' 24 h retention
    inbound_email._state.mails[_th(addr)] = [wallclock.time()] * inbound_email.MAX_MAILS_PER_DAY
    assert await inbound_queue.run_pending(idb) == {"failed:rate_limited": 1}
    assert fetched == [] and drive.files == []


async def test_other_temporary_codes_keep_the_normal_backoff(
    idb, drive, mails, flaky_brevo, no_kick, wallclock
):
    store, _, fail = flaky_brevo
    addr = await address_of(idb)
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [_two_file_item(addr, store)]}))
    fail["tok-b"] = 1
    assert await inbound_queue.run_pending(idb) == {"pending:fetch_failed": 1}
    [job] = await _jobs(idb)
    assert job["attempts"] == 1 and job["reason"] == "fetch_failed"  # counted
    assert _wait_of(job) == inbound_queue._backoff(1)
