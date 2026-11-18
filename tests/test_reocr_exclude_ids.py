"""curated_reocr ``exclude_ids``: leave documents out of the default selection.

The caller's ids are dropped BEFORE ``limit`` (so the limit is filled with
other documents) and counted in ``excluded_before_limit.excluded_by_caller``.
Only with the default selection: combined with ``document_ids`` it is refused.
Another patient's id never matches (no existence oracle).

Synthetic data only: invented ids, names and dates (2027+).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.curated.reocr import ReocrReport
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID
from tests.test_curated_runner import FakeDrive

OTHER_UUID = "00000000-0000-4000-8000-00000000b0b0"
DOCS = list(range(9401, 9409))  # 8 candidates; fewer facts = later in the order
FOREIGN = 9499


def _name(doc_id: int) -> str:
    return f"20270101_Test_Lab_Labs_Panel{doc_id}.pdf"


async def _insert(db, doc_id: int, facts: int, pid: str = ERIKA_UUID) -> None:
    meta = {"curated": {"sidecar_id": f"s{doc_id}"}, "ocr_engine": "claude-vision"}
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, "
        "mime_type, size_bytes, patient_id, gdrive_id, structured_metadata) "
        "VALUES (?, ?, ?, ?, 'labs', 'application/pdf', 1, ?, ?, ?)",
        (doc_id, f"gdrive:g{doc_id}", _name(doc_id), _name(doc_id), pid, f"g{doc_id}",
         json.dumps(meta)),
    )  # fmt: skip
    for _ in range(facts):
        await db.db.execute(
            "INSERT INTO clinical_records (patient_id, record_type, source_document_id, "
            "param, verification, source) VALUES (?, 'lab', ?, 'X', 'unverified', 't')",
            (pid, doc_id),
        )


@pytest.fixture
async def seeded(db):
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, 'other-test', 'Other Test', 'other@example.invalid')",
        (OTHER_UUID,),
    )
    for n, doc_id in enumerate(DOCS):
        await _insert(db, doc_id, 20 - n)  # 9401 has the most facts, picked first
    await _insert(db, FOREIGN, 50, pid=OTHER_UUID)
    await db.db.commit()
    return db


@pytest.fixture
def jobs(monkeypatch):
    monkeypatch.setattr(tool, "_reocr_jobs", {})


async def _start(db, sent: list, **kw) -> dict:
    drive = FakeDrive()

    def fake_run(_drive, _root, docs, _vocab, **_kw):
        sent.extend(d["document_id"] for d in docs)
        return ReocrReport(dry_run=True)

    with (
        patch(
            "oncofiles.server._create_patient_clients",
            AsyncMock(return_value=(MagicMock(), None, None, drive.root)),
        ),
        patch("oncofiles.curated.drive_ops.DriveOps", lambda _own: drive),
        patch("oncofiles.curated.reocr.run_reocr", fake_run),
    ):
        out = await tool.start_reocr(db, ERIKA_UUID, dry_run=True, **kw)
        if ERIKA_UUID in tool._reocr_jobs:
            await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    return out


async def test_excluded_documents_do_not_use_up_the_limit(seeded, jobs):
    sent: list = []
    plan = await _start(seeded, sent, limit=3, exclude_ids=DOCS[:3])
    assert [d["document_id"] for d in plan["documents"]] == DOCS[3:6]
    assert plan["excluded_before_limit"] == {"excluded_by_caller": 3}
    assert plan["candidates"] == 5
    assert sorted(sent) == DOCS[3:6]  # the runner gets only the others


async def test_without_exclude_ids_the_top_documents_are_picked(seeded, jobs):
    plan = await _start(seeded, [], limit=3)
    assert [d["document_id"] for d in plan["documents"]] == DOCS[:3]
    assert "excluded_by_caller" not in plan["excluded_before_limit"]


async def test_foreign_and_unknown_ids_are_ignored_silently(seeded, jobs):
    plan = await _start(seeded, [], limit=3, exclude_ids=[FOREIGN, 123456, DOCS[0]])
    assert [d["document_id"] for d in plan["documents"]] == DOCS[1:4]
    # only this patient's own candidate is counted; the others never match
    assert plan["excluded_before_limit"] == {"excluded_by_caller": 1}
    assert "error" not in plan


async def test_exclude_ids_with_document_ids_is_refused(seeded, jobs):
    sent: list = []
    out = await _start(seeded, sent, document_ids=[DOCS[0]], exclude_ids=[DOCS[1]])
    assert "error" in out and "exclude_ids" in out["error"] and "document_ids" in out["error"]
    assert sent == [] and ERIKA_UUID not in tool._reocr_jobs


async def test_more_than_50_exclude_ids_is_refused(seeded, jobs):
    out = await _start(seeded, [], exclude_ids=list(range(1, 52)))
    assert out == {"error": "at most 50 exclude_ids per call, got 51"}
    assert ERIKA_UUID not in tool._reocr_jobs


async def test_everything_excluded_reports_nothing_to_do(seeded, jobs):
    plan = await _start(seeded, [], limit=3, exclude_ids=DOCS)
    assert plan["status"] == "nothing to do"
    assert plan["excluded_before_limit"] == {"excluded_by_caller": 8}
