# ruff: noqa: F811 - the #950 fixtures are imported and used as test arguments
"""#976: the quarantine card shows a preview of each attachment and the text
of the mail, so the caregiver can decide.

Covers: the mail text stored AES-GCM (own associated data), capped, HTML
reduced to text, never in logs or the /q page, only for the caregiver; the
Brevo queue carries it only for untrusted rows and drops it when the job
ends; the preview route's ACL (foreign = unknown = 404, admin and operator
403), integrity (sha256), types (415), a re-encoded JPEG (never the original
bytes) and its headers; the purge clears the text; the AST locks.

Synthetic only: invented addresses, file names, texts and dates in 2027.
"""

from __future__ import annotations

import ast
import base64
import json
import logging
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.exceptions import InvalidTag

from oncofiles import inbound_body, inbound_email, inbound_quarantine, inbound_queue, server
from tests.test_950_inbound_address import (  # noqa: F401 - fixtures used by name
    CARER,
    OTHER_CARER,
    PID,
    STRANGER,
    _call,
    _dash_request,
    _env,
    address_of,
    brevo,
    brevo_item,
    brevo_request,
    drive,
    idb,
    mails,
    parsed,
)
from tests.test_950_quarantine import (  # noqa: F401 - autouse fixture + helpers
    _quarantine_env,
    ago,
    get_page,
    links,
    notices,
    rows,
)
from tests.test_952_inbound_queue import no_kick  # noqa: F401 - fixture

SRC = Path(server.__file__).parent
BODY = "Dobry den, posielame vysledky. Synteticky text 2027 c. 4711."
PDF_NAME = "SynthPreviewReport2027.pdf"
PNG_NAME = "SynthPreviewPhoto2027.png"
NOTE_NAME = "SynthPreviewNote2027.txt"
OTHER_ITEM_UUID = "11111111-2027-4000-8000-000000000976"


def _pdf(label: str = "Synthetic page 2027") -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=300, height=420)
    page.insert_text((30, 60), label)
    data = doc.tobytes()
    doc.close()
    return data


def _png(seed: int = 1) -> bytes:
    import random

    import pymupdf

    # bigger than a signature image (10 KB, 200 px): noise does not compress
    width, height = 320 + seed, 240
    rng = random.Random(seed)
    samples = bytes(rng.randrange(256) for _ in range(width * height * 3))
    pix = pymupdf.Pixmap(pymupdf.csRGB, width, height, samples, False)
    return pix.tobytes("png")


def _mail(to: str, *, files, text: str | None = BODY, html: str | None = None, sender=STRANGER):
    msg = EmailMessage()
    msg["From"] = f"Synthetic Sender <{sender}>"
    msg["To"] = to
    msg["Subject"] = "Synteticky predmet 2027"
    msg["Message-ID"] = (
        f"<preview-{abs(hash((to, tuple(f[0] for f in files), text)))}@example.test>"
    )
    if text is not None:
        msg.set_content(text)
        if html is not None:
            msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    for name, data, ctype in files:
        main, sub = ctype.split("/", 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return msg


async def _quarantine(db, files=None, **kw) -> dict:
    addr = await address_of(db)
    files = files or [(PDF_NAME, _pdf(), "application/pdf")]
    result = await inbound_email.process_message(db, parsed(_mail(addr, files=files, **kw)))
    assert result.outcome == "quarantined", result
    [item] = await rows(db, "SELECT * FROM inbound_quarantine ORDER BY id DESC LIMIT 1")
    return item


def _preview_request(db, *, patient="synth950-a", item_id=1, n=0, size="thumb"):
    request = MagicMock()
    request.query_params = {
        k: str(v)
        for k, v in {"patient_id": patient, "item_id": item_id, "n": n, "size": size}.items()
        if v is not None
    }
    request.path_params = {}
    request.headers = {"authorization": "Bearer session:synthetic"}
    request.cookies = {}
    request.app.state.fastmcp_server._lifespan_result = {"db": db}
    request.json = AsyncMock(return_value={})
    return request


async def _preview(db, email=CARER, **kw):
    opts = {k: kw.pop(k) for k in ("admin", "bearer") if k in kw}
    return await _call(
        server.api_inbound_quarantine_preview, _preview_request(db, **kw), email, **opts
    )


# ── the mail text ────────────────────────────────────────────────────────


async def test_text_is_stored_encrypted_with_its_own_associated_data(idb, drive, mails):
    item = await _quarantine(idb)
    assert item["body_enc"] and BODY not in item["body_enc"]
    assert BODY.encode() not in base64.b64decode(item["body_enc"])
    assert inbound_quarantine.body_text(item) == BODY
    # bound to its item and field: under another field or item it does not open
    with pytest.raises(InvalidTag):
        inbound_quarantine.unseal(item["body_enc"], PID, item["item_uuid"], "sender")
    with pytest.raises(InvalidTag):
        inbound_quarantine.unseal(item["body_enc"], PID, OTHER_ITEM_UUID, "body")
    # nothing else holds it in clear
    for value in item.values():
        assert BODY not in str(value)


async def test_text_is_capped_at_a_character_boundary(idb, drive, mails):
    long_text = ("Ľúbezný šťavnatý text 2027. " * 800).strip()
    item = await _quarantine(idb, text=long_text)
    text = inbound_quarantine.body_text(item)
    assert len(text.encode("utf-8")) <= inbound_body.BODY_MAX_BYTES
    assert text.endswith(inbound_body.TRUNCATED_MARKER)
    assert text.startswith("Ľúbezný šťavnatý")
    text.encode("utf-8").decode("utf-8")  # no broken character


def test_html_is_reduced_to_plain_text():
    html = (
        "<html><head><title>T</title><style>p{color:red}</style></head><body>"
        "<script>alert('x2027')</script><p>Dobr&yacute; de&#328;,</p>"
        "<p>v&nbsp;pr&iacute;lohe <a href='https://evil.example/x?y=1'>v&yacute;sledky</a>"
        "<img src='https://evil.example/pixel.gif'></p><div>S&nbsp;pozdravom</div></body></html>"
    )
    text = inbound_body.reduce_body(None, html)
    assert "Dobrý deň," in text and "výsledky" in text and "S pozdravom" in text
    for gone in ("<", ">", "alert", "x2027", "color", "evil.example", "pixel", "href"):
        assert gone not in text
    # text/plain wins when both are present
    assert inbound_body.reduce_body("Plain 2027", html) == "Plain 2027"
    assert inbound_body.reduce_body("", "") == "" and inbound_body.reduce_body(None, None) == ""


def test_control_characters_and_blank_runs_are_cleaned():
    text = inbound_body.reduce_body("a\x00b‮\r\n\r\n\r\n\r\nc  \t d", None)
    assert text == "ab\n\nc d"


async def test_html_only_mail_is_stored_as_text(idb, drive, mails):
    item = await _quarantine(idb, text=None, html="<p>Iba <b>HTML</b> 2027</p><script>x()</script>")
    assert inbound_quarantine.body_text(item) == "Iba HTML 2027"


def test_brevo_item_text_and_html_fallback():
    base = brevo_item("x-abcdef@docs.oncofiles.com", sender=STRANGER)
    base["RawTextBody"] = "  Text 2027  "
    assert inbound_email.parse_brevo_item(base).body_text == "Text 2027"
    base["RawTextBody"] = ""
    base["RawHtmlBody"] = "<p>Html&nbsp;2027</p>"
    assert inbound_email.parse_brevo_item(base).body_text == "Html 2027"


async def test_text_never_in_logs_the_q_page_or_the_notice(idb, drive, mails, caplog):
    caplog.set_level(logging.DEBUG)
    item = await _quarantine(idb)
    await _call(server.api_inbound_quarantine, _dash_request(idb, "synth950-a"), CARER)
    await _preview(idb, item_id=item["id"])
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (BODY, PDF_NAME, STRANGER):
        assert secret not in logged
    [notice] = notices(mails)
    assert BODY not in notice["text"]
    approve = links(notice["text"])[0]
    page = (await get_page(idb, approve)).body.decode()
    assert BODY not in page and "<img" not in page and PDF_NAME not in page


async def test_caregiver_sees_text_and_preview_flags_admin_and_operator_do_not(idb, drive, mails):
    await _quarantine(
        idb,
        files=[
            (PDF_NAME, _pdf(), "application/pdf"),
            (NOTE_NAME, b"Synthetic note 2027\n", "text/plain"),
        ],
    )
    resp = await _call(server.api_inbound_quarantine, _dash_request(idb, "synth950-a"), CARER)
    [item] = json.loads(resp.body)["items"]
    assert item["body_text"] == BODY
    assert [f["preview"] for f in item["files"]] == [True, False]
    for kw in ({"email": "admin950@example.test", "admin": True}, {"email": None, "bearer": True}):
        email = kw.pop("email")
        other = await _call(
            server.api_inbound_quarantine, _dash_request(idb, "synth950-a"), email, **kw
        )
        data = json.loads(other.body)
        assert data["items"] == [] and data["read_only"] is True
        assert BODY not in other.body.decode()


# ── the preview route ────────────────────────────────────────────────────


async def test_preview_is_a_reencoded_jpeg_with_safe_headers(idb, drive, mails):
    original = _png(3)
    item = await _quarantine(idb, files=[(PNG_NAME, original, "image/png")])
    for size in ("thumb", "large"):
        resp = await _preview(idb, item_id=item["id"], size=size)
        assert resp.status_code == 200
        assert resp.body[:3] == b"\xff\xd8\xff" and resp.body != original
        assert original not in resp.body
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["content-disposition"].startswith("inline")
        assert "default-src 'none'" in resp.headers["content-security-policy"]


async def test_pdf_first_page_preview(idb, drive, mails):
    original = _pdf("Synthetic first page 2027")
    item = await _quarantine(idb, files=[(PDF_NAME, original, "application/pdf")])
    resp = await _preview(idb, item_id=item["id"])
    assert resp.status_code == 200 and resp.body[:3] == b"\xff\xd8\xff"
    assert b"%PDF" not in resp.body


async def test_preview_acl_foreign_equals_unknown_and_read_only_is_refused(idb, drive, mails):
    item = await _quarantine(idb)
    foreign = await _preview(idb, patient="synth950-b", item_id=item["id"])
    unknown = await _preview(idb, patient="synth950-zz", item_id=item["id"])
    assert foreign.status_code == unknown.status_code == 404 and foreign.body == unknown.body
    other = await _preview(idb, OTHER_CARER, item_id=item["id"])
    assert other.status_code == 404
    admin = await _preview(idb, "admin950@example.test", admin=True, item_id=item["id"])
    assert admin.status_code == 403 and json.loads(admin.body)["error"] == "read_only"
    operator = await _preview(idb, None, bearer=True, item_id=item["id"])
    assert operator.status_code == 403
    # an item id that is not this patient's is the same 404 as a foreign patient
    wrong = await _preview(idb, item_id=item["id"] + 999)
    assert wrong.status_code == 404 and wrong.body == foreign.body
    out_of_range = await _preview(idb, item_id=item["id"], n=5)
    assert out_of_range.status_code == 404 and out_of_range.body == foreign.body


async def test_preview_of_another_patients_item_through_own_patient_is_404(idb, drive, mails):
    item = await _quarantine(idb)
    await idb.db.execute(
        "UPDATE inbound_quarantine SET patient_id = ? WHERE id = ?",
        ("00000000-0000-4000-8000-000000000950", item["id"]),
    )
    await idb.db.commit()
    resp = await _preview(idb, item_id=item["id"])
    assert resp.status_code == 404


async def test_sha_mismatch_is_refused(idb, drive, mails, tmp_path):
    item = await _quarantine(idb)
    blob = inbound_quarantine._encrypt(
        _pdf("Swapped 2027"), inbound_quarantine._aad(PID, item["item_uuid"], "file|0")
    )
    (tmp_path / "quarantine" / item["item_uuid"] / "0.bin").write_bytes(blob)
    resp = await _preview(idb, item_id=item["id"])
    assert resp.status_code == 409 and json.loads(resp.body)["error"] == "integrity"
    assert resp.headers["cache-control"] == "no-store"


async def test_non_image_is_415(idb, drive, mails):
    item = await _quarantine(idb, files=[(NOTE_NAME, b"Synthetic note 2027\n", "text/plain")])
    resp = await _preview(idb, item_id=item["id"])
    assert resp.status_code == 415 and b"Synthetic note" not in resp.body


async def test_mismatching_bytes_are_never_rendered(idb, drive, mails, tmp_path):
    """The manifest says PDF; bytes that sniff as anything else (SVG, HTML)
    are refused: the type is decided by magic bytes, never by the name."""
    import hashlib

    item = await _quarantine(idb)
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>x()</script></svg>"
    files = inbound_quarantine.unseal(item["files_enc"], PID, item["item_uuid"], "files")
    files[0]["sha256"] = hashlib.sha256(svg).hexdigest()
    await idb.db.execute(
        "UPDATE inbound_quarantine SET files_enc = ? WHERE id = ?",
        (inbound_quarantine.seal(files, PID, item["item_uuid"], "files"), item["id"]),
    )
    await idb.db.commit()
    blob = inbound_quarantine._encrypt(
        svg, inbound_quarantine._aad(PID, item["item_uuid"], "file|0")
    )
    (tmp_path / "quarantine" / item["item_uuid"] / "0.bin").write_bytes(blob)
    resp = await _preview(idb, item_id=item["id"])
    assert resp.status_code == 415 and b"svg" not in resp.body


async def test_decided_and_purged_items_have_no_preview(idb, drive, mails):
    item = await _quarantine(idb)
    await inbound_quarantine.reject(idb, PID, item["id"], decided_by="h", via="dashboard")
    assert (await _preview(idb, item_id=item["id"])).status_code == 200  # in the trash
    await idb.db.execute(
        "UPDATE inbound_quarantine SET decided_at = ? WHERE id = ?", (ago(days=31), item["id"])
    )
    await idb.db.commit()
    await inbound_quarantine.purge_due(idb)
    assert (await _preview(idb, item_id=item["id"])).status_code == 404
    [after] = await rows(idb, "SELECT body_enc, state FROM inbound_quarantine")
    assert after == {"body_enc": None, "state": "purged"}


async def test_bad_query_is_400(idb, drive, mails):
    item = await _quarantine(idb)
    assert (await _preview(idb, item_id=item["id"], size="huge")).status_code == 400
    assert (await _preview(idb, item_id="x")).status_code == 400


async def test_decision_voids_the_unused_links(idb, drive, mails):
    item = await _quarantine(idb)
    tokens = await rows(idb, "SELECT used_at FROM inbound_quarantine_tokens")
    assert tokens and all(t["used_at"] is None for t in tokens)
    await inbound_quarantine.reject(idb, PID, item["id"], decided_by="h", via="dashboard")
    tokens = await rows(idb, "SELECT used_at FROM inbound_quarantine_tokens")
    assert all(t["used_at"] for t in tokens)


async def test_expiry_voids_the_unused_links(idb, drive, mails):
    item = await _quarantine(idb)
    await idb.db.execute(
        "UPDATE inbound_quarantine SET received_at = ? WHERE id = ?", (ago(days=40), item["id"])
    )
    await idb.db.commit()
    await inbound_quarantine.run_maintenance(idb)
    [row] = await rows(idb, "SELECT state FROM inbound_quarantine")
    assert row["state"] == "expired"
    tokens = await rows(idb, "SELECT used_at FROM inbound_quarantine_tokens")
    assert tokens and all(t["used_at"] for t in tokens)


# ── the Brevo queue ──────────────────────────────────────────────────────


def test_payload_carries_the_text_only_when_asked():
    msg = inbound_email.InboundMessage(recipients=[], sender=STRANGER, auth_pass=True)
    msg.body_text = BODY
    assert "body_text" not in inbound_queue.to_payload(msg, "synthetictoken")
    kept = inbound_queue.to_payload(msg, "synthetictoken", keep_body=True)
    assert kept["body_text"] == BODY
    assert inbound_queue.from_payload(kept).body_text == BODY


async def _job_payloads(db):
    async with db.db.execute(
        "SELECT patient_id, token_hash, job_uuid, payload_enc, state FROM inbound_jobs"
    ) as cur:
        jobs = [dict(r) for r in await cur.fetchall()]
    return [
        (
            j["state"],
            inbound_queue.decrypt_payload(
                j["payload_enc"],
                patient_id=j["patient_id"],
                token_hash=j["token_hash"],
                job_uuid=j["job_uuid"],
            ),
        )
        for j in jobs
    ]


async def test_queue_keeps_the_text_of_an_untrusted_mail_until_the_job_ends(
    idb, drive, mails, brevo, no_kick
):
    store, _ = brevo
    addr = await address_of(idb)
    store["tok-q"] = _pdf("Queued 2027")
    item = brevo_item(
        addr, sender=STRANGER, files=[(PDF_NAME, "application/pdf", len(store["tok-q"]), "tok-q")]
    )
    item["RawTextBody"] = BODY
    trusted = brevo_item(addr, sender=CARER, files=[])
    trusted["MessageId"] = "<trusted-976-2027@example.test>"
    trusted["RawTextBody"] = "Caregiver text 2027"
    await server.inbound_brevo_webhook(brevo_request(idb, {"items": [item, trusted]}))
    before = {p["sender"]: p for _s, p in await _job_payloads(idb)}
    assert before[STRANGER]["body_text"] == BODY
    assert "body_text" not in before[CARER]
    await inbound_queue.run_pending(idb)
    for state, payload in await _job_payloads(idb):
        assert state == "done" and "body_text" not in payload
    [row] = await rows(idb, "SELECT * FROM inbound_quarantine")
    assert inbound_quarantine.body_text(row) == BODY


# ── locks ────────────────────────────────────────────────────────────────


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


FORBIDDEN = ("llm", "extract", "indexer", "holding", "enhance", "ocr", "curated")


def test_preview_module_reaches_only_the_safe_renderer():
    imported = _imports(SRC / "quarantine_preview.py")
    leaf = {m.split(".")[-1] for m in imported}
    assert "doc_thumbnails" in leaf and "inbound_quarantine" in leaf
    assert not [m for m in imported if any(f in m.split(".")[-1] for f in FORBIDDEN)], imported


def test_only_the_preview_module_imports_a_renderer_for_quarantined_files():
    """``inbound_quarantine`` imports no renderer; ``quarantine_preview`` is
    imported only by the route in ``server``."""
    q = _imports(SRC / "inbound_quarantine.py")
    assert not [
        m
        for m in q
        if any(f in m.split(".")[-1] for f in ("doc_thumbnails", "pymupdf", "quarantine_preview"))
    ]
    users = [
        p.name
        for p in SRC.rglob("*.py")
        if p.name != "quarantine_preview.py"
        and any(m.split(".")[-1] == "quarantine_preview" for m in _imports(p))
    ]
    assert users == ["server.py"]


def test_migration_is_additive():
    sql = (SRC.parent.parent / "migrations" / "103_inbound_quarantine_body.sql").read_text()
    code = [ln for ln in sql.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    assert code == ["ALTER TABLE inbound_quarantine ADD COLUMN body_enc TEXT;"]


def test_dashboard_renders_text_and_previews_safely():
    js = (SRC / "dashboard.js").read_text()
    section = js[
        js.index("// #950 quarantine: attachments") : js.index("// Admin zone: mail to patient")
    ]
    assert "innerHTML" not in section and "confirm(" not in section
    assert "box.textContent = text;" in section
    assert "Text e-mailu" in section and "Text of the e-mail" in section
    assert "/api/inbound-quarantine/preview" in section
    assert "doc-card quarantine-tile" in section and "doc-grid quarantine-grid" in section
    assert "if (path === '/api/inbound-quarantine/preview')" in js


def test_demo_twin_has_text_and_previews():
    from oncofiles import demo_dashboard

    built = demo_dashboard.build([], "0.0.0-test")
    [item] = built["inbound_quarantine"]["items"]
    assert item["body_text"] and item["body_text_en"] and item["files"][0]["preview"] is True
    assert built["inbound_quarantine_previews"]["1:0"]["src"].startswith("data:image/svg+xml")
