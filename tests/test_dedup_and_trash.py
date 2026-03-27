"""Tests for document deduplication detection and trash auto-purge."""

from __future__ import annotations

from oncofiles.database import Database
from tests.helpers import make_doc

# ── Deduplication ─────────────────────────────────────────────────────────


async def test_find_duplicates_none(db: Database):
    """No duplicates when all documents are unique."""
    await db.insert_document(make_doc(file_id="f1", original_filename="a.pdf"), patient_id="erika")
    await db.insert_document(make_doc(file_id="f2", original_filename="b.pdf"), patient_id="erika")

    groups = await db.find_duplicates(patient_id="erika")
    assert groups == []


async def test_find_duplicates_by_filename_and_size(db: Database):
    """Documents with same original_filename + size_bytes are grouped."""
    await db.insert_document(
        make_doc(file_id="f1", original_filename="scan.pdf", size_bytes=5000), patient_id="erika"
    )
    await db.insert_document(
        make_doc(file_id="f2", original_filename="scan.pdf", size_bytes=5000), patient_id="erika"
    )
    # Different size — not a duplicate
    await db.insert_document(
        make_doc(file_id="f3", original_filename="scan.pdf", size_bytes=9999), patient_id="erika"
    )

    groups = await db.find_duplicates(patient_id="erika")
    assert len(groups) == 1
    assert len(groups[0]) == 2
    assert groups[0][0].file_id == "f1"
    assert groups[0][1].file_id == "f2"


async def test_find_duplicates_excludes_deleted(db: Database):
    """Soft-deleted documents are excluded from duplicate detection."""
    doc1 = await db.insert_document(
        make_doc(file_id="f1", original_filename="dup.pdf"), patient_id="erika"
    )
    await db.insert_document(
        make_doc(file_id="f2", original_filename="dup.pdf"), patient_id="erika"
    )
    # Delete one — no longer a duplicate pair
    await db.delete_document(doc1.id)

    groups = await db.find_duplicates(patient_id="erika")
    assert groups == []


# ── Trash auto-purge ──────────────────────────────────────────────────────


async def test_purge_expired_trash_none(db: Database):
    """No purge when trash is empty."""
    count = await db.purge_expired_trash(days=30, patient_id="erika")
    assert count == 0


async def test_purge_expired_trash_recent(db: Database):
    """Recently deleted documents are NOT purged."""
    doc = await db.insert_document(make_doc(file_id="f1"), patient_id="erika")
    await db.delete_document(doc.id)

    count = await db.purge_expired_trash(days=30, patient_id="erika")
    assert count == 0

    # Still in trash
    trash = await db.list_trash(patient_id="erika")
    assert len(trash) == 1


async def test_purge_expired_trash_old(db: Database):
    """Documents deleted over N days ago are purged permanently."""
    doc = await db.insert_document(make_doc(file_id="f1"), patient_id="erika")

    # Manually set deleted_at to 31 days ago
    await db.db.execute(
        "UPDATE documents SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-31 days') "
        "WHERE id = ?",
        (doc.id,),
    )
    await db.db.commit()

    count = await db.purge_expired_trash(days=30, patient_id="erika")
    assert count == 1

    # Gone from trash
    trash = await db.list_trash(patient_id="erika")
    assert len(trash) == 0

    # Gone from DB entirely
    result = await db.get_document(doc.id)
    assert result is None


async def test_purge_cleans_ocr_pages(db: Database):
    """Purge also removes associated OCR pages."""
    doc = await db.insert_document(make_doc(file_id="f1"), patient_id="erika")
    await db.save_ocr_page(doc.id, 1, "Page 1 text", "test-model")

    # Set deleted 31 days ago
    await db.db.execute(
        "UPDATE documents SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-31 days') "
        "WHERE id = ?",
        (doc.id,),
    )
    await db.db.commit()

    await db.purge_expired_trash(days=30, patient_id="erika")

    # OCR pages should be gone
    pages = await db.get_ocr_pages(doc.id)
    assert pages == []


async def test_purge_cleans_lab_values(db: Database):
    """Purge also removes associated lab values."""
    from tests.helpers import make_lab_value

    doc = await db.insert_document(make_doc(file_id="f1"), patient_id="erika")
    await db.insert_lab_values(
        [
            make_lab_value(document_id=doc.id, parameter="WBC", value=6.8),
        ]
    )

    # Set deleted 31 days ago
    await db.db.execute(
        "UPDATE documents SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-31 days') "
        "WHERE id = ?",
        (doc.id,),
    )
    await db.db.commit()

    await db.purge_expired_trash(days=30, patient_id="erika")

    # Lab values should be gone
    snapshot = await db.get_lab_snapshot(doc.id)
    assert snapshot == []


async def test_purge_preserves_active_documents(db: Database):
    """Active (non-deleted) documents are never purged."""
    await db.insert_document(make_doc(file_id="f1"), patient_id="erika")
    await db.insert_document(make_doc(file_id="f2"), patient_id="erika")

    count = await db.purge_expired_trash(days=0, patient_id="erika")  # even with 0 days
    assert count == 0
    assert await db.count_documents(patient_id="erika") == 2
