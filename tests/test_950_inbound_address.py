"""#950 phase 1: a private document e-mail address per existing patient.

Covers the address (derivation, Gmail prefix, rotation, hash-only storage),
the webhook (inert / signature / size cap / always 202), parsing (forwarded
mail, inline images, signature logos, duplicates, types, archives, limits),
the sender rule (caregiver + passing auth only), filing through the #935
upload backend with a stubbed Drive, the receipt and loop safety, logs
without addresses or file names, the dashboard routes' ACL and the demo twin.

Synthetic only: invented addresses (example.test and invented Gmail local
parts), invented file names and bytes, dates in 2027.
"""

from __future__ import annotations

import json
import logging
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from oncofiles import dashboard_upload, inbound_email, server
from oncofiles.database import Database
from tests.helpers import PRIMARY_TEST_UUID, make_doc

SRC = Path(server.__file__).parent
ROOT = SRC.parent.parent

PID = PRIMARY_TEST_UUID
OTHER_PID = "00000000-0000-4000-8000-000000000950"
CARER = "synthetic.carer950@gmail.com"
OTHER_CARER = "other.carer950@example.test"
STRANGER = "stranger950@example.test"
SECRET = "synthetic-webhook-secret-950"
SUBJECT = "Vysledky Synteticke 2027"
FILENAMES = ("SynthLabReport2027.pdf", "SynthScanPhoto.jpg", "SynthNote.txt")


# ── fixtures and builders ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "synthetic-bearer-950")
    monkeypatch.delenv("INBOUND_ADDRESS_SECRET", raising=False)
    monkeypatch.delenv("AUDIT_EMAIL_HASH_SECRET", raising=False)
    monkeypatch.setenv(inbound_email.WEBHOOK_SECRET_ENV, SECRET)
    monkeypatch.delenv("MAIL_FROM", raising=False)
    inbound_email.reset_state()
    yield
    inbound_email.reset_state()


@pytest.fixture
async def idb(db: Database) -> Database:
    await db.db.execute(
        "UPDATE patients SET slug = ?, caregiver_email = ?, display_name = ? WHERE patient_id = ?",
        ("synth950-a", CARER, "Jana Synteticka", PID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) VALUES (?,?,?,?)",
        (OTHER_PID, "synth950-b", "Bea Synthetic", OTHER_CARER),
    )
    await db.db.commit()
    return db


class _Drive:
    """Stub of the #935 targets: records what the inbox upload received."""

    def __init__(self):
        self.files: list[tuple[str, bytes, str]] = []


@pytest.fixture
def drive(monkeypatch):
    """Stub Drive under the real ``dashboard_upload.handle_upload``."""
    stub = _Drive()

    async def _target(db, patient_id, log_pid=None):
        return dashboard_upload._Target("curated_inbox", object(), "synthetic-root")

    def _upload(gdrive, root_id, files):
        out = []
        for f in files:
            stub.files.append((f.name, f.data, f.mime))
            out.append({"status": "uploaded", "name": f.name})
        return out

    monkeypatch.setattr(dashboard_upload, "_resolve_target", _target)
    monkeypatch.setattr("oncofiles.curated.inbox_upload.upload_to_inbox", _upload)
    return stub


@pytest.fixture
def mails(monkeypatch):
    sent: list[dict] = []

    async def _send(to, subject, text, **kw):
        sent.append({"to": list(to), "subject": subject, "text": text, **kw})
        return True

    monkeypatch.setattr("oncofiles.mailer.send_mail", _send)
    return sent


def pdf(n: int = 0, size: int = 20_000) -> bytes:
    return b"%PDF-1.7\n%synthetic " + str(n).encode() + b"\n" + b"0" * size


PNG_SIG = b"\x89PNG\r\n\x1a\n"
IHDR_HEAD = bytes(3) + b"\rIHDR"
IHDR_TAIL = b"\x08\x02"
IHDR_TAIL += bytes(3)
JPEG_SOI = b"\xff\xd8\xff\xe0"
WORD = 4  # bytes of a PNG dimension
SMALL = 100


def _be(n: int) -> bytes:
    return n.to_bytes(WORD, "big")


def png(width: int, height: int, pad: int = 20_000) -> bytes:
    ihdr = IHDR_HEAD + _be(width) + _be(height)
    return PNG_SIG + ihdr + IHDR_TAIL + bytes(pad)


def jpeg(size: int = 20_000) -> bytes:
    return JPEG_SOI + bytes(size)


def gif(side: int) -> bytes:
    dims = side.to_bytes(2, "little")
    return b"GIF89a" + dims + dims + bytes(SMALL)


MP4_BOX = bytes(3) + bytes([24]) + b"ftyp"
MP4_BRAND = b"mp42"


async def address_of(db: Database, pid: str = PID) -> str:
    return (await inbound_email.ensure_address(db, pid))["address"]


def mail(
    to: str,
    *,
    sender: str = CARER,
    files: list[tuple[str, bytes, str]] | None = None,
    headers: dict | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"Synthetic Carer <{sender}>"
    msg["To"] = to
    msg["Subject"] = SUBJECT
    msg["Message-ID"] = f"<synthetic-{id(msg)}@example.test>"
    msg["Date"] = "Mon, 15 Mar 2027 10:00:00 +0000"
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content("Body text that must never be stored: synthetic 2027.")
    for name, data, ctype in files or []:
        main, sub = ctype.split("/", 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return msg


def parsed(msg: EmailMessage, *, auth: str = "pass", original_to: str = ""):
    return inbound_email.parse_raw_mime(msg.as_bytes(), original_to=original_to, auth=auth)


def _request(db: Database, body: bytes, headers: dict) -> Request:
    app = MagicMock()
    app.state.fastmcp_server._lifespan_result = {"db": db}
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/inbound/email",
        "query_string": b"",
        "headers": [(k.lower().encode(), str(v).encode()) for k, v in headers.items()],
        "app": app,
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def signed_headers(body: bytes, original_to: str = "", auth: str = "pass") -> dict:
    headers = {
        "content-length": str(len(body)),
        inbound_email.SIGNATURE_HEADER: inbound_email.sign(SECRET, original_to, auth, body),
        inbound_email.AUTH_HEADER: auth,
    }
    if original_to:
        headers[inbound_email.RECIPIENT_HEADER] = original_to
    return headers


# ── address ──────────────────────────────────────────────────────────────


def test_token_is_derived_deterministic_and_unambiguous():
    first_tok = inbound_email.derive_token(PID, 1)
    assert first_tok == inbound_email.derive_token(PID, 1)
    assert len(first_tok) == 10 and set(first_tok) <= set(inbound_email.TOKEN_ALPHABET)
    assert not set(first_tok) & set("ilou")
    assert inbound_email.derive_token(PID, 2) != first_tok
    assert inbound_email.derive_token(OTHER_PID, 1) != first_tok


def test_token_key_is_purpose_bound_and_overridable(monkeypatch):
    t1 = inbound_email.derive_token(PID, 1)
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "another-synthetic-bearer")
    assert inbound_email.derive_token(PID, 1) != t1
    monkeypatch.setenv("INBOUND_ADDRESS_SECRET", "synthetic-override-950")
    t_override = inbound_email.derive_token(PID, 1)
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "synthetic-bearer-950")
    assert inbound_email.derive_token(PID, 1) == t_override  # independent of the bearer


@pytest.mark.parametrize(
    ("caregiver", "prefix"),
    [
        ("synthetic.carer950@gmail.com", "synthetic.carer950"),
        ("Synthetic.Carer950@GMAIL.com", "synthetic.carer950"),
        ("synthetic.carer950+labs@gmail.com", "synthetic.carer950"),
        ("synthetic.carer950@gmail.com, second@example.test", "synthetic.carer950"),
        ("first@example.test, synthetic.carer950@gmail.com", ""),  # first entry only
        ("carer950@example.test", ""),
        ("carer950@googlemail.example", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_gmail_prefix(caregiver, prefix):
    assert inbound_email.gmail_prefix(caregiver) == prefix


def test_format_and_parse_address():
    token = inbound_email.derive_token(PID, 1)
    addr = inbound_email.format_address(token, CARER)
    assert addr == f"synthetic.carer950-{token}@{inbound_email.INBOUND_DOMAIN}"
    assert inbound_email.format_address(token, OTHER_CARER) == (
        f"{token}@{inbound_email.INBOUND_DOMAIN}"
    )
    assert inbound_email.token_from_address(addr) == token
    assert inbound_email.token_from_address(f"Name <{addr}>") == token
    # the prefix is never checked; a +tag is dropped; case does not matter
    other = f"whoever-{token}+x@{inbound_email.INBOUND_DOMAIN.upper()}"
    assert inbound_email.token_from_address(other) == token
    assert inbound_email.token_from_address(f"{token}@example.test") is None
    assert inbound_email.token_from_address(f"short-abc@{inbound_email.INBOUND_DOMAIN}") is None


def test_crockford_reading_rules():
    # o -> 0, i / l -> 1
    assert inbound_email.token_from_address(f"x-oil0123456@{inbound_email.INBOUND_DOMAIN}") == (
        "0110123456"
    )


async def test_db_keeps_only_the_hash_and_rotation_deactivates(idb):
    first = await inbound_email.ensure_address(idb, PID)
    again = await inbound_email.ensure_address(idb, PID)
    assert first["address"] == again["address"] and first["version"] == 1
    old_token = inbound_email.token_from_address(first["address"])
    async with idb.db.execute("SELECT * FROM inbound_addresses") as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    flat = json.dumps(rows)
    assert old_token not in flat and "@" not in flat and "carer" not in flat
    assert rows[0]["token_hash"] == inbound_email.token_hash(old_token)

    rotated = await inbound_email.rotate_address(idb, PID)
    new_token = inbound_email.token_from_address(rotated["address"])
    assert rotated["version"] == 2 and new_token != old_token
    old = await idb.find_inbound_address(inbound_email.token_hash(old_token))
    new = await idb.find_inbound_address(inbound_email.token_hash(new_token))
    assert old["active"] == 0 and new["active"] == 1 and new["patient_id"] == PID
    async with idb.db.execute(
        "SELECT COUNT(*) FROM inbound_addresses WHERE patient_id = ? AND active = 1", (PID,)
    ) as cur:
        assert (await cur.fetchone())[0] == 1


def test_migration_only_adds_the_table():
    sql = (ROOT / "migrations" / "095_inbound_addresses.sql").read_text()
    code = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    assert "CREATE TABLE IF NOT EXISTS inbound_addresses" in code
    assert "patient_id TEXT NOT NULL REFERENCES patients(patient_id)" in code
    assert "token_hash TEXT NOT NULL UNIQUE" in code
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"):
        assert verb not in code.upper(), verb
    assert "token TEXT" not in code and "address TEXT" not in code


# ── webhook ──────────────────────────────────────────────────────────────


async def test_webhook_is_404_while_the_secret_is_unset(idb, monkeypatch):
    monkeypatch.delenv(inbound_email.WEBHOOK_SECRET_ENV)
    body = b"From: x\r\n\r\nhello"
    with pytest.raises(HTTPException) as exc:
        await server.inbound_email_webhook(_request(idb, body, signed_headers(body)))
    assert exc.value.status_code == 404


@pytest.mark.parametrize("kind", ["missing", "wrong", "other_secret", "tampered_to", "flip_auth"])
async def test_webhook_refuses_a_bad_signature_with_the_same_404(idb, drive, kind):
    addr = await address_of(idb)
    body = mail(addr, files=[(FILENAMES[0], pdf(), "application/pdf")]).as_bytes()
    headers = signed_headers(body, original_to=addr, auth="fail")
    if kind == "missing":
        del headers[inbound_email.SIGNATURE_HEADER]
    elif kind == "wrong":
        headers[inbound_email.SIGNATURE_HEADER] = "sha256=" + "0" * 64
    elif kind == "other_secret":
        headers[inbound_email.SIGNATURE_HEADER] = inbound_email.sign("x", addr, "fail", body)
    elif kind == "tampered_to":
        headers[inbound_email.RECIPIENT_HEADER] = f"y-{'2' * 10}@{inbound_email.INBOUND_DOMAIN}"
    else:
        headers[inbound_email.AUTH_HEADER] = "pass"  # verdict is inside the signature
    with pytest.raises(HTTPException) as exc:
        await server.inbound_email_webhook(_request(idb, body, headers))
    assert exc.value.status_code == 404
    assert drive.files == []


async def test_webhook_size_cap_before_reading(idb, monkeypatch):
    monkeypatch.setattr(inbound_email, "MAX_BODY_BYTES", 1000)
    body = b"x" * 2000
    headers = signed_headers(body)
    resp = await server.inbound_email_webhook(_request(idb, body, headers))
    assert resp.status_code == 413
    # a body longer than it declares is cut while streaming too
    headers["content-length"] = "10"
    resp = await server.inbound_email_webhook(_request(idb, body, headers))
    assert resp.status_code == 413


async def test_webhook_unknown_token_is_a_silent_202(idb, drive, mails):
    addr = f"someone-{'2' * 10}@{inbound_email.INBOUND_DOMAIN}"
    body = mail(addr, files=[(FILENAMES[0], pdf(), "application/pdf")]).as_bytes()
    resp = await server.inbound_email_webhook(_request(idb, body, signed_headers(body, addr)))
    assert resp.status_code == 202 and json.loads(resp.body) == {"status": "accepted"}
    assert drive.files == [] and mails == []
    assert inbound_email.unmatched_overview()["totals"]["unknown_token"] == 1


async def test_webhook_inactive_token_is_dropped(idb, drive, mails):
    old = await address_of(idb)
    await inbound_email.rotate_address(idb, PID)
    body = mail(old, files=[(FILENAMES[0], pdf(), "application/pdf")]).as_bytes()
    resp = await server.inbound_email_webhook(_request(idb, body, signed_headers(body, old)))
    assert resp.status_code == 202
    assert drive.files == [] and mails == []
    assert inbound_email.unmatched_overview()["totals"]["inactive_token"] == 1


async def test_webhook_end_to_end_files_and_replies(idb, drive, mails):
    addr = await address_of(idb)
    body = mail(addr, files=[(FILENAMES[0], pdf(), "application/pdf")]).as_bytes()
    resp = await server.inbound_email_webhook(_request(idb, body, signed_headers(body, addr)))
    assert resp.status_code == 202 and json.loads(resp.body) == {"status": "accepted"}
    assert [f[0] for f in drive.files] == [FILENAMES[0]]
    assert len(mails) == 1 and mails[0]["to"] == [CARER]


async def test_unparseable_body_is_accepted_and_dropped(idb, monkeypatch):
    def _boom(*a, **k):
        raise inbound_email.UnparseableMessageError("x")

    monkeypatch.setattr(inbound_email, "parse_raw_mime", _boom)
    body = b"\x00\x01"
    resp = await server.inbound_email_webhook(_request(idb, body, signed_headers(body)))
    assert resp.status_code == 202


async def test_recipient_falls_back_to_the_message_headers(idb, drive, mails):
    addr = await address_of(idb)
    msg = mail(f"Somebody <{STRANGER}>", files=[(FILENAMES[0], pdf(), "application/pdf")])
    msg["Delivered-To"] = addr
    result = await inbound_email.process_message(idb, parsed(msg))
    assert result.outcome == "filed" and result.accepted == 1


# ── senders ──────────────────────────────────────────────────────────────


async def test_caregiver_with_passing_auth_is_filed_through_the_935_backend(idb, drive, mails):
    addr = await address_of(idb)
    calls = []
    real = dashboard_upload.handle_upload

    async def spy(db, patient_id, uploads, **kw):
        calls.append((patient_id, len(uploads), kw))
        return await real(db, patient_id, uploads, **kw)

    files = [
        (FILENAMES[0], pdf(1), "application/pdf"),
        (FILENAMES[1], jpeg(), "image/jpeg"),
        (FILENAMES[2], b"Synthetic note 2027, nothing else.", "text/plain"),
    ]
    with patch.object(dashboard_upload, "handle_upload", spy):
        result = await inbound_email.process_message(idb, parsed(mail(addr, files=files)))
    assert result.outcome == "filed" and result.accepted == 3
    sizes = [c[1] for c in calls]
    assert sizes == [1] * len(FILENAMES)  # one file per call, as the dashboard
    pids = {c[0] for c in calls}
    kws = [c[2] for c in calls]
    assert pids == {PID}
    assert all(kw == {"log_pid": PID[:4]} for kw in kws)
    assert [(n, m) for n, _d, m in drive.files] == [
        (FILENAMES[0], "application/pdf"),
        (FILENAMES[1], "image/jpeg"),
        (FILENAMES[2], "text/plain"),
    ]


async def test_caregiver_matching_is_case_insensitive(idb, drive, mails):
    addr = await address_of(idb)
    msg = mail(addr, sender=CARER.upper(), files=[(FILENAMES[0], pdf(), "application/pdf")])
    assert (await inbound_email.process_message(idb, parsed(msg))).accepted == 1


async def test_unknown_sender_is_not_filed_and_listed_for_the_admin(idb, drive, mails):
    addr = await address_of(idb)
    msg = mail(addr, sender=STRANGER, files=[(FILENAMES[0], pdf(), "application/pdf")])
    result = await inbound_email.process_message(idb, parsed(msg))
    assert result.outcome == "unmatched"
    assert drive.files == [] and mails == []
    over = inbound_email.unmatched_overview()
    [entry] = over["senders"]
    assert entry["reasons"] == {"unknown_sender": 1} and entry["files"] == 1
    assert entry["patient"] == PID[:4] and len(entry["sender_hash"]) == 16
    flat = json.dumps(over)
    assert "@" not in flat and "stranger" not in flat and PID not in flat


async def test_another_patients_caregiver_is_an_unknown_sender(idb, drive, mails):
    addr = await address_of(idb)
    msg = mail(addr, sender=OTHER_CARER, files=[(FILENAMES[0], pdf(), "application/pdf")])
    assert (await inbound_email.process_message(idb, parsed(msg))).outcome == "unmatched"
    assert drive.files == []


async def test_caregiver_with_failed_auth_is_not_filed(idb, drive, mails):
    addr = await address_of(idb)
    msg = mail(addr, files=[(FILENAMES[0], pdf(), "application/pdf")])
    for auth in ("fail", "", "softfail"):
        inbound_email.reset_state()
        result = await inbound_email.process_message(idb, parsed(msg, auth=auth))
        assert result.outcome == "unmatched"
        assert inbound_email.unmatched_overview()["senders"][0]["reasons"] == {"auth_failed": 1}
    assert drive.files == [] and mails == []


async def test_patient_is_routed_only_by_the_token(idb, drive, mails):
    """The From of another patient's caregiver never re-routes a mail."""
    addr_b = await address_of(idb, OTHER_PID)
    msg = mail(addr_b, sender=CARER, files=[(FILENAMES[0], pdf(), "application/pdf")])
    result = await inbound_email.process_message(idb, parsed(msg))
    assert result.outcome == "unmatched" and result.patient_prefix == OTHER_PID[:4]
    assert drive.files == []


async def test_patient_without_drive_is_not_filed(idb, mails, monkeypatch):
    async def _no_drive(db, patient_id, log_pid=None):
        return dashboard_upload.refusal("no_drive", 409)

    monkeypatch.setattr(dashboard_upload, "_resolve_target", _no_drive)
    addr = await address_of(idb)
    files = [(f"Synth{i}.pdf", pdf(i), "application/pdf") for i in range(3)]
    result = await inbound_email.process_message(idb, parsed(mail(addr, files=files)))
    assert result.accepted == 0 and result.skipped == {"no_drive": 3}
    assert inbound_email.unmatched_overview()["totals"]["skipped_no_drive"] == 3


# ── parsing and screening ────────────────────────────────────────────────


async def test_forwarded_rfc822_attachments_are_taken(idb, drive, mails):
    addr = await address_of(idb)
    inner = mail("lab@example.test", sender="lab@example.test")
    inner.add_attachment(pdf(7), maintype="application", subtype="pdf", filename="SynthFwd.pdf")
    outer = mail(addr)
    outer.add_attachment(inner)  # message/rfc822
    result = await inbound_email.process_message(idb, parsed(outer))
    assert result.accepted == 1
    assert [f[0] for f in drive.files] == ["SynthFwd.pdf"]


async def test_forwarded_eml_file_is_taken(idb, drive, mails):
    addr = await address_of(idb)
    inner = mail("lab@example.test", sender="lab@example.test")
    inner.add_attachment(pdf(8), maintype="application", subtype="pdf", filename="SynthEml.pdf")
    outer = mail(addr, files=[("forwarded.eml", inner.as_bytes(), "application/octet-stream")])
    result = await inbound_email.process_message(idb, parsed(outer))
    assert result.accepted == 1
    assert [f[0] for f in drive.files] == ["SynthEml.pdf"]


async def test_inline_image_is_taken_and_named(idb, drive, mails):
    addr = await address_of(idb)
    msg = mail(addr)
    msg.add_alternative("<p>synthetic</p><img src='cid:scan'>", subtype="html")
    html = msg.get_payload()[-1]
    html.add_related(png(1200, 1600), maintype="image", subtype="png", cid="<scan>")
    result = await inbound_email.process_message(idb, parsed(msg))
    assert result.accepted == 1
    name = drive.files[0][0]
    assert name.startswith("email-image-") and name.endswith(".png")


async def test_signature_logos_are_skipped(idb, drive, mails):
    addr = await address_of(idb)
    files = [
        ("logo.png", png(120, 40, pad=50_000), "image/png"),  # small dimensions
        ("tiny.jpg", jpeg(size=2000), "image/jpeg"),  # under 10 kB
        ("logo.gif", gif(64), "image/gif"),
    ]
    result = await inbound_email.process_message(idb, parsed(mail(addr, files=files)))
    assert result.accepted == 0 and result.skipped == {"signature_image": 3}
    assert drive.files == []


async def test_duplicates_are_skipped(idb, drive, mails):
    addr = await address_of(idb)
    same = pdf(42)
    files = [("SynthA.pdf", same, "application/pdf"), ("SynthB.pdf", same, "application/pdf")]
    first = await inbound_email.process_message(idb, parsed(mail(addr, files=files)))
    assert first.accepted == 1 and first.skipped == {"duplicate": 1}
    # the same file again in a later mail (recent mail filings)
    again = await inbound_email.process_message(idb, parsed(mail(addr, files=files[:1])))
    assert again.accepted == 0 and again.skipped == {"duplicate": 1}


async def test_duplicate_of_an_existing_document_by_md5(idb, drive, mails):
    import hashlib

    data = pdf(99)
    doc = await idb.insert_document(make_doc(file_id="synth950-doc"), patient_id=PID)
    await idb.update_gdrive_md5(doc.id, hashlib.md5(data).hexdigest())
    addr = await address_of(idb)
    msg = mail(addr, files=[("SynthKnown.pdf", data, "application/pdf")])
    result = await inbound_email.process_message(idb, parsed(msg))
    assert result.accepted == 0 and result.skipped == {"duplicate": 1}


@pytest.mark.parametrize(
    ("name", "data", "ctype", "code"),
    [
        ("SynthArchive.zip", b"PK\x03\x04" + b"\x00" * 500, "application/zip", "archive_refused"),
        ("SynthDoc.pdf", b"PK\x03\x04" + b"\x00" * 500, "application/pdf", "archive_refused"),
        ("SynthTar.gz", b"\x1f\x8b" + b"\x00" * 500, "application/gzip", "archive_refused"),
        ("SynthWord.docx", b"PK\x03\x04" + b"\x00" * 500, "application/msword", "archive_refused"),
        ("SynthProgram.exe", b"MZ" + b"\x00" * 500, "application/octet-stream", "type_not_allowed"),
        (
            "SynthMovie.mp4",
            MP4_BOX + MP4_BRAND + bytes(SMALL),
            "video/mp4",
            "type_not_allowed",
        ),
        ("SynthFake.pdf", jpeg(), "application/pdf", "content_mismatch"),
        ("synth.vcf", b"##fileformat=VCFv4.2\n", "text/plain", "raw_genotype_excluded"),
        (
            "SynthExport.txt",
            b"rs1000001 AA\nrs1000002 AG\nrs1000003 GG\nrs1000004 CT\n",
            "text/plain",
            "raw_genotype_excluded",
        ),
        ("SynthTable.txt", b"col,col\n1,2\n", "text/csv", None),
    ],
)
async def test_type_allowlist_by_magic_bytes(idb, drive, mails, name, data, ctype, code):
    addr = await address_of(idb)
    result = await inbound_email.process_message(
        idb, parsed(mail(addr, files=[(name, data, ctype)]))
    )
    if code is None:
        assert result.accepted == 1
    else:
        assert result.accepted == 0 and result.skipped == {code: 1}
        assert drive.files == []


def test_allowlist_is_the_dashboard_one():
    """The final word is the #935 check: nothing it refuses is filed by mail."""
    for ext in (".docx", ".odt", ".gif", ".zip"):
        assert ext not in dashboard_upload.ALLOWED_TYPES
    att = inbound_email.InboundAttachment("SynthPhoto", jpeg(), "image/jpeg")
    assert inbound_email.screen(att, 0) == ("SynthPhoto.jpg", "image/jpeg")


async def test_file_count_and_size_limits(idb, drive, mails, monkeypatch):
    addr = await address_of(idb)
    files = [(f"Synth{i:02d}.pdf", pdf(i, 100), "application/pdf") for i in range(22)]
    result = await inbound_email.process_message(idb, parsed(mail(addr, files=files)))
    assert result.accepted == 20 and result.skipped == {"too_many_files": 2}

    inbound_email.reset_state()
    monkeypatch.setattr(inbound_email, "MAX_BYTES_PER_MAIL", 50_000)
    files = [(f"Big{i}.pdf", pdf(100 + i, 20_000), "application/pdf") for i in range(4)]
    result = await inbound_email.process_message(idb, parsed(mail(addr, files=files)))
    assert result.accepted == 2 and result.skipped == {"mail_too_large": 2}


async def test_per_address_caps(idb, drive, mails, monkeypatch):
    monkeypatch.setattr(inbound_email, "MAX_MAILS_PER_HOUR", 1)
    addr = await address_of(idb)
    first = await inbound_email.process_message(
        idb, parsed(mail(addr, files=[("S1.pdf", pdf(1), "application/pdf")]))
    )
    second = await inbound_email.process_message(
        idb, parsed(mail(addr, files=[("S2.pdf", pdf(2), "application/pdf")]))
    )
    assert first.outcome == "filed" and second.outcome == "rate_limited"

    inbound_email.reset_state()
    monkeypatch.setattr(inbound_email, "MAX_MAILS_PER_HOUR", 20)
    monkeypatch.setattr(inbound_email, "MAX_FILES_PER_DAY", 2)
    files = [(f"D{i}.pdf", pdf(10 + i), "application/pdf") for i in range(3)]
    result = await inbound_email.process_message(idb, parsed(mail(addr, files=files)))
    assert result.accepted == 2 and result.skipped == {"daily_file_cap": 1}


async def test_bodies_are_never_filed_or_stored(idb, drive, mails):
    addr = await address_of(idb)
    result = await inbound_email.process_message(idb, parsed(mail(addr)))
    assert result.accepted == 0 and drive.files == []
    assert mails == []  # nothing accepted or skipped: no receipt either


async def test_same_message_id_twice_is_one_delivery(idb, drive, mails):
    addr = await address_of(idb)
    msg = mail(addr, files=[("S1.pdf", pdf(1), "application/pdf")])
    first = await inbound_email.process_message(idb, parsed(msg))
    second = await inbound_email.process_message(idb, parsed(msg))
    assert first.accepted == 1 and second.outcome == "duplicate_delivery"
    assert len(drive.files) == 1


# ── receipt and loop safety ──────────────────────────────────────────────


async def test_receipt_has_counts_and_the_terms_sentence_only(idb, drive, mails):
    addr = await address_of(idb)
    files = [
        (FILENAMES[0], pdf(1), "application/pdf"),
        ("SynthArchive.zip", b"PK\x03\x04" + b"\x00" * 50, "application/zip"),
        ("SynthProgram.exe", b"MZ" + b"\x00" * 50, "application/octet-stream"),
    ]
    await inbound_email.process_message(idb, parsed(mail(addr, files=files)))
    [sent] = mails
    assert sent["to"] == [CARER] and sent["tags"] == ["inbound_receipt"]
    text = sent["subject"] + sent["text"]
    assert inbound_email.TERMS_SENTENCE_SK in text and inbound_email.TERMS_SENTENCE_EN in text
    assert "https://oncofiles.com/terms" in text
    assert "1 súbor" in text and "1 file" in text
    assert "archive_refused" in text and "type_not_allowed" in text
    for secret in (*FILENAMES, "SynthArchive", "SynthProgram", "Jana", "Synteticka", SUBJECT):
        assert secret not in text
    assert "synthetic.carer950" not in text and addr not in text


async def test_one_receipt_per_address_per_ten_minutes(idb, drive, mails):
    addr = await address_of(idb)
    for i in range(3):
        msg = mail(addr, files=[(f"S{i}.pdf", pdf(i), "application/pdf")])
        await inbound_email.process_message(idb, parsed(msg))
    assert len(drive.files) == 3 and len(mails) == 1
    assert inbound_email.unmatched_overview()["totals"]["reply_throttled"] == 2


@pytest.mark.parametrize(
    ("headers", "sender", "reason"),
    [
        ({"Auto-Submitted": "auto-replied"}, CARER, "auto_submitted"),
        ({"Auto-Submitted": "auto-generated"}, CARER, "auto_submitted"),
        ({"Precedence": "bulk"}, CARER, "bulk"),
        ({"Precedence": "list"}, CARER, "bulk"),
        ({"Precedence": "junk"}, CARER, "bulk"),
        ({"List-Id": "<synthetic.example.test>"}, CARER, "bulk"),
        ({"Return-Path": "<>"}, CARER, "bounce"),
        ({}, "MAILER-DAEMON@example.test", "bounce"),
        ({}, "postmaster@example.test", "bounce"),
        ({}, "noreply@oncofiles.com", "own_domain"),
        ({}, f"robot@{inbound_email.INBOUND_DOMAIN}", "own_domain"),
    ],
)
async def test_loop_and_bounce_mail_is_ignored(idb, drive, mails, headers, sender, reason):
    addr = await address_of(idb)
    msg = mail(
        addr, sender=sender, files=[(FILENAMES[0], pdf(), "application/pdf")], headers=headers
    )
    result = await inbound_email.process_message(idb, parsed(msg))
    assert result.outcome == "ignored_loop"
    assert drive.files == [] and mails == []
    assert inbound_email.unmatched_overview()["totals"][f"ignored_{reason}"] == 1


async def test_auto_submitted_no_is_not_a_loop(idb, drive, mails):
    addr = await address_of(idb)
    msg = mail(
        addr, files=[(FILENAMES[0], pdf(), "application/pdf")], headers={"Auto-Submitted": "no"}
    )
    assert (await inbound_email.process_message(idb, parsed(msg))).accepted == 1


async def test_delivery_report_and_own_message_id_are_ignored(idb, drive, mails, monkeypatch):
    addr = await address_of(idb)
    raw_report = (
        f"From: Mail Delivery <{CARER}>\r\nTo: {addr}\r\nSubject: {SUBJECT}\r\n"
        "Message-ID: <report.950@example.test>\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="b950"\r\n\r\n'
        "--b950\r\nContent-Type: text/plain\r\n\r\nsynthetic\r\n"
        "--b950\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; x\r\n\r\n"
        "--b950--\r\n"
    ).encode()
    report = inbound_email.parse_raw_mime(raw_report, auth="pass")
    assert report.is_report
    assert (await inbound_email.process_message(idb, report)).outcome == "ignored_loop"

    monkeypatch.setenv("MAIL_FROM", "Oncofiles <noreply@mail.example.test>")
    own = mail(addr, files=[(FILENAMES[0], pdf(), "application/pdf")])
    del own["Message-ID"]
    own["Message-ID"] = "<abc.950@mail.example.test>"
    assert (await inbound_email.process_message(idb, parsed(own))).outcome == "ignored_loop"
    assert drive.files == [] and mails == []


# ── logs ─────────────────────────────────────────────────────────────────


async def test_logs_carry_no_address_subject_or_file_name(idb, drive, mails, caplog):
    caplog.set_level(logging.DEBUG)
    addr = await address_of(idb)
    files = [
        (FILENAMES[0], pdf(1), "application/pdf"),
        ("SynthArchive.zip", b"PK\x03\x04" + b"\x00" * 50, "application/zip"),
    ]
    await inbound_email.process_message(idb, parsed(mail(addr, files=files)))
    stranger = mail(addr, sender=STRANGER, files=files)
    await inbound_email.process_message(idb, parsed(stranger))
    body = mail(f"x-{'2' * 10}@{inbound_email.INBOUND_DOMAIN}", files=files).as_bytes()
    await server.inbound_email_webhook(_request(idb, body, signed_headers(body)))
    bad = signed_headers(body)
    bad[inbound_email.SIGNATURE_HEADER] = "sha256=00"
    with pytest.raises(HTTPException):
        await server.inbound_email_webhook(_request(idb, body, bad))
    # our own loggers (aiosqlite's DEBUG echo of SQL parameters is a library trace)
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("oncofiles"))
    assert "inbound:" in text
    for secret in (
        "@",
        "synthetic.carer950",
        "stranger950",
        SUBJECT,
        *FILENAMES,
        "SynthArchive",
        inbound_email.token_from_address(addr),
        PID,  # only the 4-character prefix
    ):
        assert secret not in text, secret
    assert PID[:4] in text


# ── dashboard routes ─────────────────────────────────────────────────────


def _dash_request(db, patient_id=None, *, body=None):
    request = MagicMock()
    request.query_params = {"patient_id": patient_id} if patient_id is not None else {}
    request.path_params = {}
    request.headers = {"authorization": "Bearer session:synthetic"}
    request.cookies = {}
    request.app.state.fastmcp_server._lifespan_result = {"db": db}
    request.json = AsyncMock(return_value=body or {})
    return request


def _caller(email, *, admin=False, bearer=False):
    unauthorized = JSONResponse({"error": "unauthorized"}, status_code=401)
    return (
        patch.object(server, "_get_dashboard_email", return_value=email),
        patch.object(server, "_check_bearer", return_value=None if bearer else unauthorized),
        patch.object(server, "_is_admin_email", return_value=admin),
    )


async def _call(route, request, email, **kw):
    patch_email, patch_bearer, patch_admin = _caller(email, **kw)
    with patch_email, patch_bearer, patch_admin:
        return await route(request)


async def test_get_address_for_the_caregiver(idb):
    resp = await _call(server.api_inbound_address, _dash_request(idb, "synth950-a"), CARER)
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["enabled"] is True
    assert data["address"].startswith("synthetic.carer950-")
    assert data["address"].endswith("@" + inbound_email.INBOUND_DOMAIN)
    assert data["terms_url"].endswith("/terms")


async def test_get_address_foreign_equals_unknown(idb):
    foreign = await _call(server.api_inbound_address, _dash_request(idb, "synth950-b"), CARER)
    unknown = await _call(server.api_inbound_address, _dash_request(idb, "synth950-zz"), CARER)
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.body == unknown.body
    async with idb.db.execute("SELECT COUNT(*) FROM inbound_addresses") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_get_address_read_only_for_admin_and_off_when_disabled(idb, monkeypatch):
    resp = await _call(
        server.api_inbound_address, _dash_request(idb, "synth950-b"), CARER, admin=True
    )
    assert resp.status_code == 403 and json.loads(resp.body)["error"] == "read_only"
    monkeypatch.delenv(inbound_email.WEBHOOK_SECRET_ENV)
    resp = await _call(server.api_inbound_address, _dash_request(idb, "synth950-a"), CARER)
    assert json.loads(resp.body) == {"enabled": False}
    async with idb.db.execute("SELECT COUNT(*) FROM inbound_addresses") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_rotate_route_acl_and_effect(idb):
    before = await address_of(idb)
    foreign = await _call(
        server.api_inbound_address_rotate,
        _dash_request(idb, body={"patient_id": "synth950-b"}),
        CARER,
    )
    unknown = await _call(
        server.api_inbound_address_rotate,
        _dash_request(idb, body={"patient_id": "synth950-zz"}),
        CARER,
    )
    assert foreign.status_code == unknown.status_code == 404 and foreign.body == unknown.body
    admin = await _call(
        server.api_inbound_address_rotate,
        _dash_request(idb, body={"patient_id": "synth950-a"}),
        "admin950@example.test",
        admin=True,
    )
    assert admin.status_code == 403
    assert await address_of(idb) == before
    ok = await _call(
        server.api_inbound_address_rotate,
        _dash_request(idb, body={"patient_id": "synth950-a"}),
        CARER,
    )
    assert ok.status_code == 200
    after = json.loads(ok.body)
    assert after["version"] == 2 and after["address"] != before


async def test_unmatched_route_is_admin_only(idb):
    resp = await _call(server.api_inbound_unmatched, _dash_request(idb), CARER)
    assert resp.status_code == 403
    resp = await _call(
        server.api_inbound_unmatched, _dash_request(idb), "admin950@example.test", admin=True
    )
    assert resp.status_code == 200 and "senders" in json.loads(resp.body)


def test_rate_limit_class_and_body_cap():
    from oncofiles import ratelimit

    assert ratelimit.classify("/inbound/email") == ratelimit.API
    assert "/inbound/email" in ratelimit.INBOUND_PATHS
    cfg = ratelimit.RateLimitConfig.from_env()
    assert cfg.inbound_max_body_bytes >= inbound_email.MAX_BODY_BYTES


# ── dashboard markup and demo twin ───────────────────────────────────────


HTML = (SRC / "dashboard.html").read_text()
JS = (SRC / "dashboard.js").read_text()


def test_address_card_markup():
    start = HTML.index('id="inbound-card"')
    card = HTML[start : HTML.index("</section>", start)]
    for action in ("copyInboundAddress", "downloadInboundVcard", "rotateInboundAddress"):
        assert f'data-action="{action}"' in card
    assert 'href="/terms"' in card
    assert "data-sk=" in card and "data-en=" in card
    assert not any(f" on{e}=" in card for e in ("click", "change", "input"))
    # the card is in both variants; the admin panel is app-only
    app_only = HTML[HTML.index("<!--app-only-->\n <!-- Administration") :]
    assert "toggleAdminPanel|inbound|@this" in app_only[: app_only.index("<!--/app-only-->")]
    assert HTML.index('id="inbound-card"') < HTML.index("<!--app-only-->\n <!-- Administration")


def test_dashboard_js_wiring():
    assert "if (path === '/api/inbound-address') return d.inbound_address;" in JS
    blocked = JS[
        JS.index("var DEMO_BLOCKED_ACTIONS") : JS.index("};", JS.index("var DEMO_BLOCKED_ACTIONS"))
    ]
    assert "rotateInboundAddress: 1" in blocked
    section = JS[
        JS.index("// ── Document e-mail address (#950)") : JS.index("// ── Patient selector")
    ]
    assert "innerHTML" not in section and "confirm(" not in section and "alert(" not in section
    assert "'/api/inbound-unmatched'" in JS


def test_demo_twin_has_an_invented_address():
    from oncofiles import demo_dashboard

    built = demo_dashboard.build([], "0.0.0-test")
    twin = built["inbound_address"]
    assert twin["enabled"] is True
    local, _, domain = twin["address"].partition("@")
    assert domain == inbound_email.INBOUND_DOMAIN
    assert inbound_email.token_from_address(twin["address"]) == demo_dashboard.DEMO_INBOUND_TOKEN
    assert local.startswith("jana.example-")


def test_claude_md_mentions_the_feature():
    text = (ROOT / "CLAUDE.md").read_text()
    assert "#950" in text and "INBOUND_WEBHOOK_SECRET" in text


# ── Brevo adapter (D3 = Brevo) ───────────────────────────────────────────

AUTHSERV = "mx.synthetic-relay.example"


@pytest.fixture
def brevo(monkeypatch):
    """BREVO_API_KEY + authserv set; attachment downloads served from a dict."""
    monkeypatch.setenv(inbound_email.BREVO_API_KEY_ENV, "synthetic-brevo-key-950")
    monkeypatch.setenv(inbound_email.BREVO_AUTHSERV_ENV, AUTHSERV)
    store: dict[str, bytes] = {}
    fetched: list[str] = []

    def _fetcher(token, api_key, limit):
        async def fetch():
            fetched.append(token)
            data = store.get(token)
            return data if data is not None and len(data) <= limit else None

        return fetch

    monkeypatch.setattr(inbound_email, "_brevo_fetcher", _fetcher)
    return store, fetched


def brevo_item(to: str, *, sender: str = CARER, files=(), auth: str = "pass", headers=None):
    hdrs = {"Authentication-Results": f"{AUTHSERV}; spf=pass; dkim=pass; dmarc={auth}"}
    hdrs.update(headers or {})
    return {
        "Uuid": ["00000000-0000-4000-8000-00000000b950"],
        "MessageId": f"<brevo-{len(files)}-{to[:3]}-{sender[:3]}@example.test>",
        "InReplyTo": None,
        "From": {"Name": "Synthetic Carer", "Address": sender},
        "To": [{"Name": None, "Address": to}],
        "Recipients": [to],
        "Cc": [],
        "ReplyTo": None,
        "SentAtDate": "Mon, 15 Mar 2027 10:00:00 +0000",
        "Subject": SUBJECT,
        "RawHtmlBody": "<p>never stored</p>",
        "RawTextBody": "never stored",
        "ExtractedMarkdownMessage": "never stored",
        "ExtractedMarkdownSignature": None,
        "SpamScore": 0.5,
        "Attachments": [
            {
                "Name": name,
                "ContentType": ctype,
                "ContentLength": size,
                "ContentID": None,
                "DownloadToken": token,
            }
            for name, ctype, size, token in files
        ],
        "Headers": hdrs,
    }


def brevo_request(db, payload, token=SECRET):
    body = json.dumps(payload).encode()
    headers = {"content-length": str(len(body)), "content-type": "application/json"}
    if token is not None:
        headers[inbound_email.BREVO_TOKEN_HEADER] = token
    req = _request(db, body, headers)
    req.scope["path"] = "/inbound/brevo"
    return req


@pytest.mark.parametrize("token", [None, "", "wrong", SECRET + "x"])
async def test_brevo_bad_token_is_404(idb, drive, brevo, token):
    addr = await address_of(idb)
    payload = {"items": [brevo_item(addr, files=[("S.pdf", "application/pdf", 10, "t1")])]}
    with pytest.raises(HTTPException) as exc:
        await server.inbound_brevo_webhook(brevo_request(idb, payload, token))
    assert exc.value.status_code == 404
    assert drive.files == [] and brevo[1] == []


async def test_brevo_inert_without_secret(idb, monkeypatch):
    monkeypatch.delenv(inbound_email.WEBHOOK_SECRET_ENV)
    with pytest.raises(HTTPException) as exc:
        await server.inbound_brevo_webhook(brevo_request(idb, {"items": []}))
    assert exc.value.status_code == 404


async def test_brevo_body_cap(idb, monkeypatch):
    monkeypatch.setattr(inbound_email, "BREVO_MAX_BODY_BYTES", 100)
    resp = await server.inbound_brevo_webhook(brevo_request(idb, {"items": ["x" * 500]}))
    assert resp.status_code == 413


async def test_brevo_caregiver_is_filed_after_download(idb, drive, mails, brevo):
    store, fetched = brevo
    store["tok-a"] = pdf(1)
    store["tok-b"] = jpeg()
    addr = await address_of(idb)
    item = brevo_item(
        addr,
        files=[
            (FILENAMES[0], "application/pdf", len(store["tok-a"]), "tok-a"),
            (FILENAMES[1], "image/jpeg", len(store["tok-b"]), "tok-b"),
        ],
    )
    resp = await server.inbound_brevo_webhook(brevo_request(idb, {"items": [item]}))
    assert resp.status_code == 202 and json.loads(resp.body) == {"status": "accepted"}
    assert fetched == ["tok-a", "tok-b"]
    assert [f[0] for f in drive.files] == list(FILENAMES[:2])
    assert len(mails) == 1 and mails[0]["to"] == [CARER]


async def test_brevo_unknown_sender_downloads_nothing(idb, drive, mails, brevo):
    store, fetched = brevo
    store["tok-a"] = pdf(1)
    addr = await address_of(idb)
    item = brevo_item(addr, sender=STRANGER, files=[("S.pdf", "application/pdf", 10, "tok-a")])
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [item]}))
    assert fetched == [] and drive.files == [] and mails == []
    assert inbound_email.unmatched_overview()["senders"][0]["reasons"] == {"unknown_sender": 1}


@pytest.mark.parametrize("case", ["env_unset", "dmarc_fail", "foreign_authserv"])
async def test_brevo_auth_verdict_is_conservative(idb, drive, mails, brevo, monkeypatch, case):
    addr = await address_of(idb)
    files = [("S.pdf", "application/pdf", 10, "tok-a")]
    item = brevo_item(addr, files=files, auth="fail" if case == "dmarc_fail" else "pass")
    if case == "env_unset":
        monkeypatch.delenv(inbound_email.BREVO_AUTHSERV_ENV)
    if case == "foreign_authserv":
        # a line written by anyone else (another authserv-id) does not count
        item["Headers"] = {"Authentication-Results": "attacker.example; dmarc=pass"}
    msg = inbound_email.parse_brevo_item(item)
    assert msg.auth_pass is False
    result = await inbound_email.process_message(idb, msg)
    assert result.outcome == "unmatched" and brevo[1] == []


def test_brevo_headers_list_shape_and_loop_headers(brevo):
    item = brevo_item(f"x-{'2' * 10}@{inbound_email.INBOUND_DOMAIN}")
    item["Headers"] = [
        {"Name": "Authentication-Results", "Value": f"{AUTHSERV}; dmarc=pass"},
        {"Name": "Auto-Submitted", "Value": "auto-replied"},
    ]
    msg = inbound_email.parse_brevo_item(item)
    assert msg.auth_pass is True
    assert inbound_email.loop_reason(msg) == "auto_submitted"


async def test_brevo_limits_and_missing_key(idb, drive, mails, brevo, monkeypatch):
    store, fetched = brevo
    addr = await address_of(idb)
    big = dashboard_upload.MAX_FILE_BYTES + 1
    item = brevo_item(addr, files=[("Big.pdf", "application/pdf", big, "tok-big")])
    result = await inbound_email.process_message(idb, inbound_email.parse_brevo_item(item))
    assert result.skipped == {"file_too_large": 1} and fetched == []

    inbound_email.reset_state()
    monkeypatch.delenv(inbound_email.BREVO_API_KEY_ENV)
    item = brevo_item(addr, files=[("S.pdf", "application/pdf", 10, "tok-x")])
    result = await inbound_email.process_message(idb, inbound_email.parse_brevo_item(item))
    assert result.accepted == 0 and result.skipped == {"fetch_failed": 1}


def test_brevo_bodies_are_never_read():
    import inspect

    src = inspect.getsource(inbound_email.parse_brevo_item)
    code = src.split('"""', 2)[2]
    for name in ("RawHtmlBody", "RawTextBody", "ExtractedMarkdownMessage"):
        assert f'"{name}"' not in code
