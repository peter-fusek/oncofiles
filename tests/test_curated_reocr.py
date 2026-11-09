"""Local re-OCR of uncertain transcriptions (#660). Synthetic data only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from oncofiles.curated import llm, reocr
from oncofiles.curated.facts_rules import Vocabulary
from oncofiles.curated.indexer import parse_sidecar
from tests.test_curated_runner import LABS, LOGS, FakeDrive

NOW = datetime(2027, 5, 3, 21, 0, tzinfo=UTC)
OLD_SIDECAR = (
    "---\n"
    "source_file: 20270412-JanaTestova-LabX-Labs-KrvnyObraz.pdf\n"
    "doc_date: 2027-04-12\n"
    "doc_type: Labs\n"
    "diagnoses:\n"
    "  - C99.9\n"
    "extraction: tesseract eng-only\n"
    "handwritten: false\n"
    "review_needed: false\n"
    "---\n\n"
    "<!-- page 1 -->\nLeukocyty 6,3? [?] G/l 4,0 - 11\n"
)


@pytest.fixture
async def vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


def _pdf(text: str | None) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_needs_reocr_reasons():
    assert reocr.needs_reocr({"extraction": "tesseract eng-only"}, []) == ["cowork_ocr"]
    assert reocr.needs_reocr({"ocr_engine": "qwen3-vl-local"}, ["ok"]) == []
    assert reocr.needs_reocr({"ocr_engine": "qwen3-vl"}, ["WBC 4,1 [?]"]) == ["ocr_uncertain"]
    assert reocr.needs_reocr({}, ["a [?]"]) == ["cowork_ocr", "ocr_uncertain"]


def test_reconcile_flags_only_lines_the_second_pass_does_not_confirm():
    first = ["Leukocyty 6,37 G/l\nHemoglobín 131 g/l\nPoznámka bez čísel"]
    second = ["Leukocyty 6,87 G/l\nHemoglobín 131 g/l\nPoznámka bez čísel"]
    pages, flagged = reocr.reconcile(first, second)
    assert flagged == 1
    lines = pages[0].splitlines()
    assert lines[0].endswith("[?]") and not lines[1].endswith("[?]")
    assert lines[2] == "Poznámka bez čísel"
    again, n = reocr.reconcile(pages, second)  # an existing [?] is not doubled
    assert not n
    assert again[0].count("[?]") == flagged


def test_rewrite_keeps_front_matter_and_replaces_body():
    rr = reocr.Reread(["Leukocyty 7,93 G/l", "Strana B"], "vision2", "qwenvl")
    new = reocr.rewrite_sidecar(OLD_SIDECAR, rr, previous_engine="tesseract", now=NOW)
    front, pages = parse_sidecar(new)
    assert front["doc_date"] == "2027-04-12" and front["doc_type"] == "Labs"
    assert front["extraction"] == "vision2" and front["ocr_engine"] == "qwenvl"
    assert front["reocr_previous_engine"] == "tesseract"
    assert "  - C99.9" in new  # list continuation lines are kept verbatim
    assert pages == ["Leukocyty 7,93 G/l", "Strana B"]


def test_reread_uses_a_real_text_layer_without_the_model(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: pytest.fail("no model for a text layer"))
    text = "Leukocyty 6,37 G/l 4,0 - 11 " * 6
    rr = reocr.reread("lab.pdf", _pdf(text))
    assert rr.method == "text_layer" and rr.engine == "pymupdf"
    assert "6,37" in rr.pages[0]


def test_reread_two_vision_passes_with_high_effort(monkeypatch):
    calls = []
    answers = iter(["Leukocyty 6,37 G/l", "Leukocyty 6,87 G/l"])

    def fake_chat(system, content, **kw):
        calls.append(kw)
        return next(answers)

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "model_id", lambda: "qwen-local")
    rr = reocr.reread("scan.pdf", _pdf(None))
    assert rr.method == "vision2" and rr.engine == "qwen-local"
    assert len(calls) == 2 and all(c["reasoning_effort"] == "high" for c in calls)
    assert rr.flagged_lines == 1 and rr.pages[0].endswith("[?]")


def _setup():
    drive = FakeDrive()
    src = drive.add("20270412-JanaTestova-LabX-Labs-KrvnyObraz.pdf", drive.folder[LABS],
                    _pdf("Leukocyty 6,37 G/l 4,0 - 11 " * 6))  # fmt: skip
    side = drive.add("20270412-JanaTestova-LabX-Labs-KrvnyObraz.pdf.md", drive.folder[LABS],
                     OLD_SIDECAR.encode())  # fmt: skip
    doc = {"document_id": 7, "filename": "20270412-JanaTestova-LabX-Labs-KrvnyObraz.pdf",
           "gdrive_id": src, "sidecar_id": side, "engine": "tesseract eng-only",
           "reasons": ["cowork_ocr"]}  # fmt: skip
    return drive, doc, side


async def test_dry_run_writes_nothing(vocab):
    drive, doc, side = _setup()
    rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=True, now=NOW)
    d = rep.documents[0]
    assert d.result == "previewed" and d.method == "text_layer"
    assert d.uncertain_before == 1 and d.uncertain_after == 0
    assert drive.writes == []
    assert drive.items[side]["content"] == OLD_SIDECAR.encode()


async def test_real_run_backs_up_before_updating_in_place(vocab):
    drive, doc, side = _setup()
    rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=False, now=NOW)
    assert rep.documents[0].result == "rewritten"
    kinds = [w[0] for w in drive.writes]
    assert kinds == ["create", "update"]  # backup first, never a delete or move
    backup = drive.find(drive.folder[LOGS], f"_reocr-backup-20270503-{doc['filename']}.md")
    assert backup and drive.items[backup]["content"] == OLD_SIDECAR.encode()
    front, pages = parse_sidecar(drive.items[side]["content"].decode())
    assert front["extraction"] == "text_layer" and "6,37" in pages[0]


async def test_failed_reread_writes_nothing(vocab, monkeypatch):
    drive, doc, _ = _setup()
    monkeypatch.setattr(
        reocr, "reread", lambda *a, **k: reocr.Reread([], "none", "-", error="LLM down")
    )
    rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=False, now=NOW)
    assert rep.documents[0].result == "failed" and drive.writes == []


async def test_stop_skips_remaining_documents(vocab):
    drive, doc, _ = _setup()
    rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=False, now=NOW,
                          stop=lambda: True)  # fmt: skip
    assert rep.documents[0].result == "skipped" and drive.writes == []


def test_reocr_job_counts_as_an_active_run():
    from oncofiles.tools import curated as tool

    tool._reocr_jobs["p-test"] = {"state": "running"}
    try:
        assert tool.run_active() is True
    finally:
        tool._reocr_jobs.pop("p-test")
    assert tool.run_active() is False


async def test_targets_prefer_documents_with_unverified_facts(db):
    import json as _json

    from oncofiles.tools import curated as tool
    from tests.conftest import ERIKA_UUID

    def meta(engine):
        return _json.dumps({"curated": {"sidecar_id": "side"}, "ocr_engine": engine})

    rows = [  # id, gdrive id, mime, engine, unverified facts
        (9101, "g1", "application/pdf", "tesseract eng-only", 3),
        (9102, "g2", "application/pdf", "tesseract eng-only", 0),
        (9103, "g3", "application/pdf", "qwen-local", 2),
        (9104, "g4", "application/vnd.google-apps.document", "claude-vision", 4),
        (9105, "g5", "application/pdf", "claude-vision", 7),
    ]
    for doc_id, gid, mime, engine, n in rows:
        await db.db.execute(
            "INSERT INTO documents (id, file_id, filename, original_filename, category, "
            "mime_type, size_bytes, patient_id, gdrive_id, structured_metadata) "
            "VALUES (?, ?, ?, ?, 'labs', ?, 1, ?, ?, ?)",
            (doc_id, f"gdrive:{gid}", f"d{doc_id}.pdf", f"d{doc_id}.pdf", mime,
             ERIKA_UUID, gid, meta(engine)),
        )  # fmt: skip
        for _ in range(n):
            await db.db.execute(
                "INSERT INTO clinical_records (patient_id, record_type, source_document_id, "
                "param, verification, source) VALUES (?, 'lab', ?, 'X', 'unverified', 't')",
                (ERIKA_UUID, doc_id),
            )
    await db.db.commit()
    got = await tool._reocr_targets(db, ERIKA_UUID, None)
    assert [t["document_id"] for t in got] == [9105, 9101]  # most unverified first
    everything = await tool._reocr_targets(db, ERIKA_UUID, None, only_with_facts=False)
    assert {t["document_id"] for t in everything} == {9105, 9101, 9102}  # never 9103 / 9104
