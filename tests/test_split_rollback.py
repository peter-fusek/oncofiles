"""Split atomicity — when Files API fails mid-loop, the whole split must roll
back so we never persist orphan parts. See #456.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from oncofiles.models import Document, DocumentCategory
from oncofiles.split import split_document

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"


async def _seed_source_doc(db) -> Document:
    doc = Document(
        file_id="src_doc",
        filename="multi.pdf",
        original_filename="multi.pdf",
        document_date=date(2026, 2, 1),
        institution="NOU",
        category=DocumentCategory.LABS,
        description="mixed",
        mime_type="application/pdf",
        size_bytes=1000,
    )
    return await db.insert_document(doc, patient_id=ERIKA_UUID)


@pytest.mark.asyncio
async def test_split_rolls_back_when_files_api_fails_mid_loop(db):
    """Files API upload failure on part 2 must delete the already-created part 1.

    Historic behaviour: part 1 persisted, loop `continue`d past part 2, so only
    part 1 existed in the DB — yet its filename advertised "Part 1 of 2" and
    the orphan confused every downstream tool (#456).
    """
    # Give the source a gdrive_id so split_document's download-once branch fires
    # and we actually exercise the Files API upload path.
    src = Document(
        file_id="src_doc",
        filename="multi.pdf",
        original_filename="multi.pdf",
        document_date=date(2026, 2, 1),
        institution="NOU",
        category=DocumentCategory.LABS,
        description="mixed",
        mime_type="application/pdf",
        size_bytes=1000,
        gdrive_id="gd_src",
    )
    source = await db.insert_document(src, patient_id=ERIKA_UUID)

    gdrive = MagicMock()
    gdrive.download.return_value = b"pdf bytes"

    files = MagicMock()
    # Upload succeeds on first call (part 1) then raises on second (part 2).
    ok = MagicMock()
    ok.id = "uploaded_part_1"
    files.upload.side_effect = [ok, RuntimeError("Files API down")]

    sub_docs = [
        {
            "page_range": [1, 1],
            "document_date": "2026-02-01",
            "institution": "NOU",
            "category": "labs",
            "description": "part1",
            "confidence": 0.9,
        },
        {
            "page_range": [2, 2],
            "document_date": "2026-02-01",
            "institution": "NOU",
            "category": "labs",
            "description": "part2",
            "confidence": 0.9,
        },
    ]

    created = await split_document(
        db,
        files,
        gdrive=gdrive,
        doc=source,
        sub_docs=sub_docs,
        patient_id=ERIKA_UUID,
    )

    assert created == []
    all_docs = await db.list_documents(limit=100, patient_id=ERIKA_UUID)
    live = [d for d in all_docs if d.deleted_at is None]
    # Only the source document should remain live. The rolled-back part 1 and
    # the never-created part 2 must not leave live rows behind.
    assert [d.id for d in live] == [source.id]


@pytest.mark.asyncio
async def test_split_succeeds_when_all_uploads_succeed(db):
    """Baseline: a clean 2-part split writes 2 parts and soft-deletes the source."""
    source = await _seed_source_doc(db)

    files = MagicMock()
    ok1, ok2 = MagicMock(), MagicMock()
    ok1.id = "uploaded_1"
    ok2.id = "uploaded_2"
    files.upload.side_effect = [ok1, ok2]

    sub_docs = [
        {
            "document_date": "2026-02-01",
            "institution": "NOU",
            "category": "labs",
            "description": "a",
        },
        {
            "document_date": "2026-02-01",
            "institution": "NOU",
            "category": "labs",
            "description": "b",
        },
    ]

    created = await split_document(
        db,
        files,
        gdrive=None,
        doc=source,
        sub_docs=sub_docs,
        patient_id=ERIKA_UUID,
    )

    assert len(created) == 2
    assert {c.part_number for c in created} == {1, 2}
    assert all(c.total_parts == 2 for c in created)
    # Source should be soft-deleted after successful split.
    refreshed_source = await db.get_document(source.id)
    assert refreshed_source.deleted_at is not None
