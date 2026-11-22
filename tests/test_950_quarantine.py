# ruff: noqa: F811 - the #950 fixtures are imported and used as test arguments
"""#950 quarantine: attachments from other senders wait for the caregiver.

Maintainer decision 2026-11-22. Covers: who is quarantined (unknown sender,
forged / unverified caregiver) and who is filed directly (the caregiver);
encrypted storage that is never a document; the notification e-mail
(throttle per sender, digest, daily cap, no file names, no reply to the
sender); the ``/q/<token>`` links (GET never changes anything, POST with the
token and CSRF value in the body, single use, distinct per action, expired /
used / bad = one neutral answer, headers); the dashboard card and decisions
through the #867 ACL; caps; expiry (mark only); logs without addresses, file
names or tokens; the rate-limit class.

Synthetic only: invented addresses (example.test and invented Gmail local
parts), invented file names and bytes, dates in 2027. No network: the mailer
and Drive are stubbed.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from oncofiles import inbound_email, inbound_quarantine, server
from tests.test_950_inbound_address import (  # noqa: F401 - fixtures used by name
    CARER,
    OTHER_CARER,
    OTHER_PID,
    PID,
    STRANGER,
    SUBJECT,
    _call,
    _dash_request,
    _env,
    address_of,
    drive,
    idb,
    jpeg,
    mail,
    mails,
    parsed,
    pdf,
)

ROOT = Path(server.__file__).parent.parent.parent
SECRET_NAME = "SynthQuarantineReport2027.pdf"
PHOTO_NAME = "SynthQuarantinePhoto2027.jpg"
HOLDING_SECRET = "synthetic-holding-secret-950q"
TOKEN_RE = re.compile(r"https://oncofiles\.com/q/([A-Za-z0-9_-]+)")


@pytest.fixture(autouse=True)
def _quarantine_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOLDING_STORE_SECRET", HOLDING_SECRET)
    monkeypatch.setattr("oncofiles.config.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(inbound_quarantine, "_free_bytes", lambda: 10**15)
    # the curated inbox listing behind the approve duplicate check (stub Drive)
    monkeypatch.setattr("oncofiles.curated.inbox_upload.inbox_md5s", lambda gdrive, folder: set())
    inbound_quarantine._last_failed.clear()
    yield
    inbound_quarantine._last_failed.clear()


def ago(**kw) -> str:
    """A past instant relative to now (logic timestamps, not document dates)."""
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def notices(mails):
    return [m for m in mails if m.get("tags") == ["inbound_quarantine"]]


def links(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


async def quarantine_one(db, sender=STRANGER, files=None, auth="pass"):
    addr = await address_of(db)
    files = files or [(SECRET_NAME, pdf(1), "application/pdf")]
    return await inbound_email.process_message(
        db, parsed(mail(addr, sender=sender, files=files), auth=auth)
    )


async def rows(db, sql, *args):
    async with db.db.execute(sql, args) as cur:
        return [dict(r) for r in await cur.fetchall()]


def link_request(db, path: str, *, method="GET", body: bytes = b"", headers=None) -> Request:
    app = MagicMock()
    app.state.fastmcp_server._lifespan_result = {"db": db}
    token = path.removeprefix("/q/") if path.startswith("/q/") else ""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [(k.lower().encode(), str(v).encode()) for k, v in (headers or {}).items()],
        "app": app,
        "path_params": {"token": token} if token else {},
        "scheme": "https",
        "server": ("oncofiles.com", 443),
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


async def get_page(db, token: str):
    return await server.quarantine_link_page(link_request(db, f"/q/{token}"))


def csrf_of(page_body: bytes) -> str:
    return re.search(rb'name="csrf" value="([0-9a-f]+)"', page_body).group(1).decode()


async def post_link(db, token: str, csrf: str, headers=None):
    from urllib.parse import urlencode

    body = urlencode({"token": token, "csrf": csrf}).encode()
    hdrs = {"content-type": "application/x-www-form-urlencoded", **(headers or {})}
    return await server.quarantine_link_action(
        link_request(db, "/q", method="POST", body=body, headers=hdrs)
    )


# ── who is quarantined ───────────────────────────────────────────────────


async def test_unknown_sender_is_quarantined_not_filed_and_caregiver_notified(
    idb, drive, mails, tmp_path
):
    result = await quarantine_one(idb)
    assert result.outcome == "quarantined"
    assert drive.files == []
    [item] = await rows(idb, "SELECT * FROM inbound_quarantine")
    assert item["state"] == "pending" and item["refusal_reason"] == "unknown_sender"
    assert item["file_count"] == 1 and item["patient_id"] == PID
    # encrypted at rest: neither the bytes, the name nor the sender in plaintext
    assert STRANGER not in json.dumps(item) and SECRET_NAME not in json.dumps(item)
    [blob] = list((tmp_path / "quarantine" / item["item_uuid"]).iterdir())
    assert b"%PDF" not in blob.read_bytes() and blob.stat().st_mode & 0o777 == 0o600
    # never a document: no row, no page, nothing to index, search or preview
    assert await idb.count_documents(patient_id=PID) == 0
    assert await rows(idb, "SELECT * FROM document_pages") == []
    # one notice to the caregiver; nothing to the sender
    [note] = notices(mails)
    assert note["to"] == [CARER] and len(mails) == 1
    assert note["headers"] == {"Auto-Submitted": "auto-generated"}
    text = note["subject"] + note["text"]
    assert STRANGER in text and "PDF" in text
    assert SECRET_NAME not in text and SUBJECT not in text and "Jana" not in text
    assert "Body text" not in text
    approve, reject = links(note["text"])[:2]
    assert approve != reject


async def test_caregiver_with_passing_auth_is_filed_directly(idb, drive, mails):
    result = await quarantine_one(idb, sender=CARER)
    assert result.outcome == "filed" and result.accepted == 1
    assert len(drive.files) == 1
    assert await rows(idb, "SELECT * FROM inbound_quarantine") == []
    assert notices(mails) == []


async def test_forged_caregiver_is_quarantined_with_a_warning(idb, drive, mails):
    result = await quarantine_one(idb, sender=CARER, auth="fail")
    assert result.outcome == "quarantined" and drive.files == []
    [item] = await rows(idb, "SELECT refusal_reason FROM inbound_quarantine")
    assert item["refusal_reason"] == "auth_failed"
    [note] = notices(mails)
    assert note["to"] == [CARER]  # the real caregiver, never a reply "to the sender"
    # infra review of #970: no approve link for an unverified claim of the
    # caregiver's own address; reject + the dashboard only
    assert "not signed by your mail provider" in note["text"]
    assert "Approve:" not in note["text"] and "Schváliť:" not in note["text"]
    assert len(set(links(note["text"]))) == 1 and "/dashboard" in note["text"]
    tokens = await rows(idb, "SELECT action FROM inbound_quarantine_tokens")
    assert [t["action"] for t in tokens] == ["reject"]


async def test_loop_mail_is_still_ignored(idb, drive, mails):
    addr = await address_of(idb)
    msg = mail(
        addr,
        sender=STRANGER,
        files=[(SECRET_NAME, pdf(), "application/pdf")],
        headers={"Auto-Submitted": "auto-replied"},
    )
    assert (await inbound_email.process_message(idb, parsed(msg))).outcome == "ignored_loop"
    assert await rows(idb, "SELECT * FROM inbound_quarantine") == []


async def test_without_the_holding_secret_it_is_only_listed(idb, drive, mails, monkeypatch):
    monkeypatch.delenv("HOLDING_STORE_SECRET")
    assert (await quarantine_one(idb)).outcome == "unmatched"
    assert await rows(idb, "SELECT * FROM inbound_quarantine") == []
    assert notices(mails) == []


async def test_screens_apply_and_raw_genotype_never_enters(idb, drive, mails):
    files = [
        ("SynthGenotype2027.csv", b"rsid,chromosome,genotype\nrs0000001,1,AA\n", "text/csv"),
        ("SynthArchive2027.zip", b"PK\x03\x04" + bytes(100), "application/zip"),
    ]
    result = await quarantine_one(idb, files=files)
    assert result.outcome == "unmatched"
    assert await rows(idb, "SELECT * FROM inbound_quarantine") == []


async def test_same_delivery_is_quarantined_once(idb, drive, mails):
    addr = await address_of(idb)
    msg = mail(addr, sender=STRANGER, files=[(SECRET_NAME, pdf(), "application/pdf")])
    raw = msg.as_bytes()
    first = await inbound_email.process_message(idb, inbound_email.parse_raw_mime(raw))
    inbound_email.reset_state()  # a restart: the in-memory done-ids are gone
    again = await inbound_email.process_message(idb, inbound_email.parse_raw_mime(raw))
    assert first.outcome == again.outcome == "quarantined"
    assert len(await rows(idb, "SELECT * FROM inbound_quarantine")) == 1


async def test_temporary_download_failure_writes_nothing_and_retries(idb, mails):
    async def _fail():
        raise inbound_email.TemporaryFetchError("fetch_failed")

    addr = await address_of(idb)
    msg = inbound_email.InboundMessage(
        recipients=[addr],
        sender=STRANGER,
        auth_pass=False,
        message_id="<synthetic-q-retry@example.test>",
        attachments=[
            inbound_email.InboundAttachment(
                name=SECRET_NAME, data=b"", fetch=_fail, declared_size=1000, ref="synthref1"
            )
        ],
    )
    progress = inbound_email.JobProgress()
    async with inbound_email.PROCESS_LOCK:
        result = await inbound_email._process_message(idb, msg, progress)
    assert result.temporary == "fetch_failed"
    assert await rows(idb, "SELECT * FROM inbound_quarantine") == []


# ── notifications ────────────────────────────────────────────────────────


async def test_one_notice_per_sender_per_hour_then_a_digest(idb, drive, mails):
    await quarantine_one(idb, files=[(SECRET_NAME, pdf(1), "application/pdf")])
    await quarantine_one(idb, files=[(PHOTO_NAME, jpeg(), "image/jpeg")])
    assert len(notices(mails)) == 1
    await inbound_quarantine.run_maintenance(idb)
    assert len(notices(mails)) == 1  # still throttled
    await idb.db.execute(
        "UPDATE inbound_quarantine SET notified_at = ? WHERE notified_at IS NOT NULL",
        (ago(hours=2),),
    )
    await idb.db.commit()
    await inbound_quarantine.run_maintenance(idb)
    digest = notices(mails)[1]
    assert "JPG" in digest["text"] and PHOTO_NAME not in digest["text"]
    assert len(set(links(digest["text"]))) == 2


async def test_at_most_three_notices_per_patient_per_day(idb, drive, mails):
    for i in range(5):
        await quarantine_one(
            idb,
            sender=f"stranger{i}@example.test",
            files=[(f"Synth{i}.pdf", pdf(100 + i), "application/pdf")],
        )
    assert len(notices(mails)) == inbound_quarantine.MAX_NOTICES_PER_DAY
    await inbound_quarantine.run_maintenance(idb)
    assert len(notices(mails)) == inbound_quarantine.MAX_NOTICES_PER_DAY
    # the rest are on the dashboard card
    view = await inbound_quarantine.dashboard_view(idb, PID, caregiver=True)
    assert view["pending"] == 5


async def test_a_refused_send_is_retried_later(idb, drive, monkeypatch):
    calls = []

    async def _refuse(to, subject, text, **kw):
        calls.append(1)
        return False

    monkeypatch.setattr("oncofiles.mailer.send_mail", _refuse)
    await quarantine_one(idb)
    assert calls == [1]
    [item] = await rows(idb, "SELECT notified_at FROM inbound_quarantine")
    assert item["notified_at"] is None


# ── /q/<token> links ─────────────────────────────────────────────────────


async def _tokens(idb, mails):
    await quarantine_one(idb)
    approve, reject = links(notices(mails)[0]["text"])[:2]
    return approve, reject


async def test_get_changes_nothing_and_shows_no_file_name(idb, drive, mails):
    approve, reject = await _tokens(idb, mails)
    for _ in range(3):  # a link scanner prefetching
        page = await get_page(idb, approve)
        assert page.status_code == 200
    body = page.body.decode()
    assert STRANGER in body and "PDF" in body and SECRET_NAME not in body
    assert approve in body  # only inside the hidden form field
    assert 'method="post" action="/q"' in body
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in page.headers["x-robots-tag"]
    assert "default-src 'none'" in page.headers["content-security-policy"]
    [item] = await rows(idb, "SELECT state FROM inbound_quarantine")
    assert item["state"] == "pending" and drive.files == []
    assert all(
        r["used_at"] is None for r in await rows(idb, "SELECT * FROM inbound_quarantine_tokens")
    )


async def test_sender_is_escaped_on_the_page():
    assert inbound_quarantine.safe_sender('"<b>x</b>" <a<script>@example.test>').count("<") == 0


async def test_post_approve_files_once_and_a_second_post_is_neutral(idb, drive, mails):
    approve, reject = await _tokens(idb, mails)
    csrf = csrf_of((await get_page(idb, approve)).body)
    done = await post_link(idb, approve, csrf)
    assert b"Approved" in done.body and len(drive.files) == 1
    assert drive.files[0][0] == SECRET_NAME
    neutral = (await get_page(idb, "x" * 43)).body
    again = await post_link(idb, approve, csrf)
    assert again.body == neutral and len(drive.files) == 1
    # the reject token of a decided item is dead too
    assert (await get_page(idb, reject)).body == neutral
    [item] = await rows(idb, "SELECT state, filed_count, decided_via FROM inbound_quarantine")
    assert item == {"state": "approved", "filed_count": 1, "decided_via": "email_link"}
    [entry] = await rows(
        idb, "SELECT * FROM activity_log WHERE tool_name = 'inbound_quarantine_approve'"
    )
    assert entry["patient_id"] == PID and STRANGER not in json.dumps(entry)
    assert SECRET_NAME not in json.dumps(entry)


async def test_post_reject_files_nothing(idb, drive, mails):
    approve, reject = await _tokens(idb, mails)
    csrf = csrf_of((await get_page(idb, reject)).body)
    done = await post_link(idb, reject, csrf)
    assert b"Rejected" in done.body and drive.files == []
    [item] = await rows(idb, "SELECT state, item_uuid FROM inbound_quarantine")
    assert item["state"] == "rejected"
    neutral = (await get_page(idb, "y" * 43)).body
    assert (await get_page(idb, approve)).body == neutral
    # nothing deleted: the encrypted file stays
    assert inbound_quarantine.read_file(PID, item["item_uuid"], 0).startswith(b"%PDF")


async def test_expired_bad_and_wrong_csrf_get_one_neutral_answer(idb, drive, mails):
    approve, _reject = await _tokens(idb, mails)
    csrf = csrf_of((await get_page(idb, approve)).body)
    bad = await get_page(idb, "z" * 43)
    wrong = await post_link(idb, approve, "0" * 64)
    cross = await post_link(idb, approve, csrf, headers={"sec-fetch-site": "cross-site"})
    other_origin = await post_link(idb, approve, csrf, headers={"origin": "https://evil.example"})
    assert bad.body == wrong.body == cross.body == other_origin.body
    assert bad.headers["cache-control"] == "no-store"
    await idb.db.execute("UPDATE inbound_quarantine_tokens SET expires_at = ?", (ago(minutes=1),))
    await idb.db.commit()
    assert (await get_page(idb, approve)).body == bad.body
    assert (await post_link(idb, approve, csrf)).body == bad.body
    assert drive.files == []


async def test_tokens_are_random_hashed_and_bound(idb, drive, mails):
    approve, reject = await _tokens(idb, mails)
    assert len(approve) >= 43 and approve != reject  # 32 random bytes, url-safe
    stored = await rows(idb, "SELECT * FROM inbound_quarantine_tokens ORDER BY id")
    assert {r["action"] for r in stored} == {"approve", "reject"}
    flat = json.dumps(stored)
    assert approve not in flat and reject not in flat
    # a stored hash moved onto another row (or action) fails its binding
    await idb.db.execute(
        "UPDATE inbound_quarantine_tokens SET action = 'reject' WHERE action = 'approve'"
    )
    await idb.db.commit()
    assert await inbound_quarantine.resolve_link(idb, approve) is None


# ── dashboard ────────────────────────────────────────────────────────────


async def test_dashboard_card_for_the_caregiver_and_counts_for_an_admin(idb, drive, mails):
    await quarantine_one(idb)
    resp = await _call(server.api_inbound_quarantine, _dash_request(idb, "synth950-a"), CARER)
    data = json.loads(resp.body)
    [item] = data["items"]
    assert item["sender"] == STRANGER and item["files"][0]["name"] == SECRET_NAME
    admin = await _call(
        server.api_inbound_quarantine,
        _dash_request(idb, "synth950-a"),
        "admin950@example.test",
        admin=True,
    )
    assert json.loads(admin.body) == {
        "enabled": True,
        "read_only": True,
        "pending": 1,
        "items": [],
        "trash": [],
    }
    foreign = await _call(server.api_inbound_quarantine, _dash_request(idb, "synth950-b"), CARER)
    unknown = await _call(server.api_inbound_quarantine, _dash_request(idb, "synth950-zz"), CARER)
    assert foreign.status_code == unknown.status_code == 404 and foreign.body == unknown.body


async def test_dashboard_decide_goes_through_the_acl(idb, drive, mails):
    await quarantine_one(idb)
    [item] = await rows(idb, "SELECT id FROM inbound_quarantine")
    route = server.api_inbound_quarantine_decide

    def body(pid):
        return {"patient_id": pid, "item_id": item["id"], "action": "approve"}

    foreign = await _call(route, _dash_request(idb, body=body("synth950-b")), CARER)
    unknown = await _call(route, _dash_request(idb, body=body("synth950-zz")), CARER)
    assert foreign.status_code == unknown.status_code == 404 and foreign.body == unknown.body
    # the other patient's caregiver cannot decide this patient's item
    other = await _call(route, _dash_request(idb, body=body("synth950-a")), OTHER_CARER)
    assert other.status_code == 404
    admin = await _call(
        route, _dash_request(idb, body=body("synth950-a")), "admin950@example.test", admin=True
    )
    assert admin.status_code == 403  # read-only for an admin
    assert drive.files == []
    ok = await _call(route, _dash_request(idb, body=body("synth950-a")), CARER)
    assert ok.status_code == 200 and json.loads(ok.body)["result"] == "approved"
    again = await _call(route, _dash_request(idb, body=body("synth950-a")), CARER)
    assert json.loads(again.body)["result"] == "already" and len(drive.files) == 1
    # an item id of another patient is the same 404
    wrong = await _call(
        route,
        _dash_request(idb, body={"patient_id": "synth950-a", "item_id": 999, "action": "reject"}),
        CARER,
    )
    assert wrong.status_code == 404


async def test_approve_skips_a_file_already_stored(idb, drive, mails, monkeypatch):
    await quarantine_one(idb)
    [item] = await rows(idb, "SELECT id FROM inbound_quarantine")
    monkeypatch.setattr(idb, "document_md5_exists", lambda *a, **k: _true())
    result = await inbound_quarantine.approve(
        idb, PID, item["id"], decided_by="synthetic-hash", via="dashboard"
    )
    assert result["result"] == "approved" and result["filed"] == 0
    assert result["skipped"] == {"duplicate": 1} and drive.files == []


async def _true():
    return True


async def test_approve_that_cannot_file_now_stays_pending(idb, mails, monkeypatch):
    from oncofiles import dashboard_upload

    async def _no_drive(db, patient_id, log_pid=None):
        return dashboard_upload.refusal("no_drive", 409)

    monkeypatch.setattr(dashboard_upload, "_resolve_target", _no_drive)
    await quarantine_one(idb)
    [item] = await rows(idb, "SELECT id FROM inbound_quarantine")
    result = await inbound_quarantine.approve(
        idb, PID, item["id"], decided_by="synthetic-hash", via="dashboard"
    )
    assert result == {"result": "retry_later", "code": "no_drive", "filed": 0}
    [after] = await rows(idb, "SELECT state, claimed_at FROM inbound_quarantine")
    assert after == {"state": "pending", "claimed_at": None}


# ── caps, expiry, logs ───────────────────────────────────────────────────


async def test_daily_item_cap_lists_the_rest(idb, drive, mails, monkeypatch):
    monkeypatch.setattr(inbound_quarantine, "MAX_ITEMS_PER_DAY", 2)
    for i in range(3):
        result = await quarantine_one(
            idb, files=[(f"Synth{i}.pdf", pdf(200 + i), "application/pdf")]
        )
    assert result.outcome == "unmatched"
    assert len(await rows(idb, "SELECT * FROM inbound_quarantine")) == 2
    assert inbound_email._state.counters["quarantine_listed_quarantine_daily_cap"] == 1


async def test_file_and_byte_caps(idb, drive, mails, monkeypatch):
    monkeypatch.setattr(inbound_quarantine, "MAX_FILES_PER_DAY", 1)
    files = [(f"Synth{i}.pdf", pdf(300 + i), "application/pdf") for i in range(2)]
    assert (await quarantine_one(idb, files=files)).outcome == "unmatched"
    monkeypatch.setattr(inbound_quarantine, "MAX_FILES_PER_DAY", 30)
    monkeypatch.setattr(inbound_quarantine, "MAX_PENDING_BYTES", 1000)
    assert (await quarantine_one(idb)).outcome == "unmatched"
    assert await rows(idb, "SELECT * FROM inbound_quarantine") == []


async def test_quarantine_has_its_own_pending_budget_and_trials_are_unaffected(
    idb, drive, mails, monkeypatch
):
    """Security review of #970: a stranger's quarantine flood never counts
    against the trial holding store's budget."""
    from oncofiles import holding_store

    # the holding budget is far below what the quarantine holds: quarantine
    # still works (it never reads HOLDING_MAX_BYTES) ...
    monkeypatch.setattr(holding_store, "HOLDING_MAX_BYTES", 30_000)
    for i in range(3):
        result = await quarantine_one(
            idb,
            sender=f"flood{i}@example.test",
            files=[(f"Synth{i}.pdf", pdf(400 + i), "application/pdf")],
        )
        assert result.outcome == "quarantined"
    # ... and a trial's holding write is checked against held bytes only
    await idb.db.execute(
        "INSERT INTO trial_accounts (patient_id, sender_hash, source, trial_ends_at) "
        "VALUES (?, ?, 'welcome_email', ?)",
        (PID, "synthetic-trial-hash-950q", ago(days=-30)),
    )
    await idb.db.commit()
    assert await idb.holding_bytes() == 0
    await holding_store.check_limits(idb, PID, 1000)  # no HoldingRefusedError
    # the quarantine's own budget counts pending items only
    monkeypatch.setattr(inbound_quarantine, "QUARANTINE_MAX_PENDING_BYTES", 70_000)
    assert (await quarantine_one(idb, sender="flood9@example.test")).outcome == "unmatched"
    assert inbound_email._state.counters["quarantine_listed_quarantine_full"] == 1
    await idb.db.execute("UPDATE inbound_quarantine SET state = 'rejected'")
    await idb.db.commit()
    assert (await quarantine_one(idb, sender="flood9@example.test")).outcome == "quarantined"


async def test_free_space_guard_still_applies(idb, drive, mails, monkeypatch):
    monkeypatch.setattr(inbound_quarantine, "_free_bytes", lambda: 1)
    assert (await quarantine_one(idb)).outcome == "unmatched"
    assert await rows(idb, "SELECT * FROM inbound_quarantine") == []


async def test_reject_and_resend_loop_is_stopped_by_the_rolling_cap(idb, drive, mails, monkeypatch):
    monkeypatch.setattr(inbound_quarantine, "MAX_RECEIVED_BYTES_30D", 50_000)
    outcomes = []
    for i in range(4):
        result = await quarantine_one(
            idb, files=[(f"Loop{i}.pdf", pdf(500 + i), "application/pdf")]
        )
        outcomes.append(result.outcome)
        for item in await rows(idb, "SELECT id FROM inbound_quarantine WHERE state = 'pending'"):
            await inbound_quarantine.reject(
                idb, PID, item["id"], decided_by="synthetic-hash", via="dashboard"
            )
    assert outcomes == ["quarantined", "quarantined", "unmatched", "unmatched"]
    assert inbound_email._state.counters["quarantine_listed_quarantine_received_cap"] == 2


async def test_admin_overview_reports_retained_bytes(idb, drive, mails):
    await quarantine_one(idb)
    await quarantine_one(idb, sender="other950q@example.test")
    [first, _second] = await rows(idb, "SELECT id, total_bytes FROM inbound_quarantine ORDER BY id")
    await inbound_quarantine.reject(idb, PID, first["id"], decided_by="h", via="dashboard")
    resp = await _call(
        server.api_inbound_unmatched, _dash_request(idb), "admin950@example.test", admin=True
    )
    q = json.loads(resp.body)["quarantine"]
    assert q["retained_bytes"] == first["total_bytes"]
    assert q["by_state"]["rejected"]["items"] == 1 and q["by_state"]["pending"]["items"] == 1
    assert q["pending_budget_bytes"] == inbound_quarantine.QUARANTINE_MAX_PENDING_BYTES
    flat = json.dumps(q)
    assert "@" not in flat and PID not in flat


async def test_link_dies_when_the_caregiver_changes(idb, drive, mails):
    approve, reject = await _tokens(idb, mails)
    neutral = (await get_page(idb, "n" * 43)).body
    csrf = csrf_of((await get_page(idb, approve)).body)
    await idb.update_patient(PID, caregiver_email="new.carer950q@example.test")
    assert (await get_page(idb, approve)).body == neutral
    assert (await post_link(idb, approve, csrf)).body == neutral
    tokens = await rows(idb, "SELECT used_at FROM inbound_quarantine_tokens")
    assert tokens and all(t["used_at"] for t in tokens)  # voided at the change
    assert drive.files == []


async def test_recipient_check_catches_a_change_outside_update_patient(idb, drive, mails):
    approve, _reject = await _tokens(idb, mails)
    neutral = (await get_page(idb, "m" * 43)).body
    # a raw write that bypasses update_patient: the recipient check still holds
    await idb.db.execute(
        "UPDATE patients SET caregiver_email = ? WHERE patient_id = ?",
        ("other.address950q@example.test", PID),
    )
    await idb.db.commit()
    assert (await get_page(idb, approve)).body == neutral
    # back to the caregiver the link was sent to (Gmail variant, same canonical
    # address): valid again
    variant = CARER.replace("synthetic.", "synthetic").replace("@gmail", "+x@gmail")
    await idb.db.execute(
        "UPDATE patients SET caregiver_email = ? WHERE patient_id = ?", (variant, PID)
    )
    await idb.db.commit()
    assert (await get_page(idb, approve)).body != neutral


async def test_same_caregiver_update_keeps_the_links(idb, drive, mails):
    approve, _reject = await _tokens(idb, mails)
    await idb.update_patient(PID, caregiver_email=CARER.upper())
    assert b'name="csrf"' in (await get_page(idb, approve)).body


async def test_old_pending_items_expire_and_nothing_is_deleted(idb, drive, mails):
    await quarantine_one(idb)
    await idb.db.execute("UPDATE inbound_quarantine SET received_at = ?", (ago(days=31),))
    await idb.db.commit()
    out = await inbound_quarantine.run_maintenance(idb)
    assert out["expired"] == 1
    [item] = await rows(idb, "SELECT state, item_uuid FROM inbound_quarantine")
    assert item["state"] == "expired"
    assert inbound_quarantine.read_file(PID, item["item_uuid"], 0).startswith(b"%PDF")


async def test_logs_carry_no_address_file_name_or_token(idb, drive, mails, caplog):
    caplog.set_level(logging.DEBUG)
    approve, _ = await _tokens(idb, mails)
    csrf = csrf_of((await get_page(idb, approve)).body)
    await post_link(idb, approve, csrf)
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("oncofiles"))
    for secret in (STRANGER, CARER, SECRET_NAME, approve, csrf, SUBJECT, PID):
        assert secret not in text
    assert PID[:4] in text


def test_rate_limit_class_of_its_own():
    from oncofiles import ratelimit

    for path in ("/q", "/q/abc"):
        assert ratelimit.classify(path) == ratelimit.QLINK
    assert ratelimit.classify("/questions") == ratelimit.PUBLIC
    mw = ratelimit.RateLimitMiddleware(lambda *a: None, ratelimit.RateLimitConfig())
    n = 0
    while mw.check(ratelimit.QLINK, "ip:192.0.2.50", False) == 0:
        n += 1
    assert n < 20  # a small per-IP bucket
    assert mw.check(ratelimit.PUBLIC, "ip:192.0.2.50", False) == 0
    assert ratelimit.DEFAULTS[ratelimit.QLINK].max_body_bytes == 4 * 1024


def test_migration_is_additive():
    sql = (ROOT / "migrations" / "100_inbound_quarantine.sql").read_text()
    code = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    statements = [st.strip() for st in code.split(";") if st.strip()]
    assert statements and all(st.upper().startswith("CREATE ") for st in statements)
    assert all("IF NOT EXISTS" in st.upper() for st in statements)


def test_quarantine_module_never_reads_with_a_model():
    """No import path from the quarantine module to a model, the indexer, the
    previews or the holding reader: a quarantined file is never read."""
    import ast

    tree = ast.parse(Path(inbound_quarantine.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = ("llm", "extract", "indexer", "doc_thumbnails", "holding_extract", "enhance")
    assert not [m for m in imported if any(f in m.split(".")[-1] for f in forbidden)], imported


def test_demo_twin_and_blocked_actions():
    from oncofiles import demo_dashboard

    built = demo_dashboard.build([], "0.0.0-test")
    twin = built["inbound_quarantine"]
    assert twin["items"][0]["sender"].endswith("@example.test")
    js = (Path(server.__file__).parent / "dashboard.js").read_text()
    blocked = js[
        js.index("var DEMO_BLOCKED_ACTIONS") : js.index("};", js.index("var DEMO_BLOCKED_ACTIONS"))
    ]
    assert "approveQuarantine: 1" in blocked and "rejectQuarantine: 1" in blocked
    assert "if (path === '/api/inbound-quarantine') return d.inbound_quarantine;" in js
    section = js[js.index("// #950 quarantine") : js.index("// Admin zone: mail to patient")]
    assert "innerHTML" not in section and "confirm(" not in section


def test_claude_md_documents_the_834_exception():
    text = (ROOT / "CLAUDE.md").read_text()
    assert "inbound_quarantine" in text and "/q/<token>" in text
    assert "signed action links" in text


def test_counter_types():
    assert isinstance(inbound_quarantine.Outcome("listed").skipped, Counter)


# ── infra review of #970 ─────────────────────────────────────────────────


async def test_q_refuses_an_approve_token_of_an_unverified_claim(idb, drive, mails):
    """Even a planted approve token for an ``auth_failed`` item is refused;
    approving is possible only signed in on the dashboard."""
    await quarantine_one(idb, sender=CARER, auth="fail")
    [item] = await rows(idb, "SELECT * FROM inbound_quarantine")
    token = "a" * 43
    recipient = inbound_quarantine.recipient_hash(CARER)
    await idb.insert_quarantine_token(
        patient_id=PID,
        quarantine_id=item["id"],
        action="approve",
        token_hash=inbound_quarantine.token_hash(token),
        binding_hash=inbound_quarantine._binding(
            "approve", PID, item["item_uuid"], recipient, token
        ),
        recipient_hash=recipient,
        expires_at=ago(days=-7),
    )
    neutral = (await get_page(idb, "b" * 43)).body
    assert (await get_page(idb, token)).body == neutral
    assert (await post_link(idb, token, inbound_quarantine.csrf_for(token))).body == neutral
    assert drive.files == []
    ok = await _call(
        server.api_inbound_quarantine_decide,
        _dash_request(
            idb, body={"patient_id": "synth950-a", "item_id": item["id"], "action": "approve"}
        ),
        CARER,
    )
    assert ok.status_code == 200 and len(drive.files) == 1


def test_dashboard_card_warns_about_an_unsigned_claim():
    js = (Path(server.__file__).parent / "dashboard.js").read_text()
    assert "not signed by your mail provider" in js
    assert "váš poskytovateľ pošty ju nepodpísal" in js


def test_access_log_masks_q_tokens_and_query_strings():
    from oncofiles.app_log import AccessLogFilter, attach_access_log_filter

    token = "Q" * 43
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("192.0.2.7:5000", "GET", f"/q/{token}?x=1", "1.1", 200),
        None,
    )
    assert AccessLogFilter().filter(record)
    line = record.getMessage()
    assert token not in line and "/q/[redacted]" in line and line.endswith(" 200")
    other = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("192.0.2.7:5000", "GET", f"/api/documents?patient_id={PID}&limit=5", "1.1", 200),
        None,
    )
    AccessLogFilter().filter(other)
    msg = other.getMessage()
    assert PID not in msg and "/api/documents?[redacted]" in msg
    assert len(other.args) == 5  # uvicorn's AccessFormatter unpacks five args
    attach_access_log_filter()
    attach_access_log_filter()
    flt = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, AccessLogFilter)]
    assert len(flt) == 1
    # set up where logging is set up (stderr and the #706 file alike)
    src = (Path(server.__file__)).read_text()
    setup = src[src.index("def _setup_logging") : src.index("async def lifespan")]
    assert "attach_access_log_filter()" in setup


async def test_notice_carries_the_recognisable_brevo_tag(idb, drive, mails):
    await quarantine_one(idb)
    [note] = notices(mails)
    assert note["tags"] == [inbound_quarantine.QUARANTINE_MAIL_TAG] == ["inbound_quarantine"]


async def test_link_token_is_given_back_when_the_decision_raises(idb, drive, mails, monkeypatch):
    approve, _reject = await _tokens(idb, mails)
    csrf = csrf_of((await get_page(idb, approve)).body)

    async def _boom(*a, **k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(inbound_quarantine, "approve", _boom)
    with pytest.raises(RuntimeError):
        await inbound_quarantine.act_on_link(idb, approve, csrf)
    [tok] = await rows(
        idb, "SELECT used_at FROM inbound_quarantine_tokens WHERE action = 'approve'"
    )
    assert tok["used_at"] is None


async def test_reject_after_a_partial_approve_reports_what_was_filed(idb, drive, mails):
    from oncofiles import dashboard_upload

    files = [(f"Part{i}.pdf", pdf(600 + i), "application/pdf") for i in range(2)]
    await quarantine_one(idb, files=files)
    [item] = await rows(idb, "SELECT id FROM inbound_quarantine")
    real = dashboard_upload.handle_upload
    calls = {"n": 0}

    async def _second_fails(db, pid, uploads, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            return dashboard_upload.refusal("no_drive", 409)
        return await real(db, pid, uploads, **kw)

    import oncofiles.dashboard_upload as du

    du.handle_upload, saved = _second_fails, du.handle_upload
    try:
        first = await inbound_quarantine.approve(
            idb, PID, item["id"], decided_by="h", via="dashboard"
        )
    finally:
        du.handle_upload = saved
    assert first["result"] == "retry_later" and first["filed"] == 1
    out = await inbound_quarantine.reject(idb, PID, item["id"], decided_by="h", via="dashboard")
    assert out == {"result": "rejected", "already_filed": 1}


def test_safe_sender_drops_url_shaping_characters():
    assert inbound_quarantine.safe_sender("a/b?c=d&e%f@example.test") == "abcdef@example.test"


# ── trash and restore (maintainer decision 2026-11-22) ───────────────────


async def _rejected_item(idb, mails):
    await quarantine_one(idb)
    [item] = await rows(idb, "SELECT id FROM inbound_quarantine")
    await inbound_quarantine.reject(idb, PID, item["id"], decided_by="h", via="dashboard")
    return item["id"]


def _restore_body(pid, item_id):
    return {"patient_id": pid, "item_id": item_id, "action": "restore"}


async def test_restore_within_thirty_days_goes_back_to_pending(idb, drive, mails):
    item_id = await _rejected_item(idb, mails)
    view = await inbound_quarantine.dashboard_view(idb, PID, caregiver=True)
    [trashed] = view["trash"]
    assert trashed["state"] == "rejected" and trashed["days_left"] == 30
    route = server.api_inbound_quarantine_decide
    ok = await _call(route, _dash_request(idb, body=_restore_body("synth950-a", item_id)), CARER)
    assert ok.status_code == 200 and json.loads(ok.body)["result"] == "restored"
    again = await _call(route, _dash_request(idb, body=_restore_body("synth950-a", item_id)), CARER)
    assert json.loads(again.body)["result"] == "already"  # idempotent
    [row] = await rows(idb, "SELECT state, restored_at FROM inbound_quarantine")
    assert row["state"] == "pending" and row["restored_at"]
    # no new e-mail link, and every old one is void: decide in the dashboard
    assert len(notices(mails)) == 1
    tokens = await rows(idb, "SELECT used_at FROM inbound_quarantine_tokens")
    assert all(t["used_at"] for t in tokens)
    approve = links(notices(mails)[0]["text"])[0]
    assert b'name="csrf"' not in (await get_page(idb, approve)).body
    [entry] = await rows(
        idb, "SELECT * FROM activity_log WHERE tool_name = 'inbound_quarantine_restore'"
    )
    assert entry["patient_id"] == PID


async def test_restore_of_a_foreign_patients_item_is_unknown(idb, drive, mails):
    item_id = await _rejected_item(idb, mails)
    route = server.api_inbound_quarantine_decide
    foreign = await _call(
        route, _dash_request(idb, body=_restore_body("synth950-a", item_id)), OTHER_CARER
    )
    unknown = await _call(
        route, _dash_request(idb, body=_restore_body("synth950-zz", item_id)), OTHER_CARER
    )
    assert foreign.status_code == unknown.status_code == 404 and foreign.body == unknown.body
    admin = await _call(
        route,
        _dash_request(idb, body=_restore_body("synth950-a", item_id)),
        "admin950@example.test",
        admin=True,
    )
    assert admin.status_code == 403
    [row] = await rows(idb, "SELECT state FROM inbound_quarantine")
    assert row["state"] == "rejected"


async def test_the_q_link_cannot_restore(idb, drive, mails):
    approve, reject = await _tokens(idb, mails)
    csrf = csrf_of((await get_page(idb, reject)).body)
    await post_link(idb, reject, csrf)
    neutral = (await get_page(idb, "r" * 43)).body
    for token in (approve, reject):
        assert (await get_page(idb, token)).body == neutral
        assert (await post_link(idb, token, inbound_quarantine.csrf_for(token))).body == neutral
    [row] = await rows(idb, "SELECT state FROM inbound_quarantine")
    assert row["state"] == "rejected"
    # the link table only knows approve / reject
    from oncofiles.database._inbound_quarantine import QUARANTINE_ACTIONS

    assert QUARANTINE_ACTIONS == ("approve", "reject")


async def test_restore_after_the_trash_period_is_refused(idb, drive, mails):
    item_id = await _rejected_item(idb, mails)
    await idb.db.execute("UPDATE inbound_quarantine SET decided_at = ?", (ago(days=31),))
    await idb.db.commit()
    out = await inbound_quarantine.restore(idb, PID, item_id, decided_by="h")
    assert out == {"result": "not_restorable", "state": "rejected"}
    view = await inbound_quarantine.dashboard_view(idb, PID, caregiver=True)
    assert view["trash"] == []


async def test_a_restored_expired_item_does_not_expire_again_at_once(idb, drive, mails):
    await quarantine_one(idb)
    await idb.db.execute("UPDATE inbound_quarantine SET received_at = ?", (ago(days=40),))
    await idb.db.commit()
    await inbound_quarantine.run_maintenance(idb)
    [item] = await rows(idb, "SELECT id, state FROM inbound_quarantine")
    assert item["state"] == "expired"
    assert (await inbound_quarantine.restore(idb, PID, item["id"], decided_by="h"))[
        "result"
    ] == "restored"
    await inbound_quarantine.run_maintenance(idb)
    [after] = await rows(idb, "SELECT state FROM inbound_quarantine")
    assert after["state"] == "pending"


def test_demo_twin_has_a_trash_entry():
    from oncofiles import demo_dashboard

    built = demo_dashboard.build([], "0.0.0-test")
    [trashed] = built["inbound_quarantine"]["trash"]
    assert trashed["state"] == "rejected" and trashed["days_left"] > 0
    js = (Path(server.__file__).parent / "dashboard.js").read_text()
    assert "restoreQuarantine: 1" in js and "'Kôš (' + trash.length" in js
