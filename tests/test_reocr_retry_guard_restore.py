"""Re-OCR near-empty pages, stricter guard and sidecar restore (#806).

* a near-empty pass whose completion was almost all reasoning is retried ONCE
  for that page with thinking off (the per-call reasoning marker); the retry's
  text is kept only when longer; the reasoning channel is never a transcript;
* a blank back side (little ink, no text layer) is not retried and never
  counts toward ``near_empty_with_loss``;
* ``text_shrunk`` and ``low_word_overlap`` stop a rewrite;
* ``curated_restore_sidecar`` puts a re-OCR backup back (backup of the current
  sidecar first, verified update, re-index, one activity_log row).

Synthetic data only (invented values, dates 2027+)."""

from __future__ import annotations

import contextlib
import inspect
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.curated import drive_ops, llm, reocr
from oncofiles.curated.drive_ops import WriteVerificationError
from oncofiles.curated.facts_rules import Vocabulary
from oncofiles.secrets_keys import caller_email_hash
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID, TEST_PATIENT_UUID
from tests.test_curated_reocr import NOW
from tests.test_curated_runner import LABS, LOGS, FakeDrive
from tests.test_reocr_loss_guard import FILLER, OLD_BODY, _drop, _go

LONG = "Leukocyty 7,93 G/l 4,0 - 11\nOdber 14.03.2027 07:40\nHemoglobin 128 g/l"
SENTINEL = "SYNTHETIC REASONING TRANSCRIPT Leukocyty 9,99 G/l " * 4


@pytest.fixture
async def vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


def _pdf(*kinds: str) -> bytes:
    """One page per kind: "ink" = a dark block (a scan without a text layer),
    "blank" = an empty page."""
    import pymupdf

    doc = pymupdf.open()
    for kind in kinds:
        page = doc.new_page()
        if kind == "ink":
            black = pymupdf.pdfcolor["black"]
            page.draw_rect(pymupdf.Rect(61, 83, 413, 297), color=black, fill=black)
    data = doc.tobytes()
    doc.close()
    return data


def _fake_chat(first: str, retry: str, *, reasoning=950, completion=1000, calls=None):
    """``llm.chat`` stand-in: the normal call answers ``first`` with the given
    counts, a thinking-off retry answers ``retry``. Both put a long sentinel
    into the raw reasoning channel, which re-OCR must never use."""
    lock = threading.Lock()

    def chat(system, content, **kw):
        is_retry = kw.get("reasoning_marker") is True
        with lock:
            if calls is not None:
                calls.append({"system": system, "content": content, **kw})
        info = kw["info"]
        info.update(finish_reason="stop", raw_content="", reasoning_content=SENTINEL,
                    prompt_tokens=900, completion_tokens=completion if not is_retry else 300,
                    reasoning_tokens=reasoning if not is_retry else 0)  # fmt: skip
        return retry if is_retry else first

    return chat


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(llm, "model_id", lambda: "qwen-local")


# --- 1. retry with thinking off ---------------------------------------------------


def test_near_empty_reasoning_heavy_pass_is_retried_with_thinking_off(monkeypatch, model):
    calls: list[dict] = []
    monkeypatch.setattr(llm, "chat", _fake_chat("x", LONG, calls=calls))
    rr = reocr.reread("scan.pdf", _pdf("ink"))
    assert rr.error is None and rr.retried_pages == [1]
    assert "Leukocyty 7,93" in rr.pages[0]
    normal = [c for c in calls if "reasoning_marker" not in c]
    retries = [c for c in calls if c.get("reasoning_marker") is True]
    assert len(normal) == len(retries) == 2  # both passes, each retried once
    for r in retries:
        assert r["thinking"] is False  # through the per-call marker, nothing global
    for n, r in zip(normal, retries, strict=True):
        assert r["system"] == n["system"] == reocr.REOCR_SYSTEM_PROMPT
        assert r["content"] == n["content"]  # same page image, same dpi, same prompt
        assert r["max_tokens"] == n["max_tokens"]
    recs = rr.llm_passes
    expected = [(k, retry) for k in (1, 2) for retry in (False, True)]
    assert [(p["pass"], p.get("retry", False)) for p in recs] == expected
    assert all(p["thinking"] == "off" for p in recs if p.get("retry"))
    assert all("thinking" not in p for p in recs if not p.get("retry"))
    dpis = reocr.reocr_dpis(2)
    assert [p["dpi"] for p in recs] == [d for d in dpis for _ in ("normal", "retry")]


@pytest.mark.parametrize(
    "first,reasoning,retried",
    [
        ("x", 801, True),  # 0.801 > 0.8
        ("x", 800, False),  # exactly 0.8 is not above the ratio
        ("x" * reocr.NEAR_EMPTY_CHARS, 1000, False),  # long enough: not near-empty
        ("x" * (reocr.NEAR_EMPTY_CHARS - 1), 1000, True),
    ],
)
def test_retry_depends_on_ratio_and_length(monkeypatch, model, first, reasoning, retried):
    monkeypatch.setattr(llm, "chat", _fake_chat(first, LONG, reasoning=reasoning))
    rr = reocr.reread("scan.pdf", _pdf("ink"), passes=1)
    assert rr.retried_pages == ([1] if retried else [])
    assert any(p.get("retry") for p in rr.llm_passes) is retried


def test_unknown_token_counts_never_retry():
    assert not reocr.needs_retry("x", None, 1000)
    assert not reocr.needs_retry("x", 900, None)
    assert not reocr.needs_retry("x", 900, 0)
    assert reocr.needs_retry("", 900, 1000)


@pytest.mark.parametrize("retry_text", ["", "y", "   "])
def test_retry_text_is_kept_only_when_longer(monkeypatch, model, retry_text):
    monkeypatch.setattr(llm, "chat", _fake_chat("abc", retry_text))
    rr = reocr.reread("scan.pdf", _pdf("ink"), passes=1)
    assert rr.retried_pages == [1] and rr.pages == ["abc"]


def test_a_failed_retry_keeps_the_first_answer(monkeypatch, model):
    def chat(system, content, **kw):
        kw["info"].update(completion_tokens=1000, reasoning_tokens=990)
        if kw.get("reasoning_marker"):
            raise llm.LocalLLMError("local LLM answer truncated: reasoning used the whole budget")
        return "abc"

    monkeypatch.setattr(llm, "chat", chat)
    rr = reocr.reread("scan.pdf", _pdf("ink"), passes=1)
    assert rr.error is None and rr.pages == ["abc"] and rr.retried_pages == [1]
    assert [p.get("error") for p in rr.llm_passes] == [None, "LocalLLMError"]


def test_reasoning_channel_is_never_a_transcript(monkeypatch, model):
    monkeypatch.setattr(llm, "chat", _fake_chat("", ""))
    rr = reocr.reread("scan.pdf", _pdf("ink", "ink"))
    assert rr.retried_pages == [1, 2]
    assert all(p == "" for p in rr.pages)
    blob = json.dumps({k: v for k, v in rr.__dict__.items()}, default=str)
    assert "SYNTHETIC REASONING" not in blob and "9,99" not in blob
    # and the re-OCR module never names the raw reasoning field at all
    assert "reasoning_content" not in inspect.getsource(reocr)


def test_the_verify_only_reread_is_never_retried(monkeypatch, model):
    monkeypatch.setattr(llm, "chat", _fake_chat("x", LONG))
    rr = reocr.reread("scan.pdf", _pdf("ink"), passes=1, purpose="reread")
    assert rr.retried_pages == [] and rr.pages == ["x"]


# --- 2. blank versus miss ------------------------------------------------------------


def test_page_ink_and_text_layer_are_measured(monkeypatch, model):
    monkeypatch.setattr(llm, "chat", _fake_chat(LONG, LONG, reasoning=10))
    rr = reocr.reread("scan.pdf", _pdf("ink", "blank"), passes=1)
    ink, blank = rr.page_ink
    assert ink > reocr.BLANK_INK_MAX and blank == 0.0
    assert rr.page_text_layer == [False, False]
    assert all(isinstance(v, float) and v == round(v, 4) for v in rr.page_ink)
    assert reocr.is_blank(blank, False) and not reocr.is_blank(ink, False)
    assert not reocr.is_blank(0.0, True)  # a text layer is never blank
    assert not reocr.is_blank(None, None)  # unknown is never blank


def test_a_blank_page_is_not_retried(monkeypatch, model):
    calls: list[dict] = []
    monkeypatch.setattr(llm, "chat", _fake_chat("", LONG, calls=calls))
    rr = reocr.reread("scan.pdf", _pdf("blank"), passes=1)
    assert rr.retried_pages == [] and len(calls) == 1 and rr.pages == [""]


def _doc_with(drive, old_body):
    name = "20270412-JanaTestova-LabX-Labs-Kontrola.pdf"
    src = drive.add(name, drive.folder[LABS], b"%PDF synthetic")
    front = "---\ndoc_date: 2027-04-12\ndoc_type: Labs\nextraction: tesseract eng-only\n---\n\n"
    side = drive.add(name + ".md", drive.folder[LABS], (front + old_body).encode())
    return {"document_id": 7, "filename": name, "gdrive_id": src, "sidecar_id": side,
            "engine": "tesseract eng-only", "reasons": ["cowork_ocr"]}  # fmt: skip


@pytest.mark.parametrize("ink,blank", [(0.0, True), (0.2, False)])
async def test_blank_page_causes_no_review_but_an_inked_one_does(vocab, ink, blank):
    drive = FakeDrive()
    doc = _doc_with(drive, OLD_BODY)
    pages = [_drop("286"), ""]  # one lost number and a near-empty second page

    def reader(*a, **k):
        return reocr.Reread(pages, "vision2", "qwen-local", page_ink=[0.31, ink],
                            page_text_layer=[False, False])  # fmt: skip

    rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=False, now=NOW, reader=reader)
    d = rep.documents[0]
    assert d.near_empty_pages == [2] and d.missing_numbers == 1
    assert d.page_ink == [0.31, ink]
    if blank:
        assert d.blank_pages == [2] and d.review_reasons == [] and d.result == "rewritten"
    else:
        assert d.blank_pages == [] and d.review_reasons == ["near_empty_with_loss"]
        assert d.result == "review" and drive.writes == []


# --- 3. stricter guard rules ---------------------------------------------------------


async def test_text_shrunk_goes_to_review_without_a_near_empty_page(vocab):
    old_body = OLD_BODY + "\n".join([FILLER] * 4) + "\n"
    _, d, drive, _, _ = _go(vocab, [_drop()], old_body=old_body)
    assert d.near_empty_pages == [] and d.missing_numbers == 0
    assert d.new_chars < reocr.TEXT_SHRUNK_RATIO * d.old_chars
    assert d.review_reasons == ["text_shrunk"] and d.result == "review"
    assert drive.writes == []


async def test_low_word_overlap_goes_to_review(vocab):
    # every number and date kept, same length, but different words
    other = (
        OLD_BODY.replace("Kontrola zo dna", "Vysledok odberu")
        .replace("Riadok", "Polozka")
        .replace(FILLER, "Iny zaznam nesuvisiaci obsah zhoda nulova pismena navyse.")
    )
    _, d, drive, _, _ = _go(vocab, [other.strip()])
    assert d.missing_numbers == 0 and d.missing_dates == 0
    assert d.word_overlap is not None and d.word_overlap < reocr.MIN_WORD_OVERLAP
    assert "low_word_overlap" in d.review_reasons and d.result == "review"
    assert drive.writes == []


def test_word_overlap_normalisation():
    old = "Kontrola Hemoglobín ok 12,5 Pečeň x1"
    assert reocr.word_overlap(old, "kontrola HEMOGLOBÍN pečeň") == 1.0
    kept, words = ["kontrola"], ["kontrola", "hemoglobín", "pečeň"]
    assert reocr.word_overlap(old, "kontrola hemoglobin pecen") == round(len(kept) / len(words), 3)
    assert reocr.word_overlap("ab cd", "nic") is None  # no word of WORD_MIN_LEN letters


def test_new_thresholds_are_module_constants():
    assert reocr.TEXT_SHRUNK_RATIO == 0.6 and reocr.MIN_WORD_OVERLAP == 0.40
    assert reocr.RETRY_REASONING_RATIO == 0.8 and reocr.NEAR_EMPTY_CHARS == 50
    assert 0 < reocr.BLANK_INK_MAX <= 0.01
    base = {"missing_numbers": 0, "missing_dates": 0, "old_chars": 100}
    assert reocr.review_reasons([], {**base, "new_chars": 60}, []) == []
    assert reocr.review_reasons([], {**base, "new_chars": 59}, []) == ["text_shrunk"]
    loss = {**base, "new_chars": 100, "word_overlap": 0.4}
    assert reocr.review_reasons([], loss, []) == []
    assert reocr.review_reasons([], {**loss, "word_overlap": 0.399}, []) == ["low_word_overlap"]


# --- 4. restore: the Drive core ------------------------------------------------------

DOC_NAME = "20270412-JanaTestova-LabX-Labs-Kontrola.pdf"
CURRENT = "---\ndoc_date: 2027-04-12\n---\n\n<!-- page 1 -->\nRiadok alfa 731,4\n"
BACKUP = (
    "---\ndoc_date: 2027-04-12\n---\n\nKontrola 17.04.2027\nRiadok alfa 731,4\nRiadok beta 8862\n"
)


def _restore_setup():
    drive = FakeDrive()
    src = drive.add(DOC_NAME, drive.folder[LABS], b"%PDF synthetic")
    side = drive.add(DOC_NAME + ".md", drive.folder[LABS], CURRENT.encode())
    backup = drive.add(f"_reocr-backup-20270501-101500-{DOC_NAME}.md", drive.folder[LOGS],
                       BACKUP.encode())  # fmt: skip
    doc = {"document_id": 7, "filename": DOC_NAME, "gdrive_id": src, "sidecar_id": side}
    return drive, doc, side, backup


def test_restore_dry_run_reports_counts_and_writes_nothing():
    drive, doc, side, backup = _restore_setup()
    out = reocr.restore_sidecar(drive, drive.root, doc, backup, dry_run=True, now=NOW)
    assert out["result"] == "previewed" and drive.writes == []
    assert drive.items[side]["content"] == CURRENT.encode()
    assert (out["old_numbers"], out["new_numbers"]) == (1, 2)
    assert (out["old_dates"], out["new_dates"]) == (0, 1)
    assert out["regained_numbers"] == 1 and out["regained_dates"] == 1
    assert out["missing_numbers"] == 0 and out["new_chars"] > out["old_chars"]
    text = json.dumps(out)
    for token in ("731", "8862", "17.04.2027", "Riadok"):
        assert token not in text


def test_restore_backs_up_first_then_updates_verified():
    drive, doc, side, backup = _restore_setup()
    out = reocr.restore_sidecar(drive, drive.root, doc, backup, dry_run=False, now=NOW)
    assert out["result"] == "restored"
    name = f"_restore-backup-{NOW:%Y%m%d-%H%M%S}-{DOC_NAME}.md"
    assert drive.writes == [("create", name), ("update", DOC_NAME + ".md")]
    assert drive.items[out["restore_backup_id"]]["content"] == CURRENT.encode()
    assert drive.items[out["restore_backup_id"]]["parent"] == drive.folder[LOGS]
    assert drive.items[side]["content"] == BACKUP.encode()
    assert drive.items[backup]["content"] == BACKUP.encode()  # the re-OCR backup stays
    assert not any(i["trashed"] for i in drive.items.values())


def test_restore_stops_when_the_backup_copy_cannot_be_verified():
    drive, doc, side, backup = _restore_setup()
    drive.fail_on.add("create_text")
    with pytest.raises(WriteVerificationError):
        reocr.restore_sidecar(drive, drive.root, doc, backup, dry_run=False, now=NOW)
    assert drive.items[side]["content"] == CURRENT.encode()  # never updated


@pytest.mark.parametrize(
    "where,name,code",
    [
        (LABS, f"_reocr-backup-20270501-101500-{DOC_NAME}.md", "backup_not_in_logs"),
        (LOGS, "_reocr-backup-20270501-101500-20270413-Other-Labs-Iny.pdf.md",
         "backup_not_of_this_document"),
        (LOGS, f"_restore-backup-20270501-101500-{DOC_NAME}.md", "backup_not_of_this_document"),
        (LOGS, f"_reocr-text-20270501-{DOC_NAME}.md", "backup_not_of_this_document"),
        (LOGS, f"x_reocr-backup-20270501-{DOC_NAME}.md", "backup_not_of_this_document"),
    ],
)  # fmt: skip
def test_restore_refuses_any_other_file(where, name, code):
    drive, doc, side, _ = _restore_setup()
    other = drive.add(name, drive.folder[where], BACKUP.encode())
    with pytest.raises(reocr.RestoreRefusedError, match=code):
        reocr.restore_sidecar(drive, drive.root, doc, other, dry_run=False, now=NOW)
    with pytest.raises(reocr.RestoreRefusedError):
        reocr.restore_sidecar(drive, drive.root, doc, "no-such-id", dry_run=True, now=NOW)
    assert drive.writes == []


# --- 4. restore: the MCP tool --------------------------------------------------------

REASON = "Synthetic reason: re-OCR kept too little text"


@pytest.fixture(autouse=True)
def _reset_jobs():
    tool._reocr_jobs.clear()
    tool._runs.clear()
    tool._restoring.clear()
    yield
    tool._reocr_jobs.clear()
    tool._runs.clear()
    tool._restoring.clear()


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


async def _seed_doc(db, doc_id: int, pid: str, sidecar_id: str, filename: str = DOC_NAME):
    meta = {"curated": {"sidecar_id": sidecar_id, "sidecar_sha": "sha", "source_md5": "md"}}
    await db.db.execute(
        "INSERT OR IGNORE INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
        (TEST_PATIENT_UUID, "synth-other", "Synthetic Other"),
    )
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, mime_type, "
        "size_bytes, patient_id, gdrive_id, structured_metadata) "
        "VALUES (?, ?, ?, ?, 'labs', 'application/pdf', 1, ?, ?, ?)",
        (doc_id, f"gdrive:g{doc_id}", filename, filename, pid, f"g{doc_id}", json.dumps(meta)),
    )  # fmt: skip
    await db.db.commit()


@contextlib.contextmanager
def _wired(drive):
    gdrive = MagicMock()
    own = MagicMock()
    entered = []

    @contextlib.contextmanager
    def contract_writes():
        entered.append(True)
        yield

    own.contract_writes = contract_writes
    gdrive.dedicated_copy.return_value = own
    clients = AsyncMock(return_value=(gdrive, None, None, drive.root))
    index = AsyncMock(return_value={"status": "ok", "updated": 1})
    with (
        patch("oncofiles.server._create_patient_clients", clients),
        patch.object(drive_ops, "DriveOps", lambda _own: drive),
        patch.object(tool, "_index_patient_lock_held", index),
    ):
        yield index, entered


async def _activity(db) -> list[dict]:
    async with db.db.execute(
        "SELECT patient_id, tool_name, input_summary, output_summary, status FROM activity_log "
        "WHERE tool_name = 'curated_restore_sidecar' ORDER BY id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _call(db, **kw):
    args = {"document_id": 801, "dry_run": True, "reason": REASON, **kw}
    return json.loads(await tool.curated_restore_sidecar(_ctx(db), **args))


@pytest.mark.usefixtures("admin_scope")
async def test_tool_dry_run_counts_only_and_no_activity_row(db):
    drive, _, side, backup = _restore_setup()
    await _seed_doc(db, 801, ERIKA_UUID, side)
    with _wired(drive) as (index, entered):
        out = await _call(db, backup_id=backup)
    assert out["result"] == "previewed" and out["regained_numbers"] == 1
    assert drive.writes == [] and entered == [] and index.await_count == 0
    assert await _activity(db) == []


@pytest.mark.usefixtures("admin_scope")
async def test_tool_real_restore_backs_up_reindexes_and_audits(db, monkeypatch):
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "synthetic-static-token")
    monkeypatch.delenv("AUDIT_EMAIL_HASH_SECRET", raising=False)
    drive, _, side, backup = _restore_setup()
    await _seed_doc(db, 801, ERIKA_UUID, side)
    with (
        _wired(drive) as (index, entered),
        patch.object(tool, "_caller_email", return_value="admin@example.com"),
    ):
        out = await _call(db, backup_id=backup, dry_run=False)
    assert out["result"] == "restored" and entered == [True]
    assert [w[0] for w in drive.writes] == ["create", "update"]
    assert drive.items[side]["content"] == BACKUP.encode()
    index.assert_awaited_once()
    assert index.await_args.kwargs["force_ids"] == {801}
    assert not tool.run_active() and not tool.patient_lock(ERIKA_UUID).locked()
    [row] = await _activity(db)
    assert row["patient_id"] == ERIKA_UUID and row["status"] == "ok"
    inp, outp = json.loads(row["input_summary"]), json.loads(row["output_summary"])
    assert inp["document_id"] == 801 and inp["backup_id"] == backup
    assert inp["caller_email_hash"] == caller_email_hash("admin@example.com")
    assert "admin@example.com" not in json.dumps(row)
    assert outp["result"] == "restored" and outp["restore_backup_id"]
    assert outp["regained_numbers"] == 1
    blob = json.dumps(row)
    assert REASON not in blob and "Synthetic reason" not in blob
    for token in ("731", "8862", "17.04.2027"):
        assert token not in blob


@pytest.mark.usefixtures("admin_scope")
async def test_tool_refuses_foreign_and_invalid_backups(db):
    drive, _, side, backup = _restore_setup()
    await _seed_doc(db, 801, ERIKA_UUID, side)
    await _seed_doc(db, 802, TEST_PATIENT_UUID, side, filename="20270413-Other-Labs-Iny.pdf")
    other = drive.add(f"_reocr-backup-20270501-101500-{DOC_NAME}.md", drive.folder[LABS], b"x")
    with _wired(drive) as (index, _):
        assert (await _call(db, document_id=802, backup_id=backup, dry_run=False)) == {
            "error": "not_found"
        }  # another patient's document
        assert (await _call(db, document_id=999, backup_id=backup))["error"] == "not_found"
        out = await _call(db, backup_id=other, dry_run=False)
        assert out["error"] == "refused: backup_not_in_logs"
        out = await _call(db, backup_id="no-such-id", dry_run=False)
        assert out["error"].startswith("refused:")
    assert drive.writes == [] and index.await_count == 0
    assert await _activity(db) == []


@pytest.mark.usefixtures("admin_scope")
async def test_tool_refuses_while_busy(db):
    drive, _, side, backup = _restore_setup()
    await _seed_doc(db, 801, ERIKA_UUID, side)
    with _wired(drive):
        tool._reocr_jobs[ERIKA_UUID] = {"state": "running", "kind": "reocr"}
        assert (await _call(db, backup_id=backup, dry_run=False))["error"].startswith("busy")
        tool._reocr_jobs.clear()
        tool._runs[ERIKA_UUID] = {"state": "running"}
        assert (await _call(db, backup_id=backup, dry_run=False))["error"].startswith("busy")
        tool._runs.clear()
        async with tool.patient_lock(ERIKA_UUID):
            assert (await _call(db, backup_id=backup, dry_run=False))["error"].startswith("busy")
        tool._restoring.add(ERIKA_UUID)
        assert (await _call(db, backup_id=backup))["error"].startswith("busy")
        assert tool.run_active()  # CI deploys wait for a restore
        tool._restoring.clear()
    assert drive.writes == []


@pytest.mark.usefixtures("admin_scope")
@pytest.mark.parametrize("reason", ["", "   ", "x" * (tool.RESTORE_REASON_MAX + 1)])
async def test_tool_requires_a_short_reason(db, reason):
    drive, _, side, backup = _restore_setup()
    await _seed_doc(db, 801, ERIKA_UUID, side)
    with _wired(drive):
        out = await _call(db, backup_id=backup, dry_run=False, reason=reason)
    assert "reason" in out["error"] and drive.writes == []


async def test_tool_is_admin_only(db):
    drive, _, side, backup = _restore_setup()
    await _seed_doc(db, 801, ERIKA_UUID, side)
    with _wired(drive):
        out = await _call(db, backup_id=backup, dry_run=False)
    assert "requires admin scope" in out["error"] and drive.writes == []


@pytest.mark.usefixtures("admin_scope")
async def test_a_failed_real_restore_is_audited_and_not_reindexed(db):
    drive, _, side, backup = _restore_setup()
    await _seed_doc(db, 801, ERIKA_UUID, side)
    drive.fail_on.add("update_text")
    with _wired(drive) as (index, _):
        out = await _call(db, backup_id=backup, dry_run=False)
    assert out["result"] == "failed" and out["error"] == "WriteVerificationError"
    assert index.await_count == 0 and not tool._restoring
    [row] = await _activity(db)
    assert row["status"] == "error"
