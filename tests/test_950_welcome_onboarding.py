# ruff: noqa: F811 - the #950 fixtures are imported and used as test arguments
"""#950 phase 2: onboarding by e-mail via welcome@ (Flow B).

Covers: the onboarding gate (closed / invite / open), non-Gmail and failed
auth listed only, one trial patient per sender with its by_sending consent
row, the encrypted holding store (mandatory key, owner-only files, no
plaintext on disk), the local-only reading, previews of held documents, the
welcome reply (no names or file names, the terms sentence, the sign-in link
and the personal address) and its loop protection, the caps, the dashboard
ACL and first-steps status, and that nothing is deleted.

Synthetic only: invented Gmail local parts and example.test addresses,
invented file names and bytes, dates in 2027. No network (attachment
downloads are served from a dict, the model is a stub).
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.responses import JSONResponse

from oncofiles import (
    holding_extract,
    holding_store,
    inbound_email,
    inbound_queue,
    inbound_welcome,
    server,
)
from oncofiles.curated.extract import Extraction
from oncofiles.email_identity import canonical_email
from tests.test_950_inbound_address import (  # noqa: F401 - fixtures
    CARER,
    PID,
    SUBJECT,
    _env,
    brevo,
    brevo_item,
    brevo_request,
    drive,
    idb,
    jpeg,
    mails,
    parsed,
    pdf,
)
from tests.test_950_inbound_address import mail as raw_mail

NEW = "synthetic.welcome950@gmail.com"
NEW2 = "second.welcome950@gmail.com"
NOT_GMAIL = "someone.welcome950@example.test"
WELCOME = "welcome@docs.oncofiles.com"
FILE_A = "SynthWelcomeLab2027.pdf"
FILE_B = "SynthWelcomePhoto2027.jpg"
FILES = ("w-a", "w-b")
HOLD_SECRET = "synthetic-holding-secret-950"
PAGE_TEXT = "<!-- page 1 -->\nInvented result 2027-05-04\n<!-- page 2 -->\nSecond invented page"


@pytest.fixture(autouse=True)
def welcome_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOLDING_STORE_SECRET", HOLD_SECRET)
    monkeypatch.setattr("oncofiles.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("oncofiles.config.ONBOARDING_MODE", "open")
    monkeypatch.setattr("oncofiles.config.ONBOARDING_INVITE_EMAILS", frozenset())
    monkeypatch.setattr("oncofiles.config.DASHBOARD_ADMIN_EMAILS", [])
    monkeypatch.delenv("CURATED_DAILY_PAGE_BUDGET", raising=False)
    inbound_welcome.reset_state()
    holding_extract._drain_task = None
    inbound_email._BACKGROUND.clear()  # tasks of an earlier test's (closed) loop
    yield tmp_path
    inbound_welcome.reset_state()
    holding_extract._drain_task = None
    inbound_email._BACKGROUND.clear()  # never left to a later test's loop


@pytest.fixture
def model(monkeypatch):
    """The local model: configured, and a stub extractor that records calls."""
    calls: list[dict] = []

    def _extract(name, content, mime_type="", **kw):
        from oncofiles.cloud_guard import CloudForbiddenError, check_cloud_allowed, local_only_job

        refused = False
        try:
            check_cloud_allowed("synthetic cloud call")
        except CloudForbiddenError:
            refused = True
        calls.append(
            {"name": name, "bytes": content, "mime": mime_type, "zone": local_only_job(),
             "cloud_refused": refused}
        )  # fmt: skip
        return Extraction(PAGE_TEXT, "vision", 2, "synthetic-local")

    monkeypatch.setattr("oncofiles.curated.llm.configured", lambda: True)
    monkeypatch.setattr("oncofiles.curated.extract.extract", _extract)
    return calls


def welcome_item(sender=NEW, files=(), auth="pass", to=WELCOME, headers=None, mid=None):
    item = brevo_item(to, sender=sender, files=files, auth=auth, headers=headers)
    if mid:
        item["MessageId"] = mid
    return item


def two_files(store):
    store["w-a"] = pdf(950)
    store["w-b"] = jpeg(30_000)
    return [
        (FILE_A, "application/pdf", len(store["w-a"]), "w-a"),
        (FILE_B, "image/jpeg", len(store["w-b"]), "w-b"),
    ]


async def drain() -> None:
    """Wait for this loop's inbound background work (mail queue, holding reader,
    replies); tasks of an earlier test's loop are ignored."""
    import asyncio

    loop = asyncio.get_running_loop()
    for _ in range(200):
        pending = [t for t in inbound_email._BACKGROUND if t.get_loop() is loop and not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending)
    raise AssertionError("inbound background work did not settle")


async def send(db, *items):
    resp = await server.inbound_brevo_webhook(brevo_request(db, {"items": list(items)}))
    assert resp.status_code == 202
    await drain()
    return resp


async def trials(db):
    async with db.db.execute("SELECT * FROM trial_accounts ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def held_docs(db, pid):
    async with db.db.execute(
        "SELECT * FROM documents WHERE patient_id = ? AND file_id LIKE 'holding:%' ORDER BY id",
        (pid,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ── gate ────────────────────────────────────────────────────────────────


async def test_open_gate_creates_a_trial_and_holds_the_files(idb, mails, brevo, model):
    store, fetched = brevo
    await send(idb, welcome_item(files=two_files(store)))
    [trial] = await trials(idb)
    pid = trial["patient_id"]
    patient = await idb.get_patient(pid)
    assert patient.caregiver_email == canonical_email(NEW)
    assert patient.display_name == inbound_welcome.TRIAL_DISPLAY_NAME  # no name from the mail
    assert trial["source"] == "welcome_email" and trial["sender_hash"] != NEW
    assert fetched == ["w-a", "w-b"]
    docs = await held_docs(idb, pid)
    assert [d["filename"] for d in docs] == [FILE_A, FILE_B]
    assert [d["mime_type"] for d in docs] == ["application/pdf", "image/jpeg"]
    # read at once, locally
    assert len(model) == 2 and all(c["zone"] == holding_extract.JOB for c in model)
    pages = await idb.get_ocr_pages(docs[0]["id"], patient_id=pid)
    assert [p["page_number"] for p in pages] == [1, 2]
    assert pages[0]["model"] == holding_extract.HOLDING_MODEL
    meta = json.loads(docs[0]["structured_metadata"])
    assert meta["source"] == "holding" and meta["holding"]["extract"] == "done"


async def test_consent_row_is_by_sending_with_a_keyed_hash(idb, mails, brevo, model):
    from oncofiles.patient_consent import CONSENT_VERSION
    from oncofiles.secrets_keys import caller_email_hash

    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    [trial] = await trials(idb)
    [consent] = await idb.get_patient_consents(patient_id=trial["patient_id"])
    assert consent["kind"] == "by_sending" and consent["consent_version"] == CONSENT_VERSION
    canon = canonical_email(NEW)
    assert consent["caller_email_hash"] == caller_email_hash(canon) == trial["sender_hash"]
    assert NEW not in json.dumps(consent) and consent["is_admin"] == 0


async def test_one_trial_per_sender(idb, mails, brevo, model):
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store), mid="<w-1@example.test>"))
    store["w-c"] = pdf(951)
    files = [("SynthSecond2027.pdf", "application/pdf", len(store["w-c"]), "w-c")]
    await send(idb, welcome_item(files=files, mid="<w-2@example.test>"))
    [trial] = await trials(idb)
    assert len(await held_docs(idb, trial["patient_id"])) == 3
    everyone = await idb.list_patients(active_only=False)
    patients = [p for p in everyone if p.caregiver_email == canonical_email(NEW)]
    assert len(patients) == 1


async def test_closed_gate_replies_once_with_the_waitlist_and_keeps_nothing(
    idb, mails, brevo, model, monkeypatch
):
    monkeypatch.setattr("oncofiles.config.ONBOARDING_MODE", "closed")
    store, fetched = brevo
    await send(idb, welcome_item(files=two_files(store), mid="<c-1@example.test>"))
    await send(idb, welcome_item(files=two_files(store), mid="<c-2@example.test>"))
    assert await trials(idb) == [] and fetched == [] and model == []
    assert len(mails) == 1 and mails[0]["to"] == [NEW]
    assert inbound_welcome.WAITLIST_URL in mails[0]["text"]
    assert mails[0]["headers"] == {"Auto-Submitted": "auto-replied"}
    for name in (FILE_A, FILE_B, SUBJECT, NEW):
        assert name not in mails[0]["text"]
    async with idb.db.execute("SELECT COUNT(*) FROM inbound_jobs") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_invite_mode_admits_only_invited_senders(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr("oncofiles.config.ONBOARDING_MODE", "invite")
    monkeypatch.setattr("oncofiles.config.ONBOARDING_INVITE_EMAILS", frozenset({NEW2}))
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))  # NEW: not invited
    assert await trials(idb) == []
    assert len(mails) == 1 and inbound_welcome.WAITLIST_URL in mails[0]["text"]
    await send(idb, welcome_item(sender=NEW2, files=two_files(store)))
    [trial] = await trials(idb)
    assert (await idb.get_patient(trial["patient_id"])).caregiver_email == canonical_email(NEW2)


@pytest.mark.parametrize(
    ("sender", "auth", "reason"),
    [(NOT_GMAIL, "pass", "welcome_not_gmail"), (NEW, "fail", "welcome_auth_failed")],
)
async def test_non_gmail_and_failed_auth_are_listed_only(
    idb, mails, brevo, model, sender, auth, reason
):
    store, fetched = brevo
    await send(idb, welcome_item(sender=sender, auth=auth, files=two_files(store)))
    assert await trials(idb) == [] and fetched == [] and mails == [] and model == []
    [entry] = inbound_email.unmatched_overview()["senders"]
    assert entry["reasons"] == {reason: 1} and entry["files"] == 2
    assert sender not in json.dumps(inbound_email.unmatched_overview())


async def test_existing_caregiver_with_one_patient_is_filed_like_phase_one(
    idb, mails, brevo, model, drive
):
    """Maintainer decision: welcome@ from the caregiver of exactly one patient
    goes through the phase 1 core into that patient's inbox, with the receipt."""
    store, fetched = brevo
    await send(idb, welcome_item(sender=CARER, files=two_files(store)))
    assert await trials(idb) == [] and model == []
    assert fetched == ["w-a", "w-b"]
    assert [f[0] for f in drive.files] == [FILE_A, FILE_B]  # dashboard_upload.handle_upload
    [reply] = mails
    assert reply["to"] == [CARER]
    assert reply["text"] == inbound_email.receipt_text(len(FILES), Counter())[1]  # phase 1 receipt
    async with idb.db.execute("SELECT patient_id FROM inbound_jobs") as cur:
        assert [r[0] for r in await cur.fetchall()] == [PID]


async def test_existing_caregiver_without_drive_gets_no_drive(idb, mails, brevo, model):
    store, fetched = brevo
    await send(idb, welcome_item(sender=CARER, files=two_files(store)))
    assert await trials(idb) == []
    [reply] = mails
    assert "no_drive" in reply["text"]  # the phase 1 refusal, reported in the receipt


async def test_existing_caregiver_of_several_patients_is_listed_only(idb, mails, brevo, model):
    await idb.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) VALUES (?,?,?,?)",
        ("00000000-0000-4000-8000-000000000956", "synth956-c", "Cyril Synthetic", CARER),
    )
    await idb.db.commit()
    store, fetched = brevo
    await send(idb, welcome_item(sender=CARER, files=two_files(store)))
    assert await trials(idb) == [] and fetched == [] and mails == []
    overview = inbound_email.unmatched_overview()
    [entry] = overview["senders"]
    assert entry["reasons"] == {"caregiver_multiple_patients": 1}
    assert entry["patient"] == "" and CARER not in json.dumps(overview)


async def test_existing_caregiver_with_failed_auth_is_not_filed(idb, mails, brevo, model, drive):
    store, fetched = brevo
    await send(idb, welcome_item(sender=CARER, auth="fail", files=two_files(store)))
    assert fetched == [] and drive.files == [] and mails == []


def test_only_the_docs_subdomain_is_a_welcome_address():
    assert frozenset({"welcome@docs.oncofiles.com"}) == inbound_welcome.WELCOME_ADDRESSES
    ok = inbound_email.InboundMessage(
        recipients=["Welcome+x@Docs.Oncofiles.com"], sender="", auth_pass=True
    )
    assert inbound_welcome.is_welcome(ok)
    for other in ("welcome@oncofiles.com", "welcome@docs.oncofiles.com.example.test"):
        assert not inbound_welcome.is_welcome(
            inbound_email.InboundMessage(recipients=[other], sender="", auth_pass=True)
        )


async def test_loop_mail_to_welcome_is_ignored(idb, mails, brevo, model):
    store, fetched = brevo
    item = welcome_item(files=two_files(store), headers={"Auto-Submitted": "auto-replied"})
    await send(idb, item)
    assert await trials(idb) == [] and fetched == [] and mails == []


async def test_raw_mime_route_admits_welcome_too(idb, mails, model):
    msg = raw_mail(WELCOME, sender=NEW, files=[(FILE_A, pdf(952), "application/pdf")])
    result = await inbound_email.process_message(idb, parsed(msg))
    await drain()
    assert result.outcome == "filed" and result.accepted == 1
    [trial] = await trials(idb)
    assert len(await held_docs(idb, trial["patient_id"])) == 1


# ── the reply ───────────────────────────────────────────────────────────


async def test_welcome_reply_has_counts_terms_sign_in_and_the_address(idb, mails, brevo, model):
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    [trial] = await trials(idb)
    address = (await inbound_email.ensure_address(idb, trial["patient_id"]))["address"]
    [reply] = mails
    text = reply["text"]
    assert reply["to"] == [NEW] and reply["headers"] == {"Auto-Submitted": "auto-replied"}
    assert "prijali sme 2 dokument" in text and "we received 2 document" in text
    assert "By sending them you agreed to the terms" in text
    assert inbound_email.TERMS_URL in text
    assert "https://oncofiles.com/dashboard" in text
    assert address in text and address.endswith("@" + inbound_email.INBOUND_DOMAIN)
    for leak in (FILE_A, FILE_B, SUBJECT, inbound_welcome.TRIAL_DISPLAY_NAME, "token="):
        assert leak not in text


async def test_reply_at_most_once_per_ten_minutes(idb, mails, brevo, model):
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store), mid="<r-1@example.test>"))
    store["w-c"] = pdf(953)
    files = [("SynthThird2027.pdf", "application/pdf", len(store["w-c"]), "w-c")]
    await send(idb, welcome_item(files=files, mid="<r-2@example.test>"))
    assert len(mails) == 1
    assert inbound_email.unmatched_overview()["totals"]["reply_throttled"] == 1


# ── caps ────────────────────────────────────────────────────────────────


async def test_per_sender_mail_cap(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr(inbound_welcome, "MAX_MAILS_PER_SENDER_DAY", 1)
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store), mid="<m-1@example.test>"))
    await send(idb, welcome_item(files=two_files(store), mid="<m-2@example.test>"))
    [trial] = await trials(idb)
    assert len(await held_docs(idb, trial["patient_id"])) == 2 and len(mails) == 1
    assert inbound_email.unmatched_overview()["totals"]["welcome_sender_capped"] == 1


async def test_per_sender_file_cap(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr(inbound_welcome, "MAX_FILES_PER_SENDER_DAY", 1)
    store, fetched = brevo
    await send(idb, welcome_item(files=two_files(store)))
    assert await trials(idb) == [] and fetched == [] and mails == []
    assert inbound_email.unmatched_overview()["totals"]["welcome_sender_capped"] == 1


async def test_global_new_trial_cap(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr(inbound_welcome, "MAX_NEW_TRIALS_PER_DAY", 1)
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    await send(idb, welcome_item(sender=NEW2, files=two_files(store)))
    assert len(await trials(idb)) == 1 and len(mails) == 1
    assert inbound_email.unmatched_overview()["totals"]["welcome_trial_cap"] == 1


# ── holding store ───────────────────────────────────────────────────────


async def test_holding_files_are_encrypted_and_owner_only(idb, mails, brevo, model, welcome_env):
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    folder = welcome_env / "holding"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    files = sorted(folder.iterdir())
    assert len(files) == 2 and all(f.suffix == ".bin" for f in files)
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        raw = f.read_bytes()
        assert store["w-a"][:40] not in raw and store["w-b"][:40] not in raw
        assert b"%PDF" not in raw and FILE_A.encode() not in raw
    [trial] = await trials(idb)
    docs = await held_docs(idb, trial["patient_id"])
    assert holding_store.read(docs[0]["file_id"], trial["patient_id"]) == store["w-a"]
    with pytest.raises(holding_store.HoldingStoreError):  # bound to its patient
        holding_store.read(docs[0]["file_id"], PID)


def test_holding_key_is_mandatory(monkeypatch, welcome_env):
    from oncofiles.secrets_keys import HoldingStoreSecretMissingError

    file_id = holding_store.write_encrypted(b"invented bytes 2027", PID)
    monkeypatch.delenv("HOLDING_STORE_SECRET")
    assert holding_store.configured() is False
    with pytest.raises(HoldingStoreSecretMissingError):
        holding_store.write_encrypted(b"invented bytes 2027", PID)
    with pytest.raises(HoldingStoreSecretMissingError):
        holding_store.read(file_id, PID)
    assert len(list((welcome_env / "holding").iterdir())) == 1  # nothing written without it


def test_wrong_key_or_moved_file_does_not_decrypt(monkeypatch, welcome_env):
    a = holding_store.write_encrypted(b"invented A 2027", PID)
    b = holding_store.write_encrypted(b"invented B 2027", PID)
    folder = welcome_env / "holding"
    uid_a = a.removeprefix(holding_store.HOLDING_PREFIX)
    uid_b = b.removeprefix(holding_store.HOLDING_PREFIX)
    os.replace(folder / f"{uid_a}.bin", folder / f"{uid_b}.bin")  # a's bytes under b's id
    with pytest.raises(holding_store.HoldingStoreError):
        holding_store.read(b, PID)
    c = holding_store.write_encrypted(b"invented C 2027", PID)
    monkeypatch.setenv("HOLDING_STORE_SECRET", "another-synthetic-secret")
    with pytest.raises(holding_store.HoldingStoreError):
        holding_store.read(c, PID)


async def test_no_trial_without_the_holding_key(idb, mails, brevo, model, monkeypatch):
    monkeypatch.delenv("HOLDING_STORE_SECRET")
    store, fetched = brevo
    await send(idb, welcome_item(files=two_files(store)))
    assert await trials(idb) == [] and fetched == [] and mails == []


# ── local-only reading ──────────────────────────────────────────────────


async def test_reading_is_local_only_and_never_sees_the_file_name(idb, mails, brevo, model):
    from oncofiles.curated.indexer import CuratedCloudError, refuse_cloud

    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    assert model and all(c["cloud_refused"] for c in model)
    assert [c["name"] for c in model] == ["holding.pdf", "holding.jpg"]
    [trial] = await trials(idb)
    pid = trial["patient_id"]
    docs = await held_docs(idb, pid)
    with pytest.raises(CuratedCloudError):
        refuse_cloud(docs[0]["id"], "synthetic cloud function")
    assert await idb.get_documents_without_ai(patient_id=pid) == []
    assert docs[0]["id"] in await idb.curated_document_ids()


async def test_documents_wait_while_no_local_model(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr("oncofiles.curated.llm.configured", lambda: False)
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    [trial] = await trials(idb)
    docs = await held_docs(idb, trial["patient_id"])
    assert model == []
    assert {json.loads(d["structured_metadata"])["holding"]["extract"] for d in docs} == {"pending"}
    monkeypatch.setattr("oncofiles.curated.llm.configured", lambda: True)
    counts = await holding_extract.run_pending(idb)
    assert counts == {"done": 2}


async def test_daily_page_budget_holds_the_patient(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setenv("CURATED_DAILY_PAGE_BUDGET", "2")
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    # the first document used the 2 pages of the day; the second waits
    assert len(model) == 1
    [trial] = await trials(idb)
    states = [
        json.loads(d["structured_metadata"])["holding"]["extract"]
        for d in await held_docs(idb, trial["patient_id"])
    ]
    assert states == ["done", "pending"]


# ── previews (#948) ─────────────────────────────────────────────────────


def _tiny_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=210, height=297)
    page.insert_text((20, 40), "Invented held sheet 2027-06-01")
    data = doc.tobytes()
    doc.close()
    return data


def _thumb_request(db, ids):
    request = MagicMock()
    request.query_params = {"ids": ",".join(str(i) for i in ids)}
    request.path_params = {}
    request.headers = {"authorization": "Bearer session:synthetic"}
    request.app.state.fastmcp_server._lifespan_result = {"db": db, "files": None}
    return request


@contextmanager
def _caller(email):
    unauthorized = JSONResponse({"error": "unauthorized"}, status_code=401)
    with (
        patch.object(server, "_get_dashboard_email", return_value=email),
        patch.object(server, "_check_bearer", return_value=unauthorized),
        patch.object(server, "_is_admin_email", return_value=False),
    ):
        yield


async def _trial_with_pdf(db, monkeypatch, brevo):
    store, _ = brevo
    store["w-pdf"] = _tiny_pdf()
    files = [(FILE_A, "application/pdf", len(store["w-pdf"]), "w-pdf")]
    await send(db, welcome_item(files=files))
    [trial] = await trials(db)
    [doc] = await held_docs(db, trial["patient_id"])
    return trial["patient_id"], doc["id"]


async def test_holding_documents_get_previews_from_the_held_bytes(
    idb, mails, brevo, model, monkeypatch
):
    from oncofiles import doc_thumbnails

    doc_thumbnails._cache.clear()
    monkeypatch.setattr("oncofiles.tools.curated.run_active", lambda: False)
    monkeypatch.setattr(server, "GOOGLE_INTEGRATION_ENABLED", False)  # no Google needed
    clients = MagicMock(side_effect=AssertionError("no Drive client for a held file"))
    monkeypatch.setattr(server, "_create_patient_clients", clients)
    _pid, doc_id = await _trial_with_pdf(idb, monkeypatch, brevo)
    with _caller(NEW):
        resp = await server.api_doc_thumbnails(_thumb_request(idb, [doc_id]))
    body = json.loads(resp.body)["thumbnails"]
    assert body[str(doc_id)]["src"].startswith("data:image/jpeg;base64,")
    # another user never sees it (#590: the same null as an unknown id)
    doc_thumbnails._cache.clear()
    with _caller("other.person950@example.test"):
        resp = await server.api_doc_thumbnails(_thumb_request(idb, [doc_id]))
    assert json.loads(resp.body)["thumbnails"] == {str(doc_id): None}
    doc_thumbnails._cache.clear()


# ── dashboard: sign-in lands on the trial, first steps, ACL ─────────────


def _dash_request(db, query=None):
    request = MagicMock()
    request.query_params = query or {}
    request.path_params = {}
    request.headers = {"authorization": "Bearer session:synthetic"}
    request.app.state.fastmcp_server._lifespan_result = {"db": db, "files": None}
    return request


async def test_sign_in_lands_on_the_trial_and_others_never_see_it(idb, mails, brevo, model):
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    [trial] = await trials(idb)
    pid = trial["patient_id"]
    with _caller(NEW.upper()):  # Google may hand back another case
        own = json.loads((await server.api_list_patients(_dash_request(idb))).body)["own"]
    assert [p["patient_id"] for p in own] == [pid]  # consent exists by sending
    with _caller(NEW):
        status = json.loads((await server.api_trial_status(_dash_request(idb))).body)
    assert status["trial"] is True and status["days_left"] == inbound_welcome.TRIAL_DAYS
    assert (status["documents"], status["ready"], status["reading"]) == (len(FILES), len(FILES), 0)
    assert status["drive_connected"] is False
    assert NEW not in json.dumps(status) and FILE_A not in json.dumps(status)
    with _caller("other.person950@example.test"):
        own = json.loads((await server.api_list_patients(_dash_request(idb))).body)["own"]
        named = await server.api_trial_status(_dash_request(idb, {"patient_id": pid}))
    assert pid not in json.dumps(own)
    assert json.loads(named.body) == {"trial": False}


async def test_ordinary_patient_has_no_trial(idb):
    with _caller(CARER):
        resp = await server.api_trial_status(_dash_request(idb))
    assert json.loads(resp.body) == {"trial": False}


def test_dashboard_has_the_first_steps_card_and_the_demo_twin():
    from oncofiles import demo_dashboard

    html = (Path(server.__file__).parent / "dashboard.html").read_text()
    js = (Path(server.__file__).parent / "dashboard.js").read_text()
    assert 'id="trial-card"' in html and 'data-action="showShareModal"' in html
    assert 'data-action="showGdriveSetup"' in html
    assert "'/api/trial-status'" in js and "d.trial_status" in js
    assert demo_dashboard.build([], "0.0.0-test")["trial_status"] == {"trial": False}


# ── logs and deletion ───────────────────────────────────────────────────


async def test_logs_carry_no_address_or_file_name(idb, mails, brevo, model, caplog):
    caplog.set_level(logging.DEBUG, logger="oncofiles")
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    await send(idb, welcome_item(sender=NOT_GMAIL, files=two_files(store)))
    text = caplog.text
    for leak in (NEW, NOT_GMAIL, FILE_A, FILE_B, SUBJECT, "synthetic.welcome950"):
        assert leak not in text


async def test_nothing_is_deleted(idb, mails, brevo, model, welcome_env):
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    await holding_extract.run_pending(idb)
    await inbound_queue.run_pending(idb)
    [trial] = await trials(idb)
    docs = await held_docs(idb, trial["patient_id"])
    assert len(docs) == 2 and all(d["deleted_at"] is None for d in docs)
    assert len(list((welcome_env / "holding").glob("*.bin"))) == 2
    src = (Path(server.__file__).parent / "holding_store.py").read_text()
    for call in ("unlink(", "os.remove(", "rmtree(", "DELETE FROM"):
        assert call not in src


# ── security review: Gmail identity, held-render deadline ───────────────

CANON = "syntheticwelcome950@gmail.com"
VARIANTS = (
    "synthetic.welcome950+x@gmail.com",
    "SyntheticWelcome950@gmail.com",
    "synthetic.welcome950@googlemail.com",
)


async def test_gmail_variants_are_one_sender_and_one_trial(idb, mails, brevo, model):
    from oncofiles.secrets_keys import caller_email_hash

    store, _ = brevo
    for i, variant in enumerate(VARIANTS):
        store[f"v-{i}"] = pdf(960 + i)
        files = [(f"SynthVariant{i}.pdf", "application/pdf", len(store[f"v-{i}"]), f"v-{i}")]
        await send(idb, welcome_item(sender=variant, files=files, mid=f"<v-{i}@example.test>"))
    [trial] = await trials(idb)
    assert trial["sender_hash"] == caller_email_hash(CANON)
    assert (await idb.get_patient(trial["patient_id"])).caregiver_email == CANON
    assert len(await held_docs(idb, trial["patient_id"])) == len(VARIANTS)


async def test_gmail_variants_share_one_cap_bucket(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr(inbound_welcome, "MAX_MAILS_PER_SENDER_DAY", 1)
    store, _ = brevo
    await send(idb, welcome_item(sender=VARIANTS[0], files=two_files(store), mid="<cb-1@x.test>"))
    await send(idb, welcome_item(sender=VARIANTS[2], files=two_files(store), mid="<cb-2@x.test>"))
    assert inbound_email.unmatched_overview()["totals"]["welcome_sender_capped"] == 1
    [trial] = await trials(idb)
    assert len(await held_docs(idb, trial["patient_id"])) == len(FILES)


async def test_gmail_variant_of_an_existing_caregiver_gets_no_trial(
    idb, mails, brevo, model, drive
):
    store, fetched = brevo
    variant = "syntheticcarer950+new@googlemail.com"  # CARER is synthetic.carer950@gmail.com
    await send(idb, welcome_item(sender=variant, files=two_files(store)))
    assert await trials(idb) == []
    assert [f[0] for f in drive.files] == [FILE_A, FILE_B]  # the caregiver's own patient


@pytest.mark.parametrize("signed_in", [CANON, *VARIANTS])
async def test_sign_in_with_any_gmail_variant_reaches_the_trial(
    idb, mails, brevo, model, signed_in
):
    store, _ = brevo
    await send(idb, welcome_item(sender=VARIANTS[0], files=two_files(store)))
    [trial] = await trials(idb)
    with _caller(signed_in):
        own = json.loads((await server.api_list_patients(_dash_request(idb))).body)["own"]
    assert [p["patient_id"] for p in own] == [trial["patient_id"]]


@pytest.mark.parametrize(
    ("signed_in", "caregiver"),
    [
        ("jana.example@example.test", "janaexample@example.test"),
        ("jana+x@example.test", "jana@example.test"),
        ("jana@googlemail.example.test", "jana@gmail.com"),
        ("janaexample@gmail.com", "jana.example@example.test"),
    ],
)
def test_non_gmail_caregiver_match_stays_exact(signed_in, caregiver):
    from oncofiles.persistent_oauth import _email_matches_caregiver

    assert _email_matches_caregiver(signed_in, caregiver) is False


def test_canonical_email():
    from oncofiles.email_identity import canonical_email

    for variant in VARIANTS:
        assert canonical_email(variant) == CANON
    assert canonical_email(" A.B+c@Example.Test ") == "a.b+c@example.test"
    assert canonical_email("not-an-address") == ""


async def make_trial(db, pid=PID, *, ends=None):
    """A trial row for an existing synthetic patient (direct, for unit tests)."""
    ends = ends or _future()
    await db.db.execute(
        "INSERT INTO trial_accounts (patient_id, sender_hash, source, trial_ends_at) "
        "VALUES (?, ?, 'welcome_email', ?)",
        (pid, f"synthetic-hash-{pid[-4:]}", ends),
    )
    await db.db.commit()


async def _held_docs_for_thumbs(db, n):
    await make_trial(db)
    ids = []
    for i in range(n):
        ids.append(
            await holding_store.store_document(
                db, PID, f"SynthHeld{i}.pdf", _tiny_pdf(), "application/pdf"
            )
        )
    return ids


async def test_held_renders_share_one_batch_deadline(idb, monkeypatch):
    import time as _time

    from oncofiles import doc_thumbnails

    doc_thumbnails._cache.clear()
    monkeypatch.setattr("oncofiles.tools.curated.run_active", lambda: False)
    monkeypatch.setattr(doc_thumbnails, "BATCH_DEADLINE_S", 0.3)
    ids = await _held_docs_for_thumbs(idb, 3)
    calls = []

    def slow(file_id, patient_id, mime, size):
        calls.append(file_id)
        _time.sleep(0.5)  # longer than the whole batch deadline
        return doc_thumbnails.Outcome(b"\xff\xd8\xffsynthetic", "image/jpeg", "ok")

    monkeypatch.setattr(doc_thumbnails, "_holding_blocking", slow)
    started = _time.monotonic()
    with _caller(CARER):
        resp = await server.api_doc_thumbnails(_thumb_request(idb, ids))
    elapsed = _time.monotonic() - started
    assert json.loads(resp.body)["thumbnails"] == {str(i): None for i in ids}
    assert len(calls) == 1 and elapsed < 0.45  # one render started, the batch ended on time
    assert len(doc_thumbnails._cache) == 0  # nothing cached for the late ids
    doc_thumbnails._cache.clear()


async def test_held_renders_are_capped_per_request(idb, monkeypatch):
    from oncofiles import doc_thumbnails

    doc_thumbnails._cache.clear()
    monkeypatch.setattr("oncofiles.tools.curated.run_active", lambda: False)
    n = doc_thumbnails.MAX_HOLDING_RENDERS_PER_REQUEST + 2
    ids = await _held_docs_for_thumbs(idb, n)
    monkeypatch.setattr(
        doc_thumbnails,
        "_holding_blocking",
        lambda f, p, m, s: doc_thumbnails.Outcome(b"\xff\xd8\xffsynthetic", "image/jpeg", "ok"),
    )
    with _caller(CARER):
        resp = await server.api_doc_thumbnails(_thumb_request(idb, ids))
    body = json.loads(resp.body)["thumbnails"]
    shown = [i for i in ids if body[str(i)]]
    assert shown == ids[: doc_thumbnails.MAX_HOLDING_RENDERS_PER_REQUEST]
    assert len(doc_thumbnails._cache) == doc_thumbnails.MAX_HOLDING_RENDERS_PER_REQUEST
    doc_thumbnails._cache.clear()


# ── infra review: disk limits, shared model, trial end, claims ──────────


def _bins(folder) -> list:
    return list((folder / "holding").glob("*.bin")) if (folder / "holding").exists() else []


async def test_byte_cap_is_hard_even_when_the_file_caps_allow(idb, monkeypatch, welcome_env):
    """Guardrail (a): 100 small files fit, one large file over the byte cap is
    refused before anything is written."""
    await make_trial(idb)
    monkeypatch.setattr(holding_store, "TRIAL_MAX_BYTES", 100 * 64 + 100)
    for i in range(100):
        await holding_store.store_document(
            idb, PID, f"SynthSmall{i}.txt", b"x" * 63 + bytes([i]), "text/plain"
        )
    assert len(_bins(welcome_env)) == 100
    with pytest.raises(holding_store.HoldingRefusedError) as refused:
        await holding_store.store_document(idb, PID, "SynthBig.pdf", b"y" * 500, "application/pdf")
    assert refused.value.code == "trial_bytes_cap"
    assert len(_bins(welcome_env)) == 100  # no orphan file


@pytest.mark.parametrize(
    ("limit", "code"),
    [("HOLDING_MAX_BYTES", "holding_full"), ("MIN_FREE_BYTES", "disk_low")],
)
async def test_global_and_disk_limits_refuse_before_writing(
    idb, monkeypatch, welcome_env, limit, code
):
    await make_trial(idb)
    if limit == "HOLDING_MAX_BYTES":
        monkeypatch.setattr(holding_store, "HOLDING_MAX_BYTES", 10)
    else:
        monkeypatch.setattr(holding_store, "_free_bytes", lambda: holding_store.MIN_FREE_BYTES)
    with pytest.raises(holding_store.HoldingRefusedError) as refused:
        await holding_store.store_document(idb, PID, "S.pdf", b"z" * 50, "application/pdf")
    assert refused.value.code == code and _bins(welcome_env) == []


async def test_a_refused_file_is_counted_in_the_reply(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr(holding_store, "TRIAL_MAX_BYTES", len(pdf(950)) + 10)
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    [trial] = await trials(idb)
    assert len(await held_docs(idb, trial["patient_id"])) == 1
    [reply] = mails
    assert "prijali sme 1 dokument" in reply["text"] and "(trial_bytes_cap): 1" in reply["text"]
    assert inbound_email.unmatched_overview()["totals"]["skipped_trial_bytes_cap"] == 1


async def test_personal_address_of_a_trial_gets_the_welcome_caps(
    idb, mails, brevo, model, monkeypatch
):
    monkeypatch.setattr(inbound_welcome, "MAX_MAILS_PER_SENDER_DAY", 1)
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))
    [trial] = await trials(idb)
    address = (await inbound_email.ensure_address(idb, trial["patient_id"]))["address"]
    store["p-1"] = pdf(970)
    files = [("SynthPersonal.pdf", "application/pdf", len(store["p-1"]), "p-1")]
    await send(idb, welcome_item(to=address, files=files, mid="<p-1@example.test>"))
    assert len(await held_docs(idb, trial["patient_id"])) == 2  # the second mail: capped
    assert inbound_email.unmatched_overview()["totals"]["trial_capped"] == 1
    assert len(mails) == 1


async def test_caps_survive_a_restart(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr(inbound_welcome, "MAX_MAILS_PER_SENDER_DAY", 1)
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store), mid="<s-1@example.test>"))
    inbound_email.reset_state()  # a deploy: every in-memory counter is gone
    inbound_welcome.reset_state()
    await send(idb, welcome_item(files=two_files(store), mid="<s-2@example.test>"))
    [trial] = await trials(idb)
    assert len(await held_docs(idb, trial["patient_id"])) == len(FILES)
    assert inbound_email.unmatched_overview()["totals"]["welcome_sender_capped"] == 1


@pytest.mark.parametrize("busy", ["run_active", "lm_job", "curated_pending"])
async def test_reader_leaves_the_model_to_real_patients(
    idb, mails, brevo, model, monkeypatch, busy
):
    from oncofiles.tools import curated

    monkeypatch.setattr("oncofiles.curated.llm.configured", lambda: False)
    store, _ = brevo
    await send(idb, welcome_item(files=two_files(store)))  # stored, not read yet
    monkeypatch.setattr("oncofiles.curated.llm.configured", lambda: True)
    if busy == "run_active":
        monkeypatch.setattr(curated, "run_active", lambda: True)
    elif busy == "lm_job":
        monkeypatch.setitem(curated._reocr_jobs, "synthetic", {"state": "running", "kind": "x"})
    else:
        monkeypatch.setitem(
            curated._probe_stats, "synthetic", curated._ProbeStats(pending_inbox_count=2)
        )
    counts = await holding_extract.run_pending(idb)
    assert model == [] and sum(counts.values()) == 1
    assert holding_extract.lm_busy() is not None


async def test_global_daily_page_cap(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr(holding_extract, "GLOBAL_DAILY_PAGES", 2)
    store, _ = brevo
    store["glob-a"], store["glob-b"] = jpeg(31_000), jpeg(32_000)
    files = [
        ("SynthG1.jpg", "image/jpeg", len(store["glob-a"]), "glob-a"),
        ("SynthG2.jpg", "image/jpeg", len(store["glob-b"]), "glob-b"),
    ]
    await send(idb, welcome_item(files=files))
    # the stub reports 2 vision pages: the first reading uses the day's cap
    assert len(model) == 1
    store["glob-c"] = jpeg(33_000)
    files = [("SynthG3.jpg", "image/jpeg", len(store["glob-c"]), "glob-c")]
    await send(idb, welcome_item(sender=NEW2, files=files))
    assert len(model) == 1  # another trial: the same global cap


async def test_page_count_is_checked_before_reading(idb, mails, brevo, model, monkeypatch):
    import pymupdf

    monkeypatch.setenv("CURATED_DAILY_PAGE_BUDGET", "2")
    doc = pymupdf.open()
    for i in range(3):
        doc.new_page().insert_text((20, 40), f"Invented page {i} 2027")
    three = doc.tobytes()
    doc.close()
    store, _ = brevo
    store["t-3"] = three
    await send(idb, welcome_item(files=[("SynthThree.pdf", "application/pdf", len(three), "t-3")]))
    assert model == []  # never sent to the model
    [trial] = await trials(idb)
    [held] = await held_docs(idb, trial["patient_id"])
    state = json.loads(held["structured_metadata"])["holding"]
    assert (state["extract"], state["error"]) == ("failed", "too_many_pages")


def _future() -> str:
    """A moment far ahead (trial machinery timestamp, not health data)."""
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past() -> str:
    """A moment in the past (a timestamp of the trial machinery, not health data)."""
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _end_trial(db, pid):
    await db.db.execute(
        "UPDATE trial_accounts SET trial_ends_at = ? WHERE patient_id = ?",
        (_past(), pid),
    )
    await db.db.commit()


async def test_ended_trial_takes_no_mail_and_says_sign_in(idb, mails, brevo, model):
    store, fetched = brevo
    await send(idb, welcome_item(files=two_files(store), mid="<e-1@example.test>"))
    [trial] = await trials(idb)
    pid = trial["patient_id"]
    await _end_trial(idb, pid)
    inbound_email.reset_state()  # the reply throttle of the first mail
    fetched.clear()
    store["e-2"] = pdf(980)
    files = [("SynthLate.pdf", "application/pdf", len(store["e-2"]), "e-2")]
    await send(idb, welcome_item(files=files, mid="<e-2@example.test>"))
    assert fetched == [] and len(await held_docs(idb, pid)) == 2
    assert (
        "trial has ended" in mails[-1]["text"] and inbound_welcome.SIGN_IN_URL in mails[-1]["text"]
    )
    assert mails[-1]["headers"] == {"Auto-Submitted": "auto-replied"}
    # the personal address: the file is refused as trial_ended, the receipt says so
    address = (await inbound_email.ensure_address(idb, pid))["address"]
    await send(idb, welcome_item(to=address, files=files, mid="<e-3@example.test>"))
    assert len(await held_docs(idb, pid)) == 2
    assert "trial_ended" in mails[-1]["text"] and "connect Google Drive" in mails[-1]["text"]


async def test_ended_trial_is_not_read_nor_previewed(idb, mails, brevo, model, monkeypatch):
    from oncofiles import doc_thumbnails

    monkeypatch.setattr("oncofiles.curated.llm.configured", lambda: False)
    store, _ = brevo
    store["e-pdf"] = _tiny_pdf()
    await send(idb, welcome_item(files=[(FILE_A, "application/pdf", 9999, "e-pdf")]))
    [trial] = await trials(idb)
    pid = trial["patient_id"]
    await _end_trial(idb, pid)
    monkeypatch.setattr("oncofiles.curated.llm.configured", lambda: True)
    assert await holding_extract.run_pending(idb) == {}
    assert model == []
    doc_thumbnails._cache.clear()
    monkeypatch.setattr("oncofiles.tools.curated.run_active", lambda: False)
    [held] = await held_docs(idb, pid)
    with _caller(canonical_email(NEW)):
        resp = await server.api_doc_thumbnails(_thumb_request(idb, [held["id"]]))
    assert json.loads(resp.body)["thumbnails"] == {str(held["id"]): None}


async def test_a_claimed_document_is_never_read_twice(idb, monkeypatch, model):
    await make_trial(idb)
    doc_id = await holding_store.store_document(idb, PID, "S.jpg", jpeg(40_000), "image/jpeg")
    now = _future()
    stale = _past()
    assert await idb.claim_holding_extraction(
        doc_id, patient_id=PID, claimed_at=now, stale_before=stale
    )
    assert not await idb.claim_holding_extraction(  # another container: lost
        doc_id, patient_id=PID, claimed_at=now, stale_before=stale
    )
    assert await holding_extract.run_pending(idb) == {}  # a fresh claim: not taken over
    assert model == []
    # a claim older than STALE_READING_MIN belongs to a dead reader: taken over
    await idb.db.execute(
        "UPDATE documents SET structured_metadata = json_set(structured_metadata, "
        "'$.holding.claimed_at', ?) WHERE id = ?",
        (_past(), doc_id),
    )
    await idb.db.commit()
    assert await holding_extract.run_pending(idb) == {"done": 1}


async def test_excluded_is_stored_as_a_code(idb, monkeypatch):
    await make_trial(idb)
    monkeypatch.setattr("oncofiles.curated.llm.configured", lambda: True)
    monkeypatch.setattr(
        "oncofiles.curated.extract.extract",
        lambda name, content, mime="", **kw: Extraction(
            "", "none", 1, "-", error="raw_genotype_excluded: not sent to the model"
        ),
    )
    doc_id = await holding_store.store_document(idb, PID, "S.jpg", jpeg(40_000), "image/jpeg")
    assert await holding_extract.run_pending(idb) == {"excluded": 1}
    [row] = [d for d in await held_docs(idb, PID) if d["id"] == doc_id]
    state = json.loads(row["structured_metadata"])["holding"]
    assert (state["extract"], state["error"]) == ("excluded", "raw_genotype_excluded")
    assert holding_extract.error_code("Some free text: x") == "extract_failed"


def test_the_directory_is_fsynced_after_the_rename(monkeypatch, welcome_env):
    calls = []
    real = holding_store._fsync_dir
    monkeypatch.setattr(holding_store, "_fsync_dir", lambda p: (calls.append(p), real(p)))
    holding_store.write_encrypted(b"invented 2027", PID)
    assert calls == [welcome_env / "holding"]


async def test_old_consent_version_keeps_full_access(idb):
    """Guardrail (c): the bump is checked only when a patient is created."""
    from oncofiles.patient_consent import CONSENT_VERSION

    await idb.db.execute(
        "INSERT INTO patient_consents (patient_id, consent_version, is_admin) VALUES (?, ?, 0)",
        (PID, "2026-11-17"),
    )
    await idb.db.commit()
    assert CONSENT_VERSION != "2026-11-17"
    with _caller(CARER):
        own = json.loads((await server.api_list_patients(_dash_request(idb))).body)["own"]
        status = await server.api_trial_status(_dash_request(idb))
    assert [p["patient_id"] for p in own] == [PID]
    assert status.status_code == 200
    src = (Path(server.__file__).parent / "server.py").read_text()
    assert src.count("consent_error(") == 1  # only POST /api/patients checks it


# ── security review: trial caps on both routes ──────────────────────────


async def _trial_address(db, brevo):
    store, _ = brevo
    await send(db, welcome_item(files=two_files(store)))
    [trial] = await trials(db)
    address = (await inbound_email.ensure_address(db, trial["patient_id"]))["address"]
    return trial["patient_id"], address


async def _raw(db, to, i, n=1):
    files = [(f"SynthRaw{i}-{k}.pdf", pdf(990 + 10 * i + k), "application/pdf") for k in range(n)]
    result = await inbound_email.process_message(db, parsed(raw_mail(to, sender=NEW, files=files)))
    await drain()
    return result


async def test_raw_route_flood_to_a_trial_address_is_capped(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr(inbound_welcome, "MAX_MAILS_PER_SENDER_DAY", 2)
    pid, address = await _trial_address(idb, brevo)  # mail 1 (Brevo, queued)
    assert (await _raw(idb, address, 1)).outcome == "filed"  # mail 2 (raw MIME)
    outcomes = [(await _raw(idb, address, i)).outcome for i in range(2, 6)]
    assert outcomes == ["trial_capped"] * 4
    assert len(await held_docs(idb, pid)) == len(FILES) + 1


async def test_files_on_both_routes_count_against_one_cap(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr(inbound_welcome, "MAX_FILES_PER_SENDER_DAY", len(FILES) + 1)
    pid, address = await _trial_address(idb, brevo)  # two files via Brevo
    result = await _raw(idb, address, 1, n=2)  # two more via raw MIME
    assert result.accepted == 1 and result.skipped["daily_file_cap"] == 1
    assert len(await held_docs(idb, pid)) == len(FILES) + 1


async def test_after_drive_connects_the_phase_one_caps_apply(
    idb, mails, brevo, model, drive, monkeypatch
):
    pid, address = await _trial_address(idb, brevo)
    monkeypatch.setattr(inbound_welcome, "MAX_MAILS_PER_SENDER_DAY", 1)
    monkeypatch.setattr(inbound_welcome, "MAX_FILES_PER_SENDER_DAY", 1)

    async def connected(db, patient_id):
        return False  # Drive connected: no holding mode

    monkeypatch.setattr(holding_store, "holding_mode", connected)
    monkeypatch.setattr(inbound_email, "MAX_MAILS_PER_HOUR", 2)
    first = await _raw(idb, address, 1, n=len(FILES))  # over the welcome caps only
    assert first.outcome == "filed" and first.accepted == len(FILES)
    assert [f[0] for f in drive.files] == ["SynthRaw1-0.pdf", "SynthRaw1-1.pdf"]
    assert (await _raw(idb, address, 2)).outcome == "rate_limited"  # the phase-1 hourly cap
