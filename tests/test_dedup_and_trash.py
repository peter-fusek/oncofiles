"""Tests for document deduplication detection and trash retention (no auto-purge, #734)."""

from __future__ import annotations

from oncofiles.database import Database
from tests.helpers import ERIKA_UUID, make_doc

# ── Deduplication ─────────────────────────────────────────────────────────


async def test_find_duplicates_none(db: Database):
    """No duplicates when all documents are unique."""
    await db.insert_document(
        make_doc(file_id="f1", original_filename="a.pdf"), patient_id=ERIKA_UUID
    )
    await db.insert_document(
        make_doc(file_id="f2", original_filename="b.pdf"), patient_id=ERIKA_UUID
    )

    groups = await db.find_duplicates(patient_id=ERIKA_UUID)
    assert groups == []


async def test_find_duplicates_by_filename_and_size(db: Database):
    """Documents with same original_filename + size_bytes are grouped."""
    await db.insert_document(
        make_doc(file_id="f1", original_filename="scan.pdf", size_bytes=5000), patient_id=ERIKA_UUID
    )
    await db.insert_document(
        make_doc(file_id="f2", original_filename="scan.pdf", size_bytes=5000), patient_id=ERIKA_UUID
    )
    # Different size — not a duplicate
    await db.insert_document(
        make_doc(file_id="f3", original_filename="scan.pdf", size_bytes=9999), patient_id=ERIKA_UUID
    )

    groups = await db.find_duplicates(patient_id=ERIKA_UUID)
    assert len(groups) == 1
    assert len(groups[0]) == 2
    assert groups[0][0].file_id == "f1"
    assert groups[0][1].file_id == "f2"


async def test_find_duplicates_excludes_deleted(db: Database):
    """Soft-deleted documents are excluded from duplicate detection."""
    doc1 = await db.insert_document(
        make_doc(file_id="f1", original_filename="dup.pdf"), patient_id=ERIKA_UUID
    )
    await db.insert_document(
        make_doc(file_id="f2", original_filename="dup.pdf"), patient_id=ERIKA_UUID
    )
    # Delete one — no longer a duplicate pair
    await db.delete_document(doc1.id)

    groups = await db.find_duplicates(patient_id=ERIKA_UUID)
    assert groups == []


# ── Trash retention: nothing is purged automatically (#734) ──────────────


def test_database_has_no_trash_purge():
    """The 30-day hard-delete purge was removed, not flagged off (#734)."""
    assert not hasattr(Database, "purge_expired_trash")


async def test_old_trash_stays_with_its_pages_and_labs_and_restores(db: Database):
    """A document trashed long ago keeps its row, OCR pages and lab values,
    and restore_document still brings it back."""
    from tests.helpers import make_lab_value

    doc = await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    await db.save_ocr_page(doc.id, 1, "Invented page text", "test-model")
    await db.insert_lab_values([make_lab_value(document_id=doc.id, parameter="WBC", value=4.2)])

    # Trashed well past the old 30-day purge window.
    await db.db.execute(
        "UPDATE documents SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-400 days') "
        "WHERE id = ?",
        (doc.id,),
    )
    await db.db.commit()

    trash = await db.list_trash(patient_id=ERIKA_UUID)
    assert [d.id for d in trash] == [doc.id]
    assert len(await db.get_ocr_pages(doc.id)) == 1
    assert len(await db.get_lab_snapshot(doc.id)) == 1

    assert await db.restore_document(doc.id)
    assert await db.list_trash(patient_id=ERIKA_UUID) == []
    restored = await db.get_document(doc.id, patient_id=ERIKA_UUID)
    assert restored is not None and restored.deleted_at is None
