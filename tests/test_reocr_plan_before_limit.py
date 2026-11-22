"""Re-OCR plan fills ``limit`` with documents the runner will read (#894).

Before #894 the default selection applied ``limit`` first and the runner then
skipped documents handled before (a candidate, backup, kept text or restore
copy in logs), so a batch of 10 could read 2. The plan now drops them, raw
genotype exports and non-vision sources before the limit, with one logs
listing per job, and reports the counts in ``excluded_before_limit``.

Synthetic data only: invented ids, names and dates (2027+).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.curated import reocr
from oncofiles.curated.reocr import ReocrReport
from oncofiles.tools import curated as tool
from tests.helpers import PRIMARY_TEST_UUID
from tests.test_curated_runner import LOGS, FakeDrive

PROCESSABLE = [9301, 9302, 9303]
HANDLED = list(range(9304, 9312))  # 8 documents with something in logs
GENOTYPE = 9312
MARKERS = (  # every name the runner treats as "handled before"
    "_reocr-candidate-20270301-101010-",
    "_reocr-backup-20270301-101010-",
    "_reocr-backup-20270302-",
    "_reocr-text-20270303-",
    "_restore-backup-20270304-111111-",
    "_reocr-candidate-20270305-121212-",
    "_reocr-backup-20270306-131313-",
    "_restore-backup-20270307-141414-",
)


def _name(doc_id: int) -> str:
    if doc_id == GENOTYPE:
        return f"20270101_Test_Lab_Genetics_Raw{doc_id}.vcf"
    return f"20270101_Test_Lab_Labs_Panel{doc_id}.pdf"


async def _insert(db, doc_id: int, facts: int) -> None:
    meta = {"curated": {"sidecar_id": f"s{doc_id}"}, "ocr_engine": "claude-vision"}
    mime = "text/plain" if doc_id == GENOTYPE else "application/pdf"
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, "
        "mime_type, size_bytes, patient_id, gdrive_id, structured_metadata) "
        "VALUES (?, ?, ?, ?, 'labs', ?, 1, ?, ?, ?)",
        (doc_id, f"gdrive:g{doc_id}", _name(doc_id), _name(doc_id), mime, PRIMARY_TEST_UUID,
         f"g{doc_id}", json.dumps(meta)),
    )  # fmt: skip
    for _ in range(facts):
        await db.db.execute(
            "INSERT INTO clinical_records (patient_id, record_type, source_document_id, "
            "param, verification, source) VALUES (?, 'lab', ?, 'X', 'unverified', 't')",
            (PRIMARY_TEST_UUID, doc_id),
        )


@pytest.fixture
async def seeded(db):
    # handled and genotype documents have the MOST unverified facts, so the old
    # code put them first and filled the limit with them
    for doc_id in PROCESSABLE:
        await _insert(db, doc_id, 1)
    for doc_id in [*HANDLED, GENOTYPE]:
        await _insert(db, doc_id, 5)
    await db.db.commit()
    return db


class CountingDrive:
    """FakeDrive behind a counter of every read the plan makes."""

    def __init__(self, drive: FakeDrive):
        self.drive, self.calls = drive, []

    def list_children(self, folder_id):
        self.calls.append(("list_children", folder_id))
        return self.drive.list_children(folder_id)

    def list_my_drive_root(self):
        self.calls.append(("list_my_drive_root", None))
        return self.drive.list_my_drive_root()

    def download(self, fid):
        self.calls.append(("download", fid))
        return self.drive.download(fid)


def _drive(handled=HANDLED) -> FakeDrive:
    drive = FakeDrive()
    for marker, doc_id in zip(MARKERS, handled, strict=True):
        drive.add(f"{marker}{_name(doc_id)}.md", drive.folder[LOGS], b"x")
    drive.add("_reocr-backup-20270308-x" + _name(PROCESSABLE[0]) + ".md", drive.folder[LOGS], b"x")
    return drive


@pytest.fixture
def jobs(monkeypatch):
    monkeypatch.setattr(tool, "_reocr_jobs", {})


async def _start(db, drive: CountingDrive | None, sent: list, **kw) -> dict:
    def fake_run(_drive, _root, docs, _vocab, **_kw):
        sent.extend((d["document_id"], d.get("explicit")) for d in docs)
        return ReocrReport(dry_run=True)

    clients = (MagicMock(), None, None, drive.drive.root) if drive else None
    with (
        patch("oncofiles.server._create_patient_clients", AsyncMock(return_value=clients)),
        patch("oncofiles.curated.drive_ops.DriveOps", lambda _own: drive),
        patch("oncofiles.curated.reocr.run_reocr", fake_run),
    ):
        out = await tool.start_reocr(db, PRIMARY_TEST_UUID, dry_run=True, **kw)
        if PRIMARY_TEST_UUID in tool._reocr_jobs:
            await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    return out


async def test_default_plan_fills_the_limit_with_processable_documents(seeded, jobs):
    drive, sent = CountingDrive(_drive()), []
    plan = await _start(seeded, drive, sent, limit=10)
    assert sorted(d["document_id"] for d in plan["documents"]) == PROCESSABLE  # not 10
    assert plan["excluded_before_limit"] == {"handled_before": 8, "raw_genotype_excluded": 1}
    assert "logs_listing" not in plan
    assert [(s["document_id"], s["reason"]) for s in plan["skipped"]] == [
        (GENOTYPE, "raw_genotype_excluded")
    ]
    assert sorted(i for i, _ in sent) == PROCESSABLE  # the runner gets only these
    assert plan["candidates"] == 3


async def test_plan_reads_the_logs_folder_once_not_per_document(seeded, jobs):
    """One walk + README + one logs listing for 12 documents: exactly what a
    single ``logs_listing`` costs; no document or sidecar is ever downloaded."""
    drive, sent = CountingDrive(_drive()), []
    reocr.logs_listing(drive, drive.drive.root)
    one_listing = list(drive.calls)
    drive.calls.clear()
    with patch.object(tool, "_launch", AsyncMock(return_value={})):  # plan only
        await _start(seeded, drive, sent, limit=10)
    assert drive.calls == one_listing
    downloaded = {fid for op, fid in drive.calls if op == "download"}
    readme = drive.drive.find(drive.drive.root, "README.md")
    assert downloaded == {readme}


async def test_explicit_ids_keep_reporting_skips(seeded, jobs):
    drive, sent = CountingDrive(_drive()), []
    plan = await _start(seeded, drive, sent, document_ids=[HANDLED[0], GENOTYPE, PROCESSABLE[0]])
    assert drive.calls == []  # explicit ids: no logs listing at plan time
    assert sorted(d["document_id"] for d in plan["documents"]) == [PROCESSABLE[0], HANDLED[0]]
    assert plan["excluded_before_limit"] == {"raw_genotype_excluded": 1}
    assert "logs_listing" not in plan
    report = tool._reocr_jobs[PRIMARY_TEST_UUID]["report"]["documents"]
    assert [(d["document_id"], d["reason"]) for d in report] == [
        (GENOTYPE, "raw_genotype_excluded")
    ]
    assert sorted(sent) == [(PROCESSABLE[0], True), (HANDLED[0], True)]  # read again


async def test_without_drive_the_plan_falls_back_to_the_runner_skip(seeded, jobs):
    sent: list = []
    with patch.object(tool, "_launch", AsyncMock(side_effect=lambda *a, **k: a[2])):
        plan = await _start(seeded, None, sent, limit=10)
    assert plan["logs_listing"] == "unavailable"
    assert len(plan["documents"]) == 10  # unknown: the runner still skips them
    assert plan["excluded_before_limit"] == {"raw_genotype_excluded": 1}


async def test_a_failed_listing_never_fails_the_plan(seeded, jobs):
    class Broken(CountingDrive):
        def list_children(self, folder_id):
            raise RuntimeError("drive down")

    with patch.object(tool, "_launch", AsyncMock(side_effect=lambda *a, **k: a[2])):
        plan = await _start(seeded, Broken(_drive()), [], limit=10)
    assert plan["logs_listing"] == "unavailable" and len(plan["documents"]) == 10


async def test_nothing_left_reports_the_counts(seeded, jobs):
    for doc_id in PROCESSABLE:
        await seeded.db.execute("UPDATE clinical_records SET verification = 'verified' "
                                "WHERE source_document_id = ?",
                                (doc_id,))  # fmt: skip
    await seeded.db.commit()
    plan = await _start(seeded, CountingDrive(_drive()), [], limit=10)
    assert plan["status"] == "nothing to do"
    assert plan["excluded_before_limit"] == {"handled_before": 8, "raw_genotype_excluded": 1}
    assert PRIMARY_TEST_UUID not in tool._reocr_jobs


def test_one_predicate_for_plan_and_runner():
    name = _name(9301)
    for marker in MARKERS:
        logs = [f"{marker}{name}.md"]
        assert reocr.auto_selection_skip({"filename": name}, logs) == reocr.HANDLED_BEFORE
        assert reocr.auto_selection_skip({"filename": name, "explicit": True}, logs) is None
    assert reocr.auto_selection_skip({"filename": name}, [f"_restore-backup-x-{name}.md"]) is None
    assert reocr.auto_selection_skip({"filename": "x" + name}, [f"{MARKERS[0]}{name}.md"]) is None
