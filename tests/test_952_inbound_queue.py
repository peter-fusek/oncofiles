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
        "SELECT id, patient_id, state, attempts, reason, payload_enc FROM inbound_jobs ORDER BY id"
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

    payload = inbound_queue.decrypt_payload(
        blob, patient_id=PID, token_hash=inbound_email.token_hash(token)
    )
    assert payload["sender"] == CARER and "Subject" not in json.dumps(payload)
    assert SUBJECT not in json.dumps(payload) and "never stored" not in json.dumps(payload)
    with pytest.raises(inbound_queue.UndecryptablePayloadError):  # bound to its row
        inbound_queue.decrypt_payload(blob, patient_id="other", token_hash="x")


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
