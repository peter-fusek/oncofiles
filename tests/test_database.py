"""Tests for the database module."""

import sqlite3
from datetime import date

import pytest

from oncofiles.database import Database
from oncofiles.models import DocumentCategory, Patient, SearchQuery
from tests.helpers import ERIKA_UUID, make_doc


async def test_idempotent_migrate():
    """migrate() can be called twice without crashing (already-migrated DB)."""
    database = Database(":memory:")
    await database.connect()
    await database.migrate()
    # Second call should not raise
    await database.migrate()

    # Verify schema_migrations table has entries
    async with database.db.execute("SELECT COUNT(*) FROM schema_migrations") as cursor:
        row = await cursor.fetchone()
        count = row["COUNT(*)"] if isinstance(row, dict) else row[0]
        assert count >= 16  # at least 16 migration files

    await database.close()


async def test_insert_and_get(db: Database):
    doc = make_doc()
    result = await db.insert_document(doc, patient_id=ERIKA_UUID)
    assert result.id is not None

    fetched = await db.get_document(result.id)
    assert fetched is not None
    assert fetched.file_id == "file_test123"
    assert fetched.institution == "NOUonko"
    assert fetched.category == DocumentCategory.LABS


async def test_get_by_file_id(db: Database):
    doc = make_doc()
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    fetched = await db.get_document_by_file_id("file_test123", patient_id=ERIKA_UUID)
    assert fetched is not None
    assert fetched.filename == "20240115_NOUonko_labs_krvnyObraz.pdf"


async def test_get_nonexistent(db: Database):
    assert await db.get_document(999) is None
    assert await db.get_document_by_file_id("file_nope", patient_id=ERIKA_UUID) is None


async def test_list_documents(db: Database):
    await db.insert_document(
        make_doc(file_id="file_1", document_date=date(2024, 1, 1)), patient_id=ERIKA_UUID
    )
    await db.insert_document(
        make_doc(file_id="file_2", document_date=date(2024, 3, 1)), patient_id=ERIKA_UUID
    )
    await db.insert_document(
        make_doc(file_id="file_3", document_date=date(2024, 2, 1)), patient_id=ERIKA_UUID
    )

    docs = await db.list_documents(patient_id=ERIKA_UUID)
    assert len(docs) == 3
    # Should be ordered by date descending
    assert docs[0].file_id == "file_2"
    assert docs[1].file_id == "file_3"
    assert docs[2].file_id == "file_1"


async def test_list_with_limit(db: Database):
    for i in range(10):
        await db.insert_document(make_doc(file_id=f"file_{i}"), patient_id=ERIKA_UUID)

    docs = await db.list_documents(limit=3, patient_id=ERIKA_UUID)
    assert len(docs) == 3


async def test_delete_document(db: Database):
    """Soft delete: document hidden from list but still retrievable by ID."""
    doc = make_doc()
    result = await db.insert_document(doc, patient_id=ERIKA_UUID)

    deleted = await db.delete_document(result.id)
    assert deleted is True

    # Still retrievable by ID (needed for restore)
    fetched = await db.get_document(result.id)
    assert fetched is not None
    assert fetched.deleted_at is not None

    # Hidden from list
    docs = await db.list_documents(patient_id=ERIKA_UUID)
    assert len(docs) == 0


async def test_delete_by_file_id(db: Database):
    """Soft delete by file_id: hidden from search."""
    await db.insert_document(make_doc(), patient_id=ERIKA_UUID)
    deleted = await db.delete_document_by_file_id("file_test123", patient_id=ERIKA_UUID)
    assert deleted is True

    # Hidden from search
    results = await db.search_documents(SearchQuery(text="krvny"), patient_id=ERIKA_UUID)
    assert len(results) == 0


async def test_delete_nonexistent(db: Database):
    assert await db.delete_document(999) is False
    assert await db.delete_document_by_file_id("file_nope", patient_id=ERIKA_UUID) is False


async def test_delete_already_deleted(db: Database):
    """Deleting an already soft-deleted doc returns False."""
    doc = await db.insert_document(make_doc(), patient_id=ERIKA_UUID)
    assert await db.delete_document(doc.id) is True
    assert await db.delete_document(doc.id) is False


async def test_restore_document(db: Database):
    """Restore a soft-deleted document back to active."""
    doc = await db.insert_document(make_doc(), patient_id=ERIKA_UUID)
    await db.delete_document(doc.id)

    restored = await db.restore_document(doc.id)
    assert restored is True

    # Back in listings
    docs = await db.list_documents(patient_id=ERIKA_UUID)
    assert len(docs) == 1
    assert docs[0].deleted_at is None


async def test_restore_nonexistent(db: Database):
    assert await db.restore_document(999) is False


async def test_restore_active_document(db: Database):
    """Restoring a non-deleted doc returns False."""
    doc = await db.insert_document(make_doc(), patient_id=ERIKA_UUID)
    assert await db.restore_document(doc.id) is False


async def test_list_trash(db: Database):
    doc1 = await db.insert_document(make_doc(file_id="file_1"), patient_id=ERIKA_UUID)
    doc2 = await db.insert_document(make_doc(file_id="file_2"), patient_id=ERIKA_UUID)
    await db.insert_document(make_doc(file_id="file_3"), patient_id=ERIKA_UUID)  # active

    await db.delete_document(doc1.id)
    await db.delete_document(doc2.id)

    trash = await db.list_trash(patient_id=ERIKA_UUID)
    assert len(trash) == 2
    trash_ids = {d.id for d in trash}
    assert doc1.id in trash_ids
    assert doc2.id in trash_ids

    # Active docs not in trash
    active = await db.list_documents(patient_id=ERIKA_UUID)
    assert len(active) == 1


async def test_soft_delete_filtered_from_search(db: Database):
    """Soft-deleted docs excluded from all search/filter queries."""
    doc = await db.insert_document(
        make_doc(
            file_id="file_del", category=DocumentCategory.LABS, document_date=date(2024, 6, 1)
        ),
        patient_id=ERIKA_UUID,
    )
    await db.delete_document(doc.id)

    assert len(await db.search_documents(SearchQuery(text="krvny"), patient_id=ERIKA_UUID)) == 0
    assert len(await db.get_latest_labs(patient_id=ERIKA_UUID)) == 0
    assert len(await db.get_treatment_timeline(patient_id=ERIKA_UUID)) == 0
    assert len(await db.get_documents_without_ai(patient_id=ERIKA_UUID)) == 0
    assert len(await db.get_pending_sync_documents(patient_id=ERIKA_UUID)) == 0


async def test_count_documents(db: Database):
    await db.insert_document(make_doc(file_id="file_1"), patient_id=ERIKA_UUID)
    await db.insert_document(make_doc(file_id="file_2"), patient_id=ERIKA_UUID)
    doc3 = await db.insert_document(make_doc(file_id="file_3"), patient_id=ERIKA_UUID)

    assert await db.count_documents(patient_id=ERIKA_UUID) == 3
    await db.delete_document(doc3.id)
    assert await db.count_documents(patient_id=ERIKA_UUID) == 2


async def test_search_by_text(db: Database):
    await db.insert_document(
        make_doc(
            file_id="file_1",
            filename="20240115_NOUonko_labs_krvnyObraz.pdf",
            description="krvny obraz",
            institution="NOUonko",
        ),
        patient_id=ERIKA_UUID,
    )
    await db.insert_document(
        make_doc(
            file_id="file_2",
            filename="20240301_UNB_imaging_CTabdomen.pdf",
            original_filename="20240301_UNB_imaging_CTabdomen.pdf",
            description="CT abdomen",
            institution="UNB",
        ),
        patient_id=ERIKA_UUID,
    )

    results = await db.search_documents(SearchQuery(text="krvny"), patient_id=ERIKA_UUID)
    assert len(results) == 1
    assert results[0].file_id == "file_1"


async def test_search_by_text_substring_in_filename(db: Database):
    """Text search matches substrings inside CamelCase filenames (#40)."""
    await db.insert_document(
        make_doc(
            file_id="file_genetika",
            filename="20240301_NOU_pathology_GenetikaMudrMalejcikova.pdf",
            original_filename="20240301 ErikaFusekova-NOU-GenetikaMudrMalejcikova.pdf",
            description="GenetikaMudrMalejcikova",
        ),
        patient_id=ERIKA_UUID,
    )
    await db.insert_document(
        make_doc(file_id="file_other", description="CT abdomen"), patient_id=ERIKA_UUID
    )

    # Substring in filename
    results = await db.search_documents(SearchQuery(text="genetik"), patient_id=ERIKA_UUID)
    assert len(results) == 1
    assert results[0].file_id == "file_genetika"

    # Case-insensitive
    results = await db.search_documents(SearchQuery(text="GENETIK"), patient_id=ERIKA_UUID)
    assert len(results) == 1
    assert results[0].file_id == "file_genetika"

    # Substring in original_filename
    results = await db.search_documents(SearchQuery(text="Malejcik"), patient_id=ERIKA_UUID)
    assert len(results) == 1
    assert results[0].file_id == "file_genetika"


async def test_search_by_text_no_match(db: Database):
    """Text search returns empty when nothing matches."""
    await db.insert_document(
        make_doc(file_id="file_1", description="krvny obraz"), patient_id=ERIKA_UUID
    )

    results = await db.search_documents(SearchQuery(text="nonexistent"), patient_id=ERIKA_UUID)
    assert len(results) == 0


async def test_search_by_institution(db: Database):
    await db.insert_document(
        make_doc(file_id="file_1", institution="NOUonko"), patient_id=ERIKA_UUID
    )
    await db.insert_document(make_doc(file_id="file_2", institution="OUSA"), patient_id=ERIKA_UUID)

    results = await db.search_documents(SearchQuery(institution="OUSA"), patient_id=ERIKA_UUID)
    assert len(results) == 1
    assert results[0].file_id == "file_2"


async def test_search_by_category(db: Database):
    await db.insert_document(
        make_doc(file_id="file_1", category=DocumentCategory.LABS), patient_id=ERIKA_UUID
    )
    await db.insert_document(
        make_doc(file_id="file_2", category=DocumentCategory.IMAGING), patient_id=ERIKA_UUID
    )

    results = await db.search_documents(
        SearchQuery(category=DocumentCategory.IMAGING), patient_id=ERIKA_UUID
    )
    assert len(results) == 1
    assert results[0].file_id == "file_2"


async def test_search_by_date_range(db: Database):
    await db.insert_document(
        make_doc(file_id="file_1", document_date=date(2024, 1, 1)), patient_id=ERIKA_UUID
    )
    await db.insert_document(
        make_doc(file_id="file_2", document_date=date(2024, 6, 1)), patient_id=ERIKA_UUID
    )
    await db.insert_document(
        make_doc(file_id="file_3", document_date=date(2024, 12, 1)), patient_id=ERIKA_UUID
    )

    results = await db.search_documents(
        SearchQuery(date_from=date(2024, 3, 1), date_to=date(2024, 9, 1)),
        patient_id=ERIKA_UUID,
    )
    assert len(results) == 1
    assert results[0].file_id == "file_2"


async def test_get_latest_labs(db: Database):
    await db.insert_document(
        make_doc(
            file_id="file_lab1",
            category=DocumentCategory.LABS,
            document_date=date(2024, 1, 1),
        ),
        patient_id=ERIKA_UUID,
    )
    await db.insert_document(
        make_doc(
            file_id="file_lab2",
            category=DocumentCategory.LABS,
            document_date=date(2024, 6, 1),
        ),
        patient_id=ERIKA_UUID,
    )
    await db.insert_document(
        make_doc(
            file_id="file_img",
            category=DocumentCategory.IMAGING,
            document_date=date(2024, 12, 1),
        ),
        patient_id=ERIKA_UUID,
    )

    labs = await db.get_latest_labs(limit=5, patient_id=ERIKA_UUID)
    assert len(labs) == 2
    assert labs[0].file_id == "file_lab2"  # newest first
    assert labs[1].file_id == "file_lab1"


async def test_get_treatment_timeline(db: Database):
    await db.insert_document(
        make_doc(
            file_id="file_surgery",
            category=DocumentCategory.SURGERY,
            document_date=date(2024, 1, 10),
        ),
        patient_id=ERIKA_UUID,
    )
    await db.insert_document(
        make_doc(
            file_id="file_labs",
            category=DocumentCategory.LABS,
            document_date=date(2024, 3, 15),
        ),
        patient_id=ERIKA_UUID,
    )
    await db.insert_document(
        make_doc(
            file_id="file_other",
            category=DocumentCategory.OTHER,
            document_date=date(2024, 2, 1),
        ),
        patient_id=ERIKA_UUID,
    )

    timeline = await db.get_treatment_timeline(patient_id=ERIKA_UUID)
    assert len(timeline) == 2  # 'other' excluded
    # Chronological ASC
    assert timeline[0].file_id == "file_surgery"
    assert timeline[1].file_id == "file_labs"


async def test_get_treatment_timeline_limit(db: Database):
    for i in range(5):
        await db.insert_document(
            make_doc(
                file_id=f"file_report_{i}",
                category=DocumentCategory.REPORT,
                document_date=date(2024, 1, i + 1),
            ),
            patient_id=ERIKA_UUID,
        )

    timeline = await db.get_treatment_timeline(limit=3, patient_id=ERIKA_UUID)
    assert len(timeline) == 3


# ── OCR cache ───────────────────────────────────────────────────────────────


async def test_save_and_get_ocr_pages(db: Database):
    doc = await db.insert_document(make_doc(), patient_id=ERIKA_UUID)
    await db.save_ocr_page(doc.id, 1, "Page 1 text", "claude-haiku-4-5-20251001")
    await db.save_ocr_page(doc.id, 2, "Page 2 text", "claude-haiku-4-5-20251001")

    pages = await db.get_ocr_pages(doc.id)
    assert len(pages) == 2
    assert pages[0]["page_number"] == 1
    assert pages[0]["extracted_text"] == "Page 1 text"
    assert pages[1]["page_number"] == 2
    assert pages[1]["extracted_text"] == "Page 2 text"


async def test_has_ocr_text(db: Database):
    doc = await db.insert_document(make_doc(), patient_id=ERIKA_UUID)
    assert await db.has_ocr_text(doc.id) is False

    await db.save_ocr_page(doc.id, 1, "Some text", "claude-haiku-4-5-20251001")
    assert await db.has_ocr_text(doc.id) is True


async def test_delete_ocr_pages(db: Database):
    doc = await db.insert_document(make_doc(), patient_id=ERIKA_UUID)
    await db.save_ocr_page(doc.id, 1, "Text", "claude-haiku-4-5-20251001")
    assert await db.has_ocr_text(doc.id) is True

    deleted = await db.delete_ocr_pages(doc.id)
    assert deleted is True
    assert await db.has_ocr_text(doc.id) is False


async def test_delete_ocr_pages_nonexistent(db: Database):
    assert await db.delete_ocr_pages(999) is False


async def test_save_ocr_page_replace(db: Database):
    """INSERT OR REPLACE should update existing page text."""
    doc = await db.insert_document(make_doc(), patient_id=ERIKA_UUID)
    await db.save_ocr_page(doc.id, 1, "Old text", "claude-haiku-4-5-20251001")
    await db.save_ocr_page(doc.id, 1, "New text", "claude-haiku-4-5-20251001")

    pages = await db.get_ocr_pages(doc.id)
    assert len(pages) == 1
    assert pages[0]["extracted_text"] == "New text"


async def test_ocr_preserved_on_soft_delete(db: Database):
    """Soft-deleting a document should preserve OCR pages (needed for restore)."""
    doc = await db.insert_document(make_doc(), patient_id=ERIKA_UUID)
    await db.save_ocr_page(doc.id, 1, "Text", "claude-haiku-4-5-20251001")

    await db.delete_document(doc.id)
    # OCR pages still exist (soft delete doesn't cascade)
    assert await db.has_ocr_text(doc.id) is True


async def test_duplicate_slug_rejected(db: Database):
    """Inserting a patient with an existing slug raises IntegrityError (#325)."""
    await db.insert_patient(Patient(patient_id="dup-test", display_name="First"))
    with pytest.raises(sqlite3.IntegrityError):
        await db.insert_patient(Patient(patient_id="dup-test", display_name="Second"))
