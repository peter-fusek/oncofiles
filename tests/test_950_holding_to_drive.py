# ruff: noqa: F811 - the #950 fixtures are imported and used as test arguments
"""#950 phase 3: holding store -> the patient's Drive, the "ready" e-mail, the funnel.

Covers: a trial patient connecting a curated Drive folder copies each held
file once into the curated inbox through the #935 upload path (only a
verified copy is recorded), an idempotent re-run, a file whose md5 is already
in the inbox or the filed tree is not copied again, a failed copy leaves the
held copy intact and is retried later, nothing is ever deleted, the dashboard
never shows a document twice (inbox entry, then curated mirror row), a moved
row is served from Drive, the one "ready" e-mail (counts and the dashboard
link only), the time-to-value funnel (counts and medians only) and the ACL.

Synthetic only: invented Gmail local parts, file names, bytes and Drive ids,
dates in 2027. No network: Drive is a stub behind the real mover.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oncofiles import (
    dashboard_upload,
    drive_folder_link,
    holding_move,
    holding_store,
    inbound_email,
    inbound_welcome,
    server,
    trial_funnel,
)
from oncofiles.curated import inbox_upload
from oncofiles.models import OAuthToken
from tests.test_950_inbound_address import (  # noqa: F401 - fixtures
    CARER,
    PID,
    _call,
    _env,
    brevo,
    brevo_item,
    brevo_request,
    idb,
    mails,
)
from tests.test_950_welcome_onboarding import (  # noqa: F401 - fixtures
    FILE_A,
    FILE_B,
    NEW,
    SUBJECT,
    _caller,
    _dash_request,
    drain,
    held_docs,
    model,
    send,
    trials,
    two_files,
    welcome_env,
    welcome_item,
)

ROOT = "idSynthRoot9500001"
AT = "2027-01-01 00:00:00"
SRC = Path(holding_move.__file__)


class InboxStub:
    """The curated inbox of the trial patient, behind the real mover."""

    def __init__(self):
        self.files: dict[str, tuple[str, bytes, str]] = {}
        self.uploads = 0
        self.fail = False
        self.targets: list[str] = []

    def md5_ids(self, gdrive, root_id):
        return {
            hashlib.md5(d, usedforsecurity=False).hexdigest(): i
            for i, (_, d, _) in self.files.items()
        }

    def upload(self, gdrive, root_id, files):
        out = []
        for f in files:
            self.uploads += 1
            if self.fail:
                out.append({"status": "failed", "code": "HttpError"})
                continue
            fid = f"idSynthCopy95{len(self.files):05d}"
            self.files[fid] = (f.name, f.data, f.mime)
            out.append({"status": "uploaded", "name": f.name, "id": fid})
        return out


@pytest.fixture
def inbox(monkeypatch):
    stub = InboxStub()

    async def _target(db, patient_id, log_pid=None):
        stub.targets.append(patient_id)
        return dashboard_upload._Target("curated_inbox", object(), ROOT)

    monkeypatch.setattr(dashboard_upload, "_resolve_target", _target)
    monkeypatch.setattr(inbox_upload, "inbox_md5_ids", stub.md5_ids)
    monkeypatch.setattr(inbox_upload, "upload_to_inbox", stub.upload)
    trial_funnel.reset_state()
    holding_move._drain_task = None
    yield stub
    trial_funnel.reset_state()
    holding_move._drain_task = None


def _future(days=1) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


async def connect(db, pid, folder=ROOT):
    await db.upsert_oauth_token(
        OAuthToken(
            patient_id=pid,
            access_token="synthetic-access",
            refresh_token="synthetic-refresh",
            token_expiry=_future(),
            gdrive_folder_id=folder,
        )
    )


async def trial_with_two(db, brevo):
    store, _ = brevo
    await send(db, welcome_item(files=two_files(store)))
    [trial] = await trials(db)
    return trial["patient_id"]


def bins(folder) -> list[str]:
    path = Path(folder) / "holding"
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


def moved(doc) -> dict | None:
    return json.loads(doc["structured_metadata"])["holding"].get("moved_to_drive")


async def run(db):
    async with server._sync_semaphore:
        return await holding_move.run_pending(db)


# ── connect -> copied once, verified, converted ─────────────────────────


async def test_connect_copies_each_held_file_once_and_converts(
    idb, mails, brevo, model, inbox, welcome_env, monkeypatch
):
    pid = await trial_with_two(idb, brevo)
    held_before = await held_docs(idb, pid)
    files_before = bins(welcome_env)
    await connect(idb, pid)
    assert await holding_store.holding_mode(idb, pid) is False  # Drive connected

    # the real connect_folder, Drive answered by stubs: a curated root
    client = MagicMock()
    client._service.files.return_value.get.return_value.execute.return_value = {
        "id": ROOT,
        "name": "Synthetic",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client.get_folder_owner.return_value = None
    monkeypatch.setattr(drive_folder_link.GDriveClient, "from_oauth", lambda **kw: client)
    monkeypatch.setattr(
        "oncofiles.curated_folder.folder_overview",
        lambda g, f: {"container_reason": None, "subfolder_count": 0, "curated_children": []},
    )
    monkeypatch.setattr(
        "oncofiles.curated_folder.curated_reason", lambda g, f, **kw: "is a curated folder"
    )
    monkeypatch.setattr("oncofiles.curated_folder.mark_if_curated", lambda g, f: True)
    out = await drive_folder_link.connect_folder(idb, pid, folder_id=ROOT, create_subfolders=False)
    assert out.ok and out.payload["curated_root"] is True
    await drain()

    assert inbox.uploads == 2 and set(inbox.targets) == {pid}
    names = sorted(n for n, _, _ in inbox.files.values())
    assert names == sorted([FILE_A, FILE_B])
    by_md5 = {
        hashlib.md5(d, usedforsecurity=False).hexdigest(): i for i, (_, d, _) in inbox.files.items()
    }
    for doc in await held_docs(idb, pid):
        m = moved(doc)
        held = json.loads(doc["structured_metadata"])["holding"]
        assert m["via"] == "copied" and m["drive_id"] == by_md5[held["md5"]]
    [trial] = await trials(idb)
    assert trial["drive_connected_at"] and trial["converted_at"]
    # the held copies and rows are all still there (#734)
    assert bins(welcome_env) == files_before
    assert [d["id"] for d in await held_docs(idb, pid)] == [d["id"] for d in held_before]
    assert all(d["deleted_at"] is None for d in await held_docs(idb, pid))


async def test_a_trial_without_drive_is_never_moved(idb, mails, brevo, model, inbox):
    pid = await trial_with_two(idb, brevo)
    counts = await run(idb)
    assert inbox.uploads == 0 and not counts.get("copied")
    assert all(moved(d) is None for d in await held_docs(idb, pid))
    assert await holding_store.holding_mode(idb, pid) is True


async def test_rerun_is_idempotent(idb, mails, brevo, model, inbox):
    pid = await trial_with_two(idb, brevo)
    await connect(idb, pid)
    await run(idb)
    assert inbox.uploads == 2
    await run(idb)
    assert inbox.uploads == 2
    # a copy whose record was lost (killed between the write and the DB update)
    # is found by its md5 in the inbox and never written twice
    first = (await held_docs(idb, pid))[0]
    await idb.db.execute(
        "UPDATE documents SET structured_metadata = json_remove(structured_metadata, "
        "'$.holding.moved_to_drive') WHERE id = ?",
        (first["id"],),
    )
    await idb.db.execute(
        "UPDATE trial_accounts SET converted_at = NULL WHERE patient_id = ?", (pid,)
    )
    await idb.db.commit()
    counts = await run(idb)
    assert inbox.uploads == 2
    assert counts["present"] == 1
    assert counts["converted"] == 1
    assert moved((await held_docs(idb, pid))[0])["via"] == "present"


async def test_md5_already_in_inbox_or_filed_tree_is_skipped(idb, mails, brevo, model, inbox):
    pid = await trial_with_two(idb, brevo)
    store, _ = brevo
    a, b = store["w-a"], store["w-b"]
    inbox.files["idSynthInboxA95001"] = ("other-name.pdf", a, "application/pdf")
    md5_b = hashlib.md5(b, usedforsecurity=False).hexdigest()
    await idb.db.execute(
        "INSERT INTO documents (file_id, filename, original_filename, category, mime_type, "
        "size_bytes, gdrive_id, structured_metadata, patient_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "gdrive:idSynthFiledB95001",
            "20270504_Synth_Lab.jpg",
            "20270504_Synth_Lab.jpg",
            "labs",
            "image/jpeg",
            len(b),
            "idSynthFiledB95001",
            json.dumps({"source": "curated", "curated": {"source_md5": md5_b}}),
            pid,
        ),
    )
    await idb.db.commit()
    await connect(idb, pid)
    counts = await run(idb)
    assert inbox.uploads == 0 and counts["present"] == 2
    ids = {moved(d)["drive_id"] for d in await held_docs(idb, pid)}
    assert ids == {"idSynthInboxA95001", "idSynthFiledB95001"}
    [trial] = await trials(idb)
    assert trial["converted_at"]


async def test_failure_keeps_the_held_copy_and_retries_later(
    idb, mails, brevo, model, inbox, welcome_env
):
    pid = await trial_with_two(idb, brevo)
    await connect(idb, pid)
    files_before = bins(welcome_env)
    inbox.fail = True
    counts = await run(idb)
    assert inbox.uploads == 1 and counts["copy_failed"] == 1  # the first failure stops
    docs = await held_docs(idb, pid)
    assert all(moved(d) is None for d in docs)
    first = json.loads(docs[0]["structured_metadata"])["holding"]
    assert first["move_error"] == "HttpError" and first["move_attempts"] == 1
    assert bins(welcome_env) == files_before
    assert holding_store.read(docs[0]["file_id"], pid)  # still readable, intact
    [trial] = await trials(idb)
    assert trial["converted_at"] is None
    # served from the store while not moved
    inbox.fail = False
    counts = await run(idb)
    assert counts["copied"] == 2 and counts["converted"] == 1
    docs = await held_docs(idb, pid)
    assert all(moved(d) for d in docs)
    assert "move_error" not in json.loads(docs[0]["structured_metadata"])["holding"]


async def test_a_listing_failure_fails_closed(idb, mails, brevo, model, inbox, monkeypatch):
    pid = await trial_with_two(idb, brevo)
    await connect(idb, pid)

    def broken(gdrive, root_id):
        raise RuntimeError("synthetic listing failure")

    monkeypatch.setattr(inbox_upload, "inbox_md5_ids", broken)
    counts = await run(idb)
    assert counts["listing_failed"] == 1 and inbox.uploads == 0


async def test_a_non_curated_folder_is_a_wait_not_a_write(
    idb, mails, brevo, model, inbox, monkeypatch
):
    pid = await trial_with_two(idb, brevo)
    await connect(idb, pid)

    async def _drive_only(db, patient_id, log_pid=None):
        return dashboard_upload._Target("drive_only", object(), ROOT)

    monkeypatch.setattr(dashboard_upload, "_resolve_target", _drive_only)
    counts = await run(idb)
    assert counts["not_curated"] == 1 and inbox.uploads == 0


def test_inbox_name_is_the_sanitised_original():
    row = {"id": 7, "filename": "../_Synth Lab:2027", "original_filename": None,
           "mime_type": "application/pdf", "structured_metadata": "{}"}  # fmt: skip
    assert holding_move.inbox_name(row) == "Synth Lab_2027.pdf"
    held = json.dumps({"holding": {"original_filename": "Scan2027.png"}})
    row = {"id": 8, "filename": "", "original_filename": "", "mime_type": "image/png",
           "structured_metadata": held}  # fmt: skip
    assert holding_move.inbox_name(row) == "Scan2027.png"
    row = {"id": 9, "filename": "", "original_filename": "", "mime_type": "image/png",
           "structured_metadata": "{}"}  # fmt: skip
    assert holding_move.inbox_name(row) == "document-9.png"


# ── nothing is deleted (#734) ───────────────────────────────────────────


def test_the_mover_has_no_deletion_and_one_write_path():
    text = SRC.read_text()
    for forbidden in ("DELETE FROM", "trash", ".delete(", "delete_document", "os.remove",
                      "unlink(", "rmtree"):  # fmt: skip
        assert forbidden not in text.replace("deleted", ""), forbidden
    tree = ast.parse(text)
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    # Drive is written only through the #935 inbox upload
    assert "upload_to_inbox" in attrs
    for write in ("upload_original", "upload", "update", "create_folder", "move_file",
                  "rename_file", "copy_file", "contract_writes"):  # fmt: skip
        assert write not in calls, write


# ── the dashboard never shows one document twice ────────────────────────


async def test_dashboard_shows_each_document_once(idb, mails, brevo, model, inbox):
    from oncofiles.tools.hygiene import _build_document_matrix

    pid = await trial_with_two(idb, brevo)
    await connect(idb, pid)
    await run(idb)
    copies = list(inbox.files)
    snapshot = [{"id": c, "name": inbox.files[c][0], "size": 10} for c in copies]

    async def table():
        res = await _build_document_matrix(
            idb, patient_id=pid, truncate_names=False, inbox_items=snapshot
        )
        return res["documents"], res["summary"]["total"]

    # in the inbox: the held rows only (with their text), no inbox duplicates
    rows, total = await table()
    assert total == 2 and all(not r.get("inbox") for r in rows)

    # filed and mirrored by the curated indexer: the mirror rows only
    store, _ = brevo
    for c in copies:
        name, data, mime = inbox.files[c]
        await idb.db.execute(
            "INSERT INTO documents (file_id, filename, original_filename, category, mime_type, "
            "size_bytes, gdrive_id, structured_metadata, patient_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"gdrive:{c}", f"20270504_{name}", name, "labs", mime, len(data), c,
             json.dumps({"source": "curated", "curated": {"source_md5": "x"}}), pid),
        )  # fmt: skip
    await idb.db.commit()
    rows, total = await table()
    assert total == 2 and all(str(r["id"]).isdigit() for r in rows)
    assert all(not holding_store.is_holding_file_id(d.file_id)
               for d in await idb.list_documents(limit=50, patient_id=pid))  # fmt: skip
    assert await idb.count_documents(patient_id=pid) == 2
    # the next mover run records the supersession; the held rows stay
    counts = await run(idb)
    assert counts["superseded"] == 2
    docs = await held_docs(idb, pid)
    assert len(docs) == 2 and all(d["deleted_at"] is None for d in docs)
    assert all(json.loads(d["structured_metadata"])["holding"]["superseded_by"] for d in docs)


async def test_a_moved_row_is_served_from_drive(idb, mails, brevo, model, inbox, monkeypatch):
    from oncofiles.tools._helpers import _try_download

    pid = await trial_with_two(idb, brevo)
    await connect(idb, pid)
    await run(idb)
    doc_id = next(d["id"] for d in await held_docs(idb, pid) if d["mime_type"] == "image/jpeg")
    doc = await idb.get_document(doc_id, patient_id=pid)
    drive_id = holding_store.moved_drive_id(doc.structured_metadata)
    monkeypatch.setattr(
        holding_store, "read", MagicMock(side_effect=AssertionError("not from the store"))
    )
    gdrive = MagicMock()
    gdrive.download.return_value = inbox.files[drive_id][1]
    ok, _content, raw = await _try_download(MagicMock(), doc, gdrive)
    assert ok and raw == inbox.files[drive_id][1]
    gdrive.download.assert_called_once_with(drive_id)
    ok, content, raw = await _try_download(MagicMock(), doc, None)
    assert not ok and raw is None


# ── conversion: an ordinary patient afterwards ──────────────────────────


async def test_a_converted_trial_is_an_ordinary_caregiver(idb, mails, brevo, model, inbox):
    pid = await trial_with_two(idb, brevo)
    await connect(idb, pid)
    await run(idb)
    await idb.db.execute("DELETE FROM oauth_tokens WHERE patient_id = ?", (pid,))  # test setup
    await idb.db.commit()
    assert await holding_store.holding_mode(idb, pid) is False  # converted, even without Drive
    assert await inbound_welcome._caregiver_patients(idb, NEW) == [pid]


# ── the "ready" e-mail ──────────────────────────────────────────────────


def ready(sent):
    return [m for m in sent if "trial_ready" in (m.get("tags") or [])]


async def test_ready_mail_once_counts_only(idb, mails, brevo, model, inbox):
    pid = await trial_with_two(idb, brevo)  # the stub model reads both
    [mail] = ready(mails)
    assert mail["to"] == [canonical(NEW)]
    assert mail["headers"] == {"Auto-Submitted": "auto-generated"}
    text = mail["text"]
    assert "Pripravené: 2" in text and "Ready: 2" in text
    assert trial_funnel.DASHBOARD_URL in text
    address = (await inbound_email.ensure_address(idb, pid))["address"]
    for leak in (FILE_A, FILE_B, SUBJECT, inbound_welcome.TRIAL_DISPLAY_NAME, "token=", address):
        assert leak not in text and leak not in mail["subject"]
    await trial_funnel.send_due_ready_mails(idb)
    await run(idb)
    assert len(ready(mails)) == 1
    [trial] = await trials(idb)
    assert trial["ready_mail_sent_at"]


def canonical(address):
    from oncofiles.email_identity import canonical_email

    return canonical_email(address)


async def test_ready_mail_waits_until_the_batch_is_read(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr("oncofiles.curated.llm.configured", lambda: False)  # nothing read
    await trial_with_two(idb, brevo)
    assert ready(mails) == []
    assert await trial_funnel.send_due_ready_mails(idb) == 0


async def test_a_refused_ready_mail_is_retried_later(idb, mails, brevo, model, monkeypatch):
    refused: list = []

    async def _refuse(to, subject, text, **kw):
        refused.append(kw.get("tags"))
        return False

    monkeypatch.setattr("oncofiles.mailer.send_mail", _refuse)
    pid = await trial_with_two(idb, brevo)
    assert ["trial_ready"] in refused
    [trial] = await trials(idb)
    assert trial["ready_mail_sent_at"] is None  # the claim was released
    trial_funnel._last_failed.clear()  # the retry interval has passed
    monkeypatch.setattr("oncofiles.mailer.send_mail", lambda *a, **k: _ok())
    assert await trial_funnel.send_due_ready_mails(idb) == 1
    assert (await idb.get_trial(patient_id=pid))["ready_mail_sent_at"]


async def _ok():
    return True


async def test_ready_mails_are_rate_limited(idb, mails, brevo, model, monkeypatch):
    monkeypatch.setattr(trial_funnel, "READY_MAILS_PER_HOUR", 0)
    pid = await trial_with_two(idb, brevo)
    assert ready(mails) == []
    assert (await idb.get_trial(patient_id=pid))["ready_mail_sent_at"] is None


# ── the funnel ──────────────────────────────────────────────────────────


def test_funnel_counts_and_medians():
    rows = [
        {"received_at": "2027-03-01 10:00:00", "first_reply_at": "2027-03-01 10:00:30",
         "signed_in_at": "2027-03-01 10:20:00", "first_doc_ready_at": "2027-03-01 10:05:00",
         "first_mcp_call_at": "2027-03-01 11:00:00", "drive_connected_at": "2027-03-02 10:00:00",
         "converted_at": "2027-03-02 10:10:00", "ready_mail_sent_at": "2027-03-01 10:05:00"},
        {"received_at": "2027-03-03 10:00:00", "first_reply_at": "2027-03-03 10:01:30",
         "signed_in_at": None, "first_doc_ready_at": "2027-03-03 10:09:00",
         "first_mcp_call_at": None, "drive_connected_at": None, "converted_at": None,
         "ready_mail_sent_at": None},
    ]  # fmt: skip
    out = trial_funnel.funnel(rows)
    steps = {s["step"]: s for s in out["steps"]}
    assert out["trials"] == 2
    assert out["converted"] == out["ready_mail_sent"] == 1
    assert [s["step"] for s in out["steps"]] == list(trial_funnel.FUNNEL_STEPS)
    assert steps["received"]["count"] == 2
    assert (
        steps["first_reply"]["count"] == 2 and steps["first_reply"]["median_s_from_received"] == 60
    )
    assert steps["first_doc_ready"]["median_s_from_received"] == 420  # median of 300 and 540
    assert steps["signed_in"]["count"] == 1 and steps["signed_in"]["median_s_from_previous"] == 1170
    assert steps["drive_connected"]["count"] == 1
    assert trial_funnel.funnel([])["steps"][0] == {"step": "received", "count": 0}


async def test_funnel_from_the_db_and_admin_only(idb, mails, brevo, model, inbox):
    pid = await trial_with_two(idb, brevo)
    await trial_funnel.note_sign_in(idb, "Synthetic.Welcome950+x@googlemail.com")  # a variant
    await idb.db.execute(
        "INSERT INTO activity_log (session_id, agent_id, tool_name, patient_id) "
        "VALUES ('s', 'auto', 'search_documents', ?)",
        (pid,),
    )
    await idb.db.commit()
    await connect(idb, pid)
    await drive_folder_link._after_trial_connect(idb, pid)
    await drain()
    [trial] = await trials(idb)
    assert trial["first_reply_at"] and trial["signed_in_at"] and trial["drive_connected_at"]
    resp = await _call(server.api_inbound_unmatched, _dash_request(idb), CARER)
    assert resp.status_code == 403
    with_trial = await _call(server.api_inbound_unmatched, _dash_request(idb),
                             "admin950@example.test", admin=True)  # fmt: skip
    body = json.loads(with_trial.body)
    funnel = body["trial_funnel"]
    assert funnel["trials"] == 1 and funnel["converted"] == 1
    assert all(s["count"] == 1 for s in funnel["steps"])
    dumped = json.dumps(funnel)
    for leak in (pid, NEW, canonical(NEW), FILE_A, FILE_B):
        assert leak not in dumped


async def test_first_reply_is_noted_only_for_the_welcome_reply(idb, mails, brevo, model):
    await trial_with_two(idb, brevo)
    [trial] = await trials(idb)
    assert trial["first_reply_at"]
    assert await idb.note_trial_event("first_reply_at", patient_id=PID, at=AT) is False


async def test_trial_event_columns_are_an_allowlist(idb):
    with pytest.raises(ValueError):
        await idb.note_trial_event("patient_id", patient_id=PID, at=AT)


# ── ACL ─────────────────────────────────────────────────────────────────


async def test_trial_status_shows_the_move_only_to_its_owner(idb, mails, brevo, model, inbox):
    pid = await trial_with_two(idb, brevo)
    await connect(idb, pid)
    await run(idb)
    with _caller(NEW):
        own = json.loads((await server.api_trial_status(_dash_request(idb))).body)
    assert own["moved_to_drive"] == 2 and own["converted"] is True and own["drive_connected"]
    with _caller("other.person950@example.test"):
        other = await server.api_trial_status(_dash_request(idb, {"patient_id": pid}))
    assert json.loads(other.body) == {"trial": False}


async def test_the_mover_never_crosses_patients(idb, mails, brevo, model, inbox):
    from tests.test_950_welcome_onboarding import NEW2

    store, _ = brevo
    pid_a = await trial_with_two(idb, brevo)
    store["w-c"] = b"%PDF-1.7\n%synthetic second sender\n" + b"1" * 20_000
    files = [("SynthSecond2027.pdf", "application/pdf", len(store["w-c"]), "w-c")]
    await send(idb, welcome_item(sender=NEW2, files=files))
    pid_b = next(t["patient_id"] for t in await trials(idb) if t["patient_id"] != pid_a)
    await connect(idb, pid_a)  # only A connects
    await run(idb)
    assert set(inbox.targets) == {pid_a} and inbox.uploads == 2
    assert all(moved(d) is None for d in await held_docs(idb, pid_b))
    assert (await idb.get_trial(patient_id=pid_b))["converted_at"] is None
