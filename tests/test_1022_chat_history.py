"""#1022: encrypted per-patient chat history with retention, export and erasure.

Covered:

* the storage API (``oncofiles.chat_history``): round trip, citations as ids +
  pages only, grounding counts only, model only on answers;
* isolation: a foreign patient or conversation is "not found" everywhere, and
  a conversation id of another patient is refused on write;
* encryption: AES-GCM with its own purpose-derived key, the associated data
  binds a body to patient + conversation + row (a body copied to another row
  never decrypts), the key fails closed (nothing stored, counter);
* retention (``oncofiles.chat_retention``): modes off / report / on with time
  travel, purge only after 30 days in the trash, restore inside the window
  (and the 90 days count from the restore), never a hard delete;
* the loop: started with SYNC_ENABLED=false, local-only zone, telemetry on
  operator /readiness and in the admin job list, the #734 scan reaches it;
* ``job_dryrun chat_history_retention`` counts what one real run does;
* data rights: the export holds the decrypted text, the erasure removes it;
* no message text in any log.

Synthetic only: invented ids, texts and 2027+ dates.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oncofiles import (
    chat_history,
    chat_retention,
    chat_retention_sql,
    data_rights,
    job_dryrun,
    patient_erasure,
    scheduled_jobs,
    scheduler_singleton,
    secrets_keys,
)
from oncofiles.cloud_guard import local_only_job
from oncofiles.database import Database
from tests.helpers import PRIMARY_TEST_UUID
from tests.test_856_data_rights import fake_copy, fake_revoke  # noqa: F401 - fixtures
from tests.test_856_erase_retention_loop import lifespan_env  # noqa: F401 - fixture

A = PRIMARY_TEST_UUID
B = "00000000-0000-4000-8000-000000001022"
B_SLUG = "chat1022-boris"
JOB_ID = "chat_history_retention"
QUESTION = "Synthetic question about the 2027 lab sheet?"
ANSWER = "Synthetic answer: value 7.3 on page 2 of document 41."
NOW = datetime.fromisoformat("2027-06-01T12:00:00+00:00")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(secrets_keys.CHAT_HISTORY_SECRET_ENV, "synthetic-chat-secret-1022")
    monkeypatch.delenv(chat_retention.MODE_ENV, raising=False)
    chat_history._reset_for_tests()
    chat_retention._reset_for_tests()
    scheduler_singleton._reset_for_tests()
    yield
    sched = scheduler_singleton._scheduler
    if scheduler_singleton._scheduler_alive(sched):
        sched.shutdown(wait=False)
    scheduler_singleton._reset_for_tests()
    chat_retention._reset_for_tests()


@pytest.fixture
async def two(db: Database) -> Database:
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (B, B_SLUG, "Boris Synthetic", "carer-1022@example.test"),
    )
    await db.db.commit()
    return db


def _conv() -> str:
    return str(uuid.uuid4())


async def _store(db, pid, conv, role="user", text=QUESTION, **kw):
    return await chat_history.store_message(
        db, patient_id=pid, conversation_id=conv, channel="dashboard", role=role, text=text, **kw
    )


async def _rows(db, where="1", params=()):
    async with db.db.execute(f"SELECT * FROM chat_messages WHERE {where}", params) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _seed_aged(db, pid, *, created: datetime, deleted: datetime | None = None) -> int:
    """One sealed row written at ``created`` (time travel), optionally trashed."""
    conv, row_uuid = _conv(), str(uuid.uuid4())
    row_id = await db.add_chat_message(
        patient_id=pid,
        conversation_id=conv,
        channel="dashboard",
        role="user",
        row_uuid=row_uuid,
        body_enc=chat_history.seal(QUESTION, pid, conv, row_uuid),
        citations_json=json.dumps([{"document_id": 41, "page": 2}]),
        model=None,
        grounding_json=json.dumps({"numbers_checked": 1}),
        caller_email_hash="synthhash",
        created_at=chat_retention_sql.ts(created),
    )
    if deleted is not None:
        await db.db.execute(
            "UPDATE chat_messages SET deleted_at = ?, deleted_by = 'retention' WHERE id = ?",
            (chat_retention_sql.ts(deleted), row_id),
        )
        await db.db.commit()
    return row_id


# ── the storage API ──────────────────────────────────────────────────────


async def test_round_trip_and_side_data_is_ids_and_counts_only(db):
    conv = _conv()
    q_id = await _store(db, A, conv, caller_email="carer@example.test", model="ignored-on-q")
    a_id = await _store(
        db,
        A,
        conv,
        role="assistant",
        text=ANSWER,
        model="local-model-x",
        citations=[
            {"document_id": 41, "page": 2, "evidence": "PRINTED TEXT", "filename": "x.pdf"},
            {"document_id": 41, "page": 2},  # duplicate dropped
            {"document_id": "41"},  # not an int: dropped
            {"document_id": 7},
        ],
        grounding={
            "numbers_checked": 3,
            "Bad Key": 8,
            "text": "no",
            "refused": -8,
        },
    )
    assert q_id and a_id
    msgs = await chat_history.conversation_messages(db, patient_id=A, conversation_id=conv)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["text"] == QUESTION and msgs[1]["text"] == ANSWER
    question, answer = msgs
    assert question["model"] is None and answer["model"] == "local-model-x"
    assert msgs[1]["citations"] == [
        {"document_id": 41, "page": 2},
        {"document_id": 7, "page": None},
    ]
    assert msgs[1]["grounding"] == {"numbers_checked": 3}
    raw = await _rows(db)
    blob = b"".join(bytes(r["body_enc"]) for r in raw)
    assert QUESTION.encode() not in blob and ANSWER.encode() not in blob
    assert all(r["caller_email_hash"] != "carer@example.test" for r in raw)
    assert "PRINTED TEXT" not in json.dumps([r["citations_json"] for r in raw])


async def test_follow_up_window_is_the_last_n_oldest_first(db):
    conv = _conv()
    for i in range(6):
        await _store(db, A, conv, text=f"synthetic message {i}")
    msgs = await chat_history.conversation_messages(db, patient_id=A, conversation_id=conv, limit=3)
    assert [m["text"] for m in msgs] == [f"synthetic message {i}" for i in range(3, 6)]


async def test_list_conversations_has_a_title_and_no_other_text(db):
    cx, cy = _conv(), _conv()
    await _store(db, A, cx, text="First synthetic question " + "x" * 200)
    await _store(db, A, cx, role="assistant", text=ANSWER)
    await _store(db, A, cy)
    convs = await chat_history.list_conversations(db, patient_id=A)
    assert [c["conversation_id"] for c in convs] == [cy, cx]
    assert convs[1]["messages"] == 2
    assert len(convs[1]["title"]) == chat_history.TITLE_CHARS
    assert ANSWER not in json.dumps(convs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"channel": "sms"},
        {"role": "system"},
        {"conversation_id": "not-a-uuid"},
        {"conversation_id": str(uuid.uuid4()).upper()},
    ],
)
async def test_programming_errors_raise(db, kwargs):
    args = {
        "patient_id": A,
        "conversation_id": _conv(),
        "channel": "dashboard",
        "role": "user",
        "text": QUESTION,
    }
    with pytest.raises(ValueError):
        await chat_history.store_message(db, **{**args, **kwargs})
    assert await _rows(db) == []


@pytest.mark.parametrize("pid", [None, "", "  ", "default", "__no_patient_access__"])
async def test_tenant_guard_on_write(db, pid):
    with pytest.raises(ValueError):
        await _store(db, pid, _conv())
    assert await _rows(db) == []


async def test_empty_or_too_long_text_is_not_stored(db):
    assert await _store(db, A, _conv(), text="   ") is None
    assert await _store(db, A, _conv(), text="x" * (chat_history.MAX_TEXT_CHARS + 1)) is None
    assert await _rows(db) == []
    assert chat_history.counters() == {"empty_text": 1, "too_long": 1}


# ── isolation ────────────────────────────────────────────────────────────


async def test_foreign_patient_or_conversation_is_not_found(two):
    conv = _conv()
    await _store(two, A, conv)
    assert await chat_history.conversation_messages(two, patient_id=B, conversation_id=conv) == []
    assert await chat_history.list_conversations(two, patient_id=B) == []
    assert await chat_history.list_conversations(two, patient_id=B, trashed=True) == []
    for fn in (chat_history.delete_conversation, chat_history.restore_conversation):
        assert (await fn(two, patient_id=B, conversation_id=conv))["status"] == "not_found"
    unknown = _conv()
    assert (
        await chat_history.conversation_messages(two, patient_id=A, conversation_id=unknown) == []
    )
    assert (await chat_history.delete_conversation(two, patient_id=A, conversation_id=unknown))[
        "status"
    ] == "not_found"
    # A's conversation is untouched by B's attempts
    assert (
        len(await chat_history.conversation_messages(two, patient_id=A, conversation_id=conv)) == 1
    )


async def test_a_conversation_of_another_patient_is_refused_on_write(two):
    conv = _conv()
    await _store(two, A, conv)
    with pytest.raises(ValueError, match="another patient"):
        await _store(two, B, conv)
    assert len(await _rows(two)) == 1


# ── encryption ───────────────────────────────────────────────────────────


async def test_a_body_copied_to_another_row_never_decrypts(two, caplog):
    ca, cb, cc = _conv(), _conv(), _conv()
    await _store(two, A, ca, text="synthetic secret one")
    await _store(two, A, cb, text="synthetic secret two")
    await _store(two, B, cc, text="synthetic secret three")
    rows = {r["conversation_id"]: r for r in await _rows(two)}
    # copy the first body onto another row of A and onto a row of B
    for target in (cb, cc):
        await two.db.execute(
            "UPDATE chat_messages SET body_enc = ? WHERE id = ?",
            (rows[ca]["body_enc"], rows[target]["id"]),
        )
    await two.db.commit()
    caplog.set_level(logging.DEBUG)
    ma = await chat_history.conversation_messages(two, patient_id=A, conversation_id=cb)
    mb = await chat_history.conversation_messages(two, patient_id=B, conversation_id=cc)
    assert ma[0]["text"] is None
    assert mb[0]["text"] is None
    assert chat_history.counters()["unreadable"] == 2
    assert "synthetic secret" not in caplog.text
    # and the untouched row still opens
    mo = await chat_history.conversation_messages(two, patient_id=A, conversation_id=ca)
    assert mo[0]["text"] == "synthetic secret one"


def test_aad_names_patient_conversation_and_row():
    conv, row = _conv(), str(uuid.uuid4())
    blob = chat_history.seal("synthetic", A, conv, row)
    assert chat_history.unseal(blob, A, conv, row) == "synthetic"
    for args in ((B, conv, row), (A, _conv(), row), (A, conv, str(uuid.uuid4()))):
        with pytest.raises(Exception) as exc:
            chat_history.unseal(blob, *args)
        assert type(exc.value).__name__ == "InvalidTag"


def test_key_has_its_own_purpose_and_sources(monkeypatch):
    dedicated = secrets_keys.chat_history_key()
    monkeypatch.delenv(secrets_keys.CHAT_HISTORY_SECRET_ENV)
    monkeypatch.setenv(secrets_keys.HOLDING_STORE_SECRET_ENV, "synthetic-holding-1022")
    derived = secrets_keys.chat_history_key()
    assert len(derived) == 32 and derived != dedicated
    assert derived not in (secrets_keys.holding_store_key(), secrets_keys.quarantine_key())
    monkeypatch.delenv(secrets_keys.HOLDING_STORE_SECRET_ENV)
    assert not secrets_keys.chat_history_secret_set()
    with pytest.raises(secrets_keys.ChatHistorySecretMissingError):
        secrets_keys.chat_history_key()


async def test_key_unset_stores_nothing_and_counts(db, monkeypatch, caplog):
    monkeypatch.delenv(secrets_keys.CHAT_HISTORY_SECRET_ENV)
    monkeypatch.delenv(secrets_keys.HOLDING_STORE_SECRET_ENV, raising=False)
    caplog.set_level(logging.DEBUG)
    assert await _store(db, A, _conv()) is None
    assert await _rows(db) == []
    assert chat_history.counters() == {"key_missing": 1}
    assert QUESTION not in caplog.text


# ── user delete / restore ────────────────────────────────────────────────


async def test_user_delete_moves_to_trash_and_restore_brings_it_back(db):
    conv = _conv()
    await _store(db, A, conv)
    await _store(db, A, conv, role="assistant", text=ANSWER)
    out = await chat_history.delete_conversation(db, patient_id=A, conversation_id=conv)
    assert out == {"status": "trashed", "messages": 2}
    assert await chat_history.conversation_messages(db, patient_id=A, conversation_id=conv) == []
    assert await chat_history.list_conversations(db, patient_id=A) == []
    trash = await chat_history.list_conversations(db, patient_id=A, trashed=True)
    assert [t["conversation_id"] for t in trash] == [conv] and trash[0]["title"] == QUESTION
    rows = await _rows(db)
    assert {r["deleted_by"] for r in rows} == {"user"} and all(r["body_enc"] for r in rows)
    again = await chat_history.delete_conversation(db, patient_id=A, conversation_id=conv)
    assert again["status"] == "not_found"
    back = await chat_history.restore_conversation(db, patient_id=A, conversation_id=conv)
    assert back == {"status": "restored", "messages": 2}
    msgs = await chat_history.conversation_messages(db, patient_id=A, conversation_id=conv)
    assert [m["text"] for m in msgs] == [QUESTION, ANSWER]


# ── retention: modes, time travel ────────────────────────────────────────


async def _seed_retention_set(db):
    """live 100 d (→ trash), live 10 d (stays), trashed 40 d ago (→ purge),
    trashed 10 d ago (stays in the trash)."""
    return {
        "old_live": await _seed_aged(db, A, created=NOW - timedelta(days=100)),
        "new_live": await _seed_aged(db, A, created=NOW - timedelta(days=10)),
        "trash_old": await _seed_aged(
            db, A, created=NOW - timedelta(days=200), deleted=NOW - timedelta(days=40)
        ),
        "trash_new": await _seed_aged(
            db, A, created=NOW - timedelta(days=95), deleted=NOW - timedelta(days=10)
        ),
    }


async def test_mode_off_is_the_default_and_touches_nothing(db):
    await _seed_retention_set(db)
    before = await _rows(db)
    assert chat_retention.retention_mode() == "off"
    out = await chat_retention.chat_retention_iteration(db, asyncio.Semaphore(1), now=NOW)
    assert out == {"mode_off": 1}
    assert await _rows(db) == before


async def test_unknown_mode_is_off_with_one_warning(db, monkeypatch, caplog):
    monkeypatch.setenv(chat_retention.MODE_ENV, "delete-everything")
    caplog.set_level(logging.WARNING, logger="oncofiles.chat_retention")
    assert chat_retention.retention_mode() == "off"
    assert chat_retention.retention_mode() == "off"
    assert caplog.text.count("unknown value") == 1


async def test_mode_report_counts_and_writes_nothing(db, monkeypatch):
    monkeypatch.setenv(chat_retention.MODE_ENV, "report")
    await _seed_retention_set(db)
    before = await _rows(db)
    out = await chat_retention.chat_retention_iteration(db, asyncio.Semaphore(1), now=NOW)
    assert out["mode_report"] == 1
    assert (out["would_trash"], out["would_purge"]) == (1, 1)
    assert await _rows(db) == before


async def test_mode_on_trashes_after_90_and_purges_after_30_in_the_trash(db, monkeypatch, caplog):
    monkeypatch.setenv(chat_retention.MODE_ENV, "on")
    ids = await _seed_retention_set(db)
    sem = asyncio.Semaphore(1)
    caplog.set_level(logging.DEBUG)
    out = await chat_retention.chat_retention_iteration(db, sem, now=NOW)
    assert out["mode_on"] == 1
    assert (out["trashed"], out["purged"]) == (1, 1)
    rows = {r["id"]: r for r in await _rows(db)}
    assert len(rows) == 4  # never a hard delete
    old = rows[ids["old_live"]]
    assert old["deleted_at"] == chat_retention_sql.ts(NOW) and old["deleted_by"] == "retention"
    assert old["body_enc"] is not None and old["purged_at"] is None
    assert rows[ids["new_live"]]["deleted_at"] is None
    purged = rows[ids["trash_old"]]
    assert purged["purged_at"] == chat_retention_sql.ts(NOW)
    assert purged["body_enc"] is None and purged["citations_json"] is None
    assert purged["grounding_json"] is None and purged["caller_email_hash"] is None
    kept = rows[ids["trash_new"]]  # 10 days in the trash: not yet
    assert kept["purged_at"] is None and kept["body_enc"] is not None
    assert "trashed 1, purged 1 rows" in caplog.text and QUESTION not in caplog.text
    # idempotent: a second pass the same day changes nothing
    again = await chat_retention.chat_retention_iteration(db, sem, now=NOW)
    assert (again["trashed"], again["purged"]) == (0, 0)
    # 30 days on, the row trashed today is purged (and the 10-day one long since)
    later = await chat_retention.chat_retention_iteration(db, sem, now=NOW + timedelta(days=31))
    assert later["purged"] == 2
    final = {r["id"]: r for r in await _rows(db)}
    assert final[ids["old_live"]]["body_enc"] is None
    assert later["trashed"] == 0
    assert final[ids["new_live"]]["deleted_at"] is None  # 41 days old: still live


async def test_restore_inside_the_window_restarts_the_90_days(db, monkeypatch):
    monkeypatch.setenv(chat_retention.MODE_ENV, "on")
    sem = asyncio.Semaphore(1)
    row_id = await _seed_aged(db, A, created=NOW - timedelta(days=100))
    conv = (await _rows(db))[0]["conversation_id"]
    await chat_retention.chat_retention_iteration(db, sem, now=NOW)
    assert (await _rows(db))[0]["deleted_at"] is not None
    back = await chat_history.restore_conversation(db, patient_id=A, conversation_id=conv)
    assert back["status"] == "restored"
    restored_at = datetime.strptime(
        (await _rows(db))[0]["restored_at"], chat_retention_sql.TS_FORMAT
    )
    restored_at = restored_at.replace(tzinfo=UTC)
    # the next runs (restore + 89 days) do not trash it again
    out = await chat_retention.chat_retention_iteration(
        db, sem, now=restored_at + timedelta(days=89)
    )
    assert out["trashed"] == 0
    row = (await _rows(db, "id = ?", (row_id,)))[0]
    assert row["deleted_at"] is None and row["body_enc"] is not None
    # after 90 days from the restore it goes to the trash again
    out = await chat_retention.chat_retention_iteration(
        db, sem, now=restored_at + timedelta(days=91)
    )
    assert out["trashed"] == 1


async def test_a_purged_conversation_cannot_be_restored(db, monkeypatch):
    monkeypatch.setenv(chat_retention.MODE_ENV, "on")
    await _seed_aged(db, A, created=NOW - timedelta(days=200), deleted=NOW - timedelta(days=31))
    conv = (await _rows(db))[0]["conversation_id"]
    await chat_retention.chat_retention_iteration(db, asyncio.Semaphore(1), now=NOW)
    out = await chat_history.restore_conversation(db, patient_id=A, conversation_id=conv)
    assert out["status"] == "not_found"
    assert await chat_history.list_conversations(db, patient_id=A, trashed=True) == []


async def test_iteration_runs_under_the_semaphore(db, monkeypatch):
    monkeypatch.setenv(chat_retention.MODE_ENV, "on")
    sem = asyncio.Semaphore(1)
    held: list[bool] = []
    real = db.trash_expired_chat_messages

    async def spy(**kw):
        held.append(sem.locked())
        return await real(**kw)

    monkeypatch.setattr(db, "trash_expired_chat_messages", spy)
    await chat_retention.chat_retention_iteration(db, sem, now=NOW)
    assert held == [True]


def test_no_hard_delete_of_chat_rows_outside_the_erasure():
    src = Path(chat_history.__file__).parent
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert "delete from chat_messages" not in text, path


# ── the loop: lifespan, zone, telemetry ──────────────────────────────────


async def test_loop_starts_with_sync_disabled_in_its_zone(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    seen: dict = {}

    async def fake_loop(db, semaphore):
        seen["zone"] = (local_only_job(), semaphore)

    monkeypatch.setattr(chat_retention, "chat_retention_loop", fake_loop)
    async with server.lifespan(MagicMock()):
        await asyncio.sleep(0)
        assert scheduler_singleton.current_scheduler() is None
    assert seen["zone"] == (JOB_ID, server._sync_semaphore)


async def test_not_an_apscheduler_job_with_sync_enabled(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", True)
    monkeypatch.setattr(server, "AI_RUN_ON_STARTUP", False)
    async with server.lifespan(MagicMock()):
        sched, _ = scheduler_singleton.current_scheduler()
        assert JOB_ID not in {j.id for j in sched.get_jobs()}
        assert scheduler_singleton.background_loop_running(JOB_ID)


async def test_readiness_carries_mode_counts_and_storage_counters(monkeypatch):
    import oncofiles.server as server
    from tests.test_one_scheduler_per_process import OPERATOR_TOKEN, _request

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv(chat_retention.MODE_ENV, "report")
    chat_history._counters["key_missing"] += 2

    async def one():
        out = {"mode_report": 1, "would_trash": 4}
        out["would_purge"] = 1
        return out

    await chat_retention.CHAT_RETENTION.run_once(one)
    request, _ = _request(operator=True)
    body = json.loads((await server.readiness(request)).body)
    job = body["jobs"][JOB_ID]
    assert job["mode"] == "report" and job["schedule"] == "daily 04:45 UTC"
    summary = job["last_summary"]
    assert summary["would_trash"] == 4 and summary["would_purge"] == 1
    assert summary["trashed"] == 0 and summary["purged"] == 0
    assert job["last_deleted"] == 0
    assert job["chat_history"] == {"key_missing": 2}
    public, _ = _request(operator=False)
    assert "jobs" not in json.loads((await server.readiness(public)).body)


def test_catalogue_and_admin_job_list():
    assert JOB_ID in scheduled_jobs.CATALOGUE and JOB_ID in scheduled_jobs.BACKGROUND_LOOP_JOBS
    entry = scheduled_jobs.background_loop_entry(
        JOB_ID, running=True, state=scheduled_jobs.background_loop_state(JOB_ID)
    )
    assert entry["registered"] is True and entry["known"] is True
    assert entry["trigger"] == {"kind": "cron", "cadence": "daily", "text": "daily 04:45 UTC"}


def test_the_734_scan_reaches_both_steps_from_the_loop():
    from tests.test_no_automatic_deletion import (
        CHAT_RETENTION_REASON,
        JOB_REACHABLE_ALLOWLIST,
        _job_offenders,
        _load_modules,
    )

    offenders = set(_job_offenders(_load_modules()))
    for ref in ("trash_expired_chat_messages", "purge_trashed_chat_messages"):
        key = ("chat_retention.py", "chat_retention_iteration", ref)
        assert key in offenders
        assert JOB_REACHABLE_ALLOWLIST[key] == CHAT_RETENTION_REASON
    assert "maintainer decision 2026-11-28, RUNBOOK lifecycle" in CHAT_RETENTION_REASON
    # the user's delete is never reachable from a job
    assert not [o for o in offenders if o[2] == "soft_delete_chat_conversation"]


# ── job_dryrun chat_history_retention ────────────────────────────────────


async def _file_db(path: Path) -> Path:
    db = Database(str(path))
    await db.connect()
    await db.migrate()
    await _seed_retention_set(db)
    await db.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db.close()
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size == 0:
        wal.unlink()
    return path


async def test_dry_run_counts_match_one_real_run(tmp_path, monkeypatch):
    path = await _file_db(tmp_path / "copy.db")
    before = path.read_bytes()
    report = job_dryrun.run(JOB_ID, path, now=NOW)
    assert path.read_bytes() == before
    assert report["would_trash"] == 1 and report["would_purge"] == 1
    assert report["rows_total"] == 4 and report["rows_in_trash"] == 2
    assert report["rows_live"] == 2 and report["rows_purged"] == 0
    assert report["patients_with_rows"] == 1
    text = json.dumps(report)
    assert A not in text and "synthhash" not in text

    monkeypatch.setenv(chat_retention.MODE_ENV, "on")
    db = Database(str(path))
    await db.connect()
    try:
        out = await chat_retention.chat_retention_iteration(db, asyncio.Semaphore(1), now=NOW)
    finally:
        await db.close()
    assert (out["trashed"], out["purged"]) == (report["would_trash"], report["would_purge"])


def test_dry_run_job_is_registered_and_offline():
    assert JOB_ID in job_dryrun.JOBS
    src = Path(chat_retention_sql.__file__).read_text()
    assert "from oncofiles" not in src and "import oncofiles" not in src


# ── data rights: export decrypted, erased with the patient ───────────────


async def _export(db, pid) -> dict:
    data = b"".join([chunk async for chunk in data_rights.export_stream(db, pid, now=NOW)])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        lines = zf.read("tables/chat_messages.jsonl").decode().splitlines()
        manifest = json.loads(zf.read("manifest.json"))
    return {"rows": [json.loads(x) for x in lines], "manifest": manifest}


async def test_export_holds_the_decrypted_text_of_own_rows_only(two):
    conv = _conv()
    await _store(two, A, conv, caller_email="carer@example.test")
    await _store(two, A, conv, role="assistant", text=ANSWER, model="local-model-x")
    await _store(two, B, _conv(), text="Boris synthetic question")
    # one purged row and one unreadable row
    await _seed_aged(two, A, created=NOW - timedelta(days=300), deleted=NOW - timedelta(days=200))
    await two.purge_trashed_chat_messages(
        now=chat_retention_sql.ts(NOW), purge_cutoff=chat_retention_sql.purge_cutoff(NOW)
    )
    bad = await _store(two, A, _conv(), text="synthetic unreadable")
    await two.db.execute(
        "UPDATE chat_messages SET row_uuid = ? WHERE id = ?", (str(uuid.uuid4()), bad)
    )
    await two.db.commit()

    out = await _export(two, A)
    rows = out["rows"]
    assert out["manifest"]["row_counts"]["chat_messages"] == 4
    bodies = [r["body"] for r in rows]
    assert QUESTION in bodies and ANSWER in bodies and "Boris synthetic question" not in bodies
    assert None in bodies  # the purged row
    assert sum(1 for r in rows if r.get("body_unreadable")) == 1
    for r in rows:
        assert "body_enc" not in r and "row_uuid" not in r
        assert "caller_email_hash" not in r and "deleted_by" not in r
        assert r["patient_id"] == A
    assert any(r.get("model") == "local-model-x" for r in rows)


async def test_erasure_removes_the_chat_history_and_keeps_the_other_patient(
    two,
    fake_copy,  # noqa: F811
    fake_revoke,  # noqa: F811
):
    await _store(two, A, _conv())
    await _store(two, B, _conv(), text="Boris synthetic question")
    counts = await data_rights.count_rows(two, B)
    assert counts["chat_messages"] == 1
    out = await patient_erasure.erase_patient(
        two,
        patient_id=B,
        confirm_slug=B_SLUG,
        dry_run=False,
        reason="request by e-mail 2027-02-03",
        caller_email="admin-1022@example.test",
    )
    assert out["status"] == "erased", out
    assert await _rows(two, "patient_id = ?", (B,)) == []
    assert len(await _rows(two, "patient_id = ?", (A,))) == 1


def test_registry_rule_exports_with_the_decoder_and_hides_raw_columns():
    rule = data_rights.rule_for("chat_messages")
    assert rule.export is True and rule.decoder == "chat"
    cols = [
        "id",
        "patient_id",
        "conversation_id",
        "channel",
        "role",
        "row_uuid",
        "body_enc",
        "citations_json",
        "model",
        "grounding_json",
        "caller_email_hash",
        "created_at",
        "deleted_by",
    ]
    exported = data_rights.exported_columns(rule, cols)
    assert "body_enc" not in exported and "caller_email_hash" not in exported
    assert "deleted_by" not in exported


def test_sqlite_check_constraints_hold_the_vocab():
    from oncofiles import vocab

    assert vocab.codes("chat_channel") == {"dashboard", "signal", "whatsapp"}
    assert vocab.codes("chat_role") == {"user", "assistant"}
    sql = (Path(__file__).parent.parent / "migrations" / "106_chat_messages.sql").read_text()
    assert "CHECK (channel IN ('dashboard', 'signal', 'whatsapp'))" in sql
    assert "CHECK (role IN ('user', 'assistant'))" in sql


async def test_check_constraint_refuses_a_raw_insert(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.db.execute(
            "INSERT INTO chat_messages (patient_id, conversation_id, channel, role, row_uuid) "
            "VALUES (?, ?, 'sms', 'user', ?)",
            (A, _conv(), str(uuid.uuid4())),
        )
