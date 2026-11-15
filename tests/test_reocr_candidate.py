"""A re-OCR that ends in ``review`` keeps its new text next to the old one
(maintainer decision 2026-11-15).

* real run + review: the sidecar is untouched, the new transcription is
  written ONCE as ``_reocr-candidate-<ts>-<source filename>.md`` into the logs
  folder (verified create, no retry), its Drive id is ``candidate`` in the
  report; nothing is re-indexed and no guard is loosened;
* a dry run and a ``rewrite`` decision never create a candidate;
* a failed candidate write leaves ``review`` + ``candidate_error`` and the run
  continues;
* ``curated_restore_sidecar`` promotes a candidate of that very document.

Synthetic data only (invented values, dates 2027+)."""

from __future__ import annotations

import contextlib
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.curated import drive_ops, reocr
from oncofiles.curated.facts_rules import Vocabulary
from oncofiles.curated.indexer import parse_sidecar
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID
from tests.test_curated_reocr import NOW
from tests.test_curated_runner import LABS, LOGS
from tests.test_reocr_loss_guard import FRONT, OLD_BODY, TOKENS, _drop, _go, _run
from tests.test_reocr_retry_guard_restore import _call, _seed_doc

NAME = "20270412-JanaTestova-LabX-Labs-Kontrola.pdf"  # the document of ``_run``
CANDIDATE = f"{reocr.CANDIDATE_PREFIX}{NOW:%Y%m%d-%H%M%S}-{NAME}.md"
LOSSY = [_drop("8862", "4413", "286")]  # three numbers lost: review


@pytest.fixture
async def vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


@pytest.fixture(autouse=True)
def _reset_jobs():
    for d in (tool._reocr_jobs, tool._runs, tool._restoring):
        d.clear()
    yield
    for d in (tool._reocr_jobs, tool._runs, tool._restoring):
        d.clear()


def _candidates(drive) -> list[str]:
    return [n for n in drive.names(drive.folder[LOGS]) if n.startswith(reocr.CANDIDATE_PREFIX)]


# --- run_reocr ---------------------------------------------------------------------


async def test_review_in_a_real_run_keeps_one_candidate_next_to_the_sidecar(vocab):
    rep, d, drive, side, old = _go(vocab, LOSSY)
    assert d.result == "review" and d.decision == "review"
    assert d.review_reasons == ["missing_numbers"]
    assert d.backup is None and d.candidate_error is None
    assert drive.writes == [("create", CANDIDATE)]  # exactly one write, no update
    assert drive.items[side]["content"] == old.encode()  # the old text stays primary
    item = drive.items[d.candidate]
    assert item["parent"] == drive.folder[LOGS] and item["name"] == CANDIDATE
    assert not any(i["trashed"] for i in drive.items.values())

    text = item["content"].decode()
    head, _ = parse_sidecar(text)  # the header block says what the file is
    assert head["kind"] == "reocr_candidate" and head["source_file"] == NAME
    assert str(head["document_id"]) == "7"
    assert head["engine"] == "qwen-local" and head["method"] == "vision2"
    assert str(head["passes"]) == "2" and "missing_numbers" in str(head["review_reasons"])
    assert str(head["created"]).startswith("2027-")
    # then the complete sidecar a rewrite would have written, verbatim
    inner = reocr.candidate_sidecar_text(text)
    front, new_pages = parse_sidecar(inner)
    assert front["extraction"] == "vision2" and front["ocr_engine"] == "qwen-local"
    assert new_pages == LOSSY and "731,4" in inner and "8862" not in inner

    # the report carries the id only, never a value
    blob = json.dumps(rep.to_dict())
    assert rep.to_dict()["documents"][0]["candidate"] == d.candidate
    assert not [t for t in TOKENS if t in blob]


async def test_a_dry_run_writes_no_candidate(vocab):
    _, d, drive, side, old = _go(vocab, LOSSY, dry_run=True)
    assert d.result == "previewed" and d.decision == "review"
    assert d.candidate is None and d.candidate_error is None
    assert drive.writes == [] and _candidates(drive) == []
    assert drive.items[side]["content"] == old.encode()


async def test_a_rewrite_creates_no_candidate(vocab):
    _, d, drive, _, _ = _go(vocab, [_drop()])
    assert d.result == "rewritten" and d.decision == "rewrite"
    assert d.backup and d.candidate is None and d.candidate_error is None
    assert [w[0] for w in drive.writes] == ["create", "update"]
    assert _candidates(drive) == []


async def test_a_failed_candidate_write_stays_review_and_is_not_retried(vocab, caplog):
    caplog.set_level("DEBUG")
    drive, side, old, doc, reader = _run(LOSSY)
    second = "20270413-JanaTestova-LabX-Labs-Druha.pdf"
    src_b = drive.add(second, drive.folder[LABS], b"%PDF synthetic")
    side_b = drive.add(second + ".md", drive.folder[LABS], (FRONT + OLD_BODY).encode())
    doc_b = {**doc, "document_id": 8, "filename": second, "gdrive_id": src_b, "sidecar_id": side_b}
    attempts: list[str] = []
    create = drive.create_text

    def counted(folder_id, name, text, mime="text/markdown"):
        attempts.append(name)
        return create(folder_id, name, text, mime)

    drive.create_text = counted
    drive.fail_on.add("create_text")
    rep = reocr.run_reocr(drive, drive.root, [doc, doc_b], vocab, dry_run=False, now=NOW,
                          reader=reader)  # fmt: skip
    first, other = rep.documents
    for d in (first, other):  # the run went on to the second document
        assert d.result == "review" and d.decision == "review" and d.error is None
        assert d.candidate is None and d.candidate_error == "WriteVerificationError"
    assert len(attempts) == 2 and attempts[0] == CANDIDATE  # one attempt per document
    assert drive.writes == [] and _candidates(drive) == []
    assert drive.items[side]["content"] == old.encode()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "candidate of doc 7 not written: WriteVerificationError" in logged
    assert not [t for t in TOKENS if t in logged]


def test_a_candidate_does_not_mark_the_document_as_handled():
    """Only a backup (a rewrite) or a kept re-OCR text hides a document from
    the automatic selection; a candidate is an open review."""
    assert not reocr.handled_before([CANDIDATE], NAME)


# --- the job: no re-index after a review ---------------------------------------------


@contextlib.contextmanager
def _wired(drive, reader=None):
    gdrive, own, entered = MagicMock(), MagicMock(), []

    @contextlib.contextmanager
    def contract_writes():
        entered.append(True)
        yield

    own.contract_writes = contract_writes
    gdrive.dedicated_copy.return_value = own
    index = AsyncMock(return_value={"status": "ok", "updated": 1})
    with (
        patch("oncofiles.server._create_patient_clients",
              AsyncMock(return_value=(gdrive, None, None, drive.root))),
        patch.object(drive_ops, "DriveOps", lambda _own: drive),
        patch.object(reocr, "reread_isolated", reader),
        patch.object(tool, "index_patient", index),
        patch.object(tool, "_index_patient_lock_held", index),
        patch("oncofiles.pre_store.make_copy", return_value="synthetic-copy.db"),
        # the suite's own RSS must not make the job skip the document
        patch("oncofiles.config.REOCR_RSS_LIMIT_MB", 0),
        patch.object(tool, "_stopping", threading.Event()),
    ):  # fmt: skip
        yield index, entered


@pytest.mark.parametrize("dry_run", [False, True])
async def test_job_review_writes_inside_contract_writes_and_never_reindexes(db, dry_run):
    drive, side, old, doc, reader = _run(LOSSY)
    tool._reocr_jobs[ERIKA_UUID] = job = {"kind": "reocr", "state": "running",
                                          "dry_run": dry_run, "documents": [doc]}  # fmt: skip
    with _wired(drive, reader) as (index, entered):
        await tool._reocr_job(db, ERIKA_UUID, [doc], dry_run=dry_run, passes=2)
    assert job["state"] == "done" and index.await_count == 0 and "index" not in job
    [d] = job["report"]["documents"]
    assert drive.items[side]["content"] == old.encode()
    if dry_run:
        assert d["result"] == "previewed" and d["candidate"] is None
        assert drive.writes == [] and entered == []
    else:
        assert d["result"] == "review" and d["decision"] == "review"
        assert entered == [True] and len(drive.writes) == 1
        assert drive.items[d["candidate"]]["parent"] == drive.folder[LOGS]


# --- promotion: restore_sidecar accepts a candidate of that document ------------------


async def _reviewed(vocab):
    _, d, drive, side, old = _go(vocab, LOSSY)
    doc = {"document_id": 7, "filename": NAME, "sidecar_id": side}
    drive.writes.clear()
    return drive, doc, side, old, d.candidate


async def test_promote_dry_run_reports_counts_only(vocab):
    drive, doc, side, old, cand = await _reviewed(vocab)
    out = reocr.restore_sidecar(drive, drive.root, doc, cand, dry_run=True, now=NOW)
    assert out["result"] == "previewed" and out["backup_kind"] == "reocr_candidate"
    assert out["missing_numbers"] == 3 and out["missing_dates"] == 0
    assert drive.writes == [] and drive.items[side]["content"] == old.encode()
    assert not [t for t in TOKENS if t in json.dumps(out)]


async def test_promote_backs_up_the_current_sidecar_then_writes_the_candidate_text(vocab):
    drive, doc, side, old, cand = await _reviewed(vocab)
    out = reocr.restore_sidecar(drive, drive.root, doc, cand, dry_run=False, now=NOW)
    assert out["result"] == "restored" and out["backup_kind"] == "reocr_candidate"
    name = f"_restore-backup-{NOW:%Y%m%d-%H%M%S}-{NAME}.md"
    assert drive.writes == [("create", name), ("update", NAME + ".md")]
    assert drive.items[out["restore_backup_id"]]["content"] == old.encode()
    new = drive.items[side]["content"].decode()
    assert new == reocr.candidate_sidecar_text(drive.items[cand]["content"].decode())
    assert "reocr_candidate" not in new
    front, pages = parse_sidecar(new)  # a normal sidecar again: one front matter
    assert front["ocr_engine"] == "qwen-local" and pages == LOSSY
    assert not any(i["trashed"] for i in drive.items.values())  # the candidate stays


async def test_a_backup_still_restores_and_says_its_kind(vocab):
    drive, doc, _, _, _ = await _reviewed(vocab)
    backup = drive.add(f"_reocr-backup-20270501-101500-{NAME}.md", drive.folder[LOGS],
                       (FRONT + "Riadok alfa 731,4\n").encode())  # fmt: skip
    out = reocr.restore_sidecar(drive, drive.root, doc, backup, dry_run=True, now=NOW)
    assert out["result"] == "previewed" and out["backup_kind"] == "reocr_backup"


_OK = f"{reocr.CANDIDATE_PREFIX}20270501-101500-{NAME}.md"


@pytest.mark.parametrize(
    "where,name,body,code",
    [
        # another document's candidate
        (LOGS, f"{reocr.CANDIDATE_PREFIX}20270501-101500-20270413-Other-Labs-Iny.pdf.md", None,
         "backup_not_of_this_document"),
        # this document's name only as the tail of another one
        (LOGS, f"{reocr.CANDIDATE_PREFIX}20270501-101500-x{NAME}.md", None,
         "backup_not_of_this_document"),
        (LOGS, "x" + _OK, None, "backup_not_of_this_document"),
        (LOGS, f"{reocr.CANDIDATE_PREFIX}20270501-{NAME}.md", None, "backup_not_of_this_document"),
        (LOGS, _OK + ".bak", None, "backup_not_of_this_document"),
        # rightly named but outside the logs folder
        (LABS, _OK, None, "backup_not_in_logs"),
        # rightly named, but not a candidate file (no header block / nothing after it)
        (LOGS, _OK, "Riadok alfa 731,4\n", "candidate_malformed"),
        (LOGS, _OK, "---\nkind: something_else\n---\nRiadok alfa 731,4\n", "candidate_malformed"),
        (LOGS, _OK, "---\nkind: reocr_candidate\n---\n\n", "candidate_malformed"),
    ],
)  # fmt: skip
async def test_promote_refuses_any_other_file(vocab, where, name, body, code):
    drive, doc, side, old, cand = await _reviewed(vocab)
    text = drive.items[cand]["content"].decode() if body is None else body
    other = drive.add(name, drive.folder[where], text.encode())
    with pytest.raises(reocr.RestoreRefusedError, match=code):
        reocr.restore_sidecar(drive, drive.root, doc, other, dry_run=False, now=NOW)
    assert drive.writes == [] and drive.items[side]["content"] == old.encode()


# --- promotion: the MCP tool -----------------------------------------------------------


@pytest.mark.usefixtures("admin_scope")
async def test_tool_promotes_a_candidate_of_the_right_document(db, vocab):
    drive, _, side, old, cand = await _reviewed(vocab)
    await _seed_doc(db, 801, ERIKA_UUID, side, filename=NAME)
    with _wired(drive) as (index, entered):
        preview = await _call(db, backup_id=cand)
        assert preview["result"] == "previewed" and preview["backup_kind"] == "reocr_candidate"
        assert drive.writes == [] and entered == [] and index.await_count == 0
        out = await _call(db, backup_id=cand, dry_run=False)
    assert out["result"] == "restored" and entered == [True]
    assert [w[0] for w in drive.writes] == ["create", "update"]
    assert drive.writes[0][1].startswith("_restore-backup-")
    assert drive.items[out["restore_backup_id"]]["content"] == old.encode()
    assert parse_sidecar(drive.items[side]["content"].decode())[1] == LOSSY
    index.assert_awaited_once()
    assert index.await_args.kwargs["force_ids"] == {801}
    assert not [t for t in TOKENS if t in json.dumps(out)]


@pytest.mark.usefixtures("admin_scope")
async def test_tool_refuses_a_candidate_of_another_document(db, vocab):
    drive, _, side, _, cand = await _reviewed(vocab)
    other_name = "20270413-JanaTestova-LabX-Labs-Druha.pdf"
    other_side = drive.add(other_name + ".md", drive.folder[LABS], (FRONT + OLD_BODY).encode())
    await _seed_doc(db, 801, ERIKA_UUID, side, filename=NAME)
    await _seed_doc(db, 802, ERIKA_UUID, other_side, filename=other_name)
    with _wired(drive) as (index, _):
        out = await _call(db, document_id=802, backup_id=cand, dry_run=False)
    assert out["error"] == "refused: backup_not_of_this_document"
    assert drive.writes == [] and index.await_count == 0
    assert drive.items[other_side]["content"] == (FRONT + OLD_BODY).encode()
