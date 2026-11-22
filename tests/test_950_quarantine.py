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
    assert "could not be verified" in note["text"]


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


async def test_global_holding_limit_applies(idb, drive, mails, monkeypatch):
    from oncofiles import holding_store

    monkeypatch.setattr(holding_store, "HOLDING_MAX_BYTES", 1000)
    assert (await quarantine_one(idb)).outcome == "unmatched"
    monkeypatch.setattr(holding_store, "HOLDING_MAX_BYTES", 10**12)
    monkeypatch.setattr(inbound_quarantine, "_free_bytes", lambda: 1)
    assert (await quarantine_one(idb)).outcome == "unmatched"
    assert await rows(idb, "SELECT * FROM inbound_quarantine") == []


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
