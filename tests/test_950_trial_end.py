"""#950: what happens when a welcome@ trial ends (maintainer decision 2026-11-26).

After ``trial_ends_at`` a trial that is not converted is read-only: the holding
reader stops (also mid-document), new writes are refused, and the person keeps
read access (document list, download, the #856 ZIP export). ONE end-of-trial
e-mail goes out (bilingual, connect Drive / export / reply for deletion; no
name, file, count, token or signed link), claimed once per trial in
``trial_accounts.end_mail_sent_at``. A converted trial, a trial with a
connected Drive folder and an active trial get no mail. Nothing is deleted.

Synthetic only: invented addresses at example.com, invented bytes and names,
a clock fixed in 2031. No network: SMTP is ``tests/fake_smtp.py``.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from oncofiles import holding_extract, holding_move, holding_store, trial_funnel
from oncofiles.curated.extract import Extraction
from oncofiles.database import Database
from oncofiles.models import OAuthToken
from tests import fake_smtp
from tests.helpers import PRIMARY_TEST_UUID

A = PRIMARY_TEST_UUID  # the trial that ends
B = "00000000-0000-4000-8000-0000000e0950"  # an active trial
A_EMAIL = "ended.trial950@example.com"
B_EMAIL = "active.trial950@example.com"
A_NAME = "Invented Ended Person"
FILE_A = "SynthEndedLab2027.jpg"
FILE_B = "SynthActiveLab2027.jpg"
HOLD_SECRET = "synthetic-holding-secret-950-end"

NOW = datetime(2031, 8, 17, 9, 0, tzinfo=UTC)
ENDED = "2031-07-13T08:41:17Z"
ACTIVE = "2031-09-29T08:41:17Z"
_clock = {"now": NOW}
_hook: dict = {"fn": None}  # called inside the stub extractor (worker thread)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOLDING_STORE_SECRET", HOLD_SECRET)
    monkeypatch.setattr("oncofiles.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "synthetic-bearer-950-end")
    monkeypatch.delenv("CURATED_DAILY_PAGE_BUDGET", raising=False)
    monkeypatch.setattr(holding_store, "_free_bytes", lambda: 10**15)
    _clock["now"] = NOW
    _hook["fn"] = None
    monkeypatch.setattr(holding_store, "_now_iso", lambda: _iso(_clock["now"]))
    monkeypatch.setattr(trial_funnel, "_now_iso", lambda: _iso(_clock["now"]))
    monkeypatch.setattr(holding_extract, "_now", lambda: _clock["now"])
    monkeypatch.setattr("oncofiles.tools.curated.run_active", lambda: False)
    monkeypatch.setattr("oncofiles.tools.curated.probe_stats", lambda: {})
    trial_funnel.reset_state()
    holding_extract.reset_state()
    holding_extract._drain_task = None
    yield tmp_path
    trial_funnel.reset_state()
    holding_extract._drain_task = None


@pytest.fixture
def model(monkeypatch):
    """The local model: configured, and a stub extractor that records calls."""
    calls: list[dict] = []

    def _extract(name, content, mime_type="", **kw):
        calls.append({"name": name, "bytes": len(content), "mime": mime_type})
        if _hook["fn"] is not None:
            _hook["fn"]()
        return Extraction(
            "<!-- page 1 -->\nInvented line of a synthetic sheet", "vision", 1, "synthetic"
        )

    monkeypatch.setattr("oncofiles.curated.llm.configured", lambda: True)
    monkeypatch.setattr("oncofiles.curated.extract.extract", _extract)
    return calls


@pytest.fixture
def smtp(monkeypatch):
    return fake_smtp.install(monkeypatch)


async def _patient(db: Database, pid: str, email: str, slug: str) -> None:
    if pid == A:
        await db.db.execute(
            "UPDATE patients SET slug = ?, caregiver_email = ?, display_name = ? "
            "WHERE patient_id = ?",
            (slug, email, A_NAME, pid),
        )
    else:
        await db.db.execute(
            "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
            "VALUES (?,?,?,?)",
            (pid, slug, "Invented Active Person", email),
        )
    await db.db.commit()


async def _trial(db: Database, pid: str, ends: str) -> None:
    await db.db.execute(
        "INSERT INTO trial_accounts (patient_id, sender_hash, source, trial_ends_at) "
        "VALUES (?, ?, 'welcome_email', ?)",
        (pid, f"synthetic-end-hash-{pid[-4:]}", ends),
    )
    await db.db.commit()


def _jpeg(n: int) -> bytes:
    return b"\xff\xd8\xff\xe0" + bytes([n % 251]) * 30_000


def _pdf(pages: int) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=210, height=297)
        page.insert_text((20, 40), f"Invented page {i + 1} of a synthetic sheet")
    data = doc.tobytes()
    doc.close()
    return data


async def _store(db: Database, pid: str, name: str, data: bytes, mime: str) -> int:
    """Store while the trial is active (the clock before both ends)."""
    _clock["now"] = datetime(2031, 5, 11, tzinfo=UTC)
    try:
        return await holding_store.store_document(db, pid, name, data, mime)
    finally:
        _clock["now"] = NOW


async def _state(db: Database, doc_id: int) -> dict:
    async with db.db.execute(
        "SELECT structured_metadata FROM documents WHERE id = ?", (doc_id,)
    ) as cur:
        return json.loads((await cur.fetchone())[0])["holding"]


async def _two_trials(db: Database) -> tuple[int, int]:
    await _patient(db, A, A_EMAIL, "trial-end950a")
    await _patient(db, B, B_EMAIL, "trial-end950b")
    await _trial(db, A, ENDED)
    await _trial(db, B, ACTIVE)
    a_doc = await _store(db, A, FILE_A, _jpeg(1), "image/jpeg")
    b_doc = await _store(db, B, FILE_B, _jpeg(2), "image/jpeg")
    return a_doc, b_doc


def _messages(instances) -> list:
    return [msg for smtp in instances for msg, _to in smtp.sent]


def _plain(msg) -> str:
    return msg.get_body(("plain",)).get_content()


# ── processing stops; an active trial is unaffected ─────────────────────


async def test_ended_trial_gets_no_further_extraction(db, model):
    a_doc, b_doc = await _two_trials(db)
    counts = await holding_extract.run_pending(db)
    assert counts["done"] == 1
    assert len(model) == 1  # only the active trial's document was read
    assert (await _state(db, b_doc))["extract"] == "done"
    assert (await _state(db, a_doc))["extract"] == "pending"
    # later runs never pick it up either
    await holding_extract.run_pending(db)
    assert len(model) == 1
    assert (await _state(db, a_doc))["extract"] == "pending"


async def test_trial_ending_mid_document_stops_before_the_next_batch(db, model):
    await _patient(db, A, A_EMAIL, "trial-end950a")
    await _trial(db, A, "2031-08-17T10:00:00Z")  # one hour after NOW
    pages = holding_extract.PAGE_BATCH + 1
    doc_id = await _store(db, A, "SynthLong2027.pdf", _pdf(pages), "application/pdf")

    def _end_now():
        _clock["now"] = NOW + timedelta(hours=2)

    _hook["fn"] = _end_now
    counts = await holding_extract.run_pending(db)
    assert len(model) == 1  # the second batch was never sent to the model
    assert counts["trial_ended"] == 1 and not counts.get("done")
    state = await _state(db, doc_id)
    assert state["extract"] == "pending" and int(state.get("attempts") or 0) == 0
    async with db.db.execute(
        "SELECT COUNT(*) FROM document_pages WHERE document_id = ?", (doc_id,)
    ) as cur:
        assert (await cur.fetchone())[0] == 0
    await holding_extract.run_pending(db)
    assert len(model) == 1


async def test_ended_trial_refuses_new_writes(db):
    await _patient(db, A, A_EMAIL, "trial-end950a")
    await _trial(db, A, ENDED)
    with pytest.raises(holding_store.HoldingRefusedError) as refused:
        await holding_store.store_document(db, A, "SynthLate.jpg", _jpeg(3), "image/jpeg")
    assert refused.value.code == "trial_ended"


def test_trial_ended_predicate():
    assert holding_store.trial_ended({"trial_ends_at": ENDED}) is True
    assert holding_store.trial_ended({"trial_ends_at": ACTIVE}) is False
    converted = {"trial_ends_at": ENDED, "converted_at": "2031-06-23T07:13:29Z"}
    assert holding_store.trial_ended(converted) is False
    assert holding_store.trial_ended(None) is False


# ── read access stays ───────────────────────────────────────────────────


async def test_read_access_after_the_end(db):
    from oncofiles import data_rights
    from oncofiles.tools._helpers import _try_download

    await _patient(db, A, A_EMAIL, "trial-end950a")
    await _trial(db, A, ENDED)
    data = _jpeg(4)
    doc_id = await _store(db, A, FILE_A, data, "image/jpeg")
    assert holding_store.trial_ended(await db.get_trial(patient_id=A))
    # the document list (dashboard + MCP list tools)
    listed = await db.list_documents(patient_id=A)
    assert [d.id for d in listed] == [doc_id]
    # download / view_document of the person's own file
    doc = await db.get_document(doc_id, patient_id=A)
    ok, _content, raw = await _try_download(MagicMock(), doc, None)
    assert ok is True and raw == data
    # the #856 ZIP export carries the held original
    chunks = [c async for c in data_rights.export_stream(db, A)]
    zf = zipfile.ZipFile(io.BytesIO(b"".join(chunks)))
    originals = [n for n in zf.namelist() if n.startswith("originals/")]
    assert len(originals) == 1 and zf.read(originals[0]) == data
    assert json.loads(zf.read("manifest.json"))["originals"] == {"included": 1, "unavailable": 0}


# ── the one end-of-trial e-mail ─────────────────────────────────────────


async def test_ended_trial_gets_exactly_one_mail(db, smtp):
    await _two_trials(db)
    counts = await holding_move.run_pending(db)
    assert counts["end_mails"] == 1
    [msg] = _messages(smtp)
    assert msg["To"] == A_EMAIL
    assert msg["Reply-To"] == fake_smtp.SMTP_ENV["MAIL_REPLY_TO"]  # deletion = a reply
    assert msg["Auto-Submitted"] is None  # the person is asked to reply
    assert "trial_ended" in msg["X-Mailin-Tag"]
    subject, text = msg["Subject"], _plain(msg)
    assert "skončila" in subject and "ended" in subject
    # bilingual, the three options, a plain unsigned dashboard link
    assert "skúšobná doba" in text and "trial has ended" in text
    assert "Google Drive" in text and "ZIP" in text
    assert "odpovedzte na tento e-mail" in text and "reply to this e-mail" in text
    assert trial_funnel.DASHBOARD_URL in text
    assert trial_funnel.DASHBOARD_URL + "?" not in text  # no query string
    assert "/q/" not in text and "token" not in text.lower()
    # no patient data
    for leak in (A_NAME, FILE_A, "trial-end950a", A, "holding:", ENDED[:10]):
        assert leak not in text and leak not in subject
    trial = await db.get_trial(patient_id=A)
    assert trial["end_mail_sent_at"] == _iso(NOW)
    assert (await db.get_trial(patient_id=B))["end_mail_sent_at"] is None
    # idempotent across runs
    await holding_move.run_pending(db)
    assert await trial_funnel.send_due_end_mails(db) == 0
    trial_funnel.reset_state()  # even a fresh process sends nothing again
    assert await trial_funnel.send_due_end_mails(db) == 0
    assert len(_messages(smtp)) == 1


async def test_a_refused_end_mail_is_retried_later(db, monkeypatch):
    await _patient(db, A, A_EMAIL, "trial-end950a")
    await _trial(db, A, ENDED)
    fake_smtp.install(monkeypatch, fail_with=OSError("synthetic relay down"))
    assert await trial_funnel.send_due_end_mails(db) == 0
    assert (await db.get_trial(patient_id=A))["end_mail_sent_at"] is None  # claim released
    assert await trial_funnel.send_due_end_mails(db) == 0  # within the retry interval
    instances = fake_smtp.install(monkeypatch)
    trial_funnel._end_last_failed.clear()  # the retry interval has passed
    assert await trial_funnel.send_due_end_mails(db) == 1
    assert len(_messages(instances)) == 1
    assert (await db.get_trial(patient_id=A))["end_mail_sent_at"]


async def test_converted_trial_gets_no_mail(db, smtp):
    await _patient(db, A, A_EMAIL, "trial-end950a")
    await _trial(db, A, ENDED)
    await db.db.execute(
        "UPDATE trial_accounts SET converted_at = ? WHERE patient_id = ?",
        ("2031-06-23T07:13:29Z", A),
    )
    await db.db.commit()
    assert await trial_funnel.send_due_end_mails(db) == 0
    assert _messages(smtp) == []
    assert (await db.get_trial(patient_id=A))["end_mail_sent_at"] is None


async def test_trial_with_connected_drive_gets_no_mail(db, smtp):
    """Drive connected, the move still running: converting, not 'ended'."""
    await _patient(db, A, A_EMAIL, "trial-end950a")
    await _trial(db, A, ENDED)
    await db.upsert_oauth_token(
        OAuthToken(
            patient_id=A,
            access_token="synthetic-access",
            refresh_token="synthetic-refresh",
            token_expiry=datetime.now(UTC) + timedelta(days=1),
            gdrive_folder_id="idSynthRoot950End1",
        )
    )
    assert await trial_funnel.send_due_end_mails(db) == 0
    assert _messages(smtp) == []


async def test_active_trial_gets_no_mail(db, smtp):
    await _patient(db, B, B_EMAIL, "trial-end950b")
    await _trial(db, B, ACTIVE)
    assert await trial_funnel.send_due_end_mails(db) == 0
    assert _messages(smtp) == []


async def test_archived_patient_gets_no_mail(db, smtp):
    await _patient(db, A, A_EMAIL, "trial-end950a")
    await _trial(db, A, ENDED)
    await db.db.execute("UPDATE patients SET is_active = 0 WHERE patient_id = ?", (A,))
    await db.db.commit()
    assert await trial_funnel.send_due_end_mails(db) == 0
    assert _messages(smtp) == []


async def test_end_mails_are_rate_limited(db, smtp, monkeypatch):
    monkeypatch.setattr(trial_funnel, "END_MAILS_PER_HOUR", 0)
    await _patient(db, A, A_EMAIL, "trial-end950a")
    await _trial(db, A, ENDED)
    assert await trial_funnel.send_due_end_mails(db) == 0
    assert (await db.get_trial(patient_id=A))["end_mail_sent_at"] is None


def test_end_text_is_bilingual_and_carries_no_placeholder():
    subject, body = trial_funnel.end_text()
    sk, en = body.split("\n---\n", 1)
    assert "Dobrý deň" in sk and "Hello" in en
    for part in (sk, en):
        assert trial_funnel.DASHBOARD_URL in part
        for option in "123":
            assert part.count(f"\n{option}. ") == 1
    assert "{" not in body and "waitlist" not in body.lower()


async def test_funnel_counts_end_mails(db, smtp):
    await _two_trials(db)
    await trial_funnel.send_due_end_mails(db)
    out = trial_funnel.funnel(await db.trial_funnel_rows())
    assert out["trials"] == 2 and out["end_mail_sent"] == 1


async def test_nothing_is_deleted_when_a_trial_ends(db, smtp, model, env):
    a_doc, _ = await _two_trials(db)
    files_before = sorted(p.name for p in (env / "holding").iterdir())
    await holding_extract.run_pending(db)
    await holding_move.run_pending(db)
    assert sorted(p.name for p in (env / "holding").iterdir()) == files_before
    doc = await db.get_document(a_doc, patient_id=A)
    assert doc is not None and doc.deleted_at is None
    assert await db.get_trial(patient_id=A) is not None
