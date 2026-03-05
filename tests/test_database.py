"""Tests for the database module."""

from datetime import date

from erika_files_mcp.database import Database
from erika_files_mcp.models import DocumentCategory, SearchQuery
from tests.helpers import make_doc


async def test_insert_and_get(db: Database):
    doc = make_doc()
    result = await db.insert_document(doc)
    assert result.id is not None

    fetched = await db.get_document(result.id)
    assert fetched is not None
    assert fetched.file_id == "file_test123"
    assert fetched.institution == "NOUonko"
    assert fetched.category == DocumentCategory.LABS


async def test_get_by_file_id(db: Database):
    doc = make_doc()
    await db.insert_document(doc)

    fetched = await db.get_document_by_file_id("file_test123")
    assert fetched is not None
    assert fetched.filename == "20240115_NOUonko_labs_krvnyObraz.pdf"


async def test_get_nonexistent(db: Database):
    assert await db.get_document(999) is None
    assert await db.get_document_by_file_id("file_nope") is None


async def test_list_documents(db: Database):
    await db.insert_document(make_doc(file_id="file_1", document_date=date(2024, 1, 1)))
    await db.insert_document(make_doc(file_id="file_2", document_date=date(2024, 3, 1)))
    await db.insert_document(make_doc(file_id="file_3", document_date=date(2024, 2, 1)))

    docs = await db.list_documents()
    assert len(docs) == 3
    # Should be ordered by date descending
    assert docs[0].file_id == "file_2"
    assert docs[1].file_id == "file_3"
    assert docs[2].file_id == "file_1"


async def test_list_with_limit(db: Database):
    for i in range(10):
        await db.insert_document(make_doc(file_id=f"file_{i}"))

    docs = await db.list_documents(limit=3)
    assert len(docs) == 3


async def test_delete_document(db: Database):
    doc = make_doc()
    result = await db.insert_document(doc)

    deleted = await db.delete_document(result.id)
    assert deleted is True

    assert await db.get_document(result.id) is None


async def test_delete_by_file_id(db: Database):
    await db.insert_document(make_doc())
    deleted = await db.delete_document_by_file_id("file_test123")
    assert deleted is True

    assert await db.get_document_by_file_id("file_test123") is None


async def test_delete_nonexistent(db: Database):
    assert await db.delete_document(999) is False
    assert await db.delete_document_by_file_id("file_nope") is False


async def test_search_by_text(db: Database):
    await db.insert_document(
        make_doc(
            file_id="file_1",
            filename="20240115_NOUonko_labs_krvnyObraz.pdf",
            description="krvny obraz",
            institution="NOUonko",
        )
    )
    await db.insert_document(
        make_doc(
            file_id="file_2",
            filename="20240301_UNB_imaging_CTabdomen.pdf",
            original_filename="20240301_UNB_imaging_CTabdomen.pdf",
            description="CT abdomen",
            institution="UNB",
        )
    )

    results = await db.search_documents(SearchQuery(text="krvny"))
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
        )
    )
    await db.insert_document(
        make_doc(file_id="file_other", description="CT abdomen")
    )

    # Substring in filename
    results = await db.search_documents(SearchQuery(text="genetik"))
    assert len(results) == 1
    assert results[0].file_id == "file_genetika"

    # Case-insensitive
    results = await db.search_documents(SearchQuery(text="GENETIK"))
    assert len(results) == 1
    assert results[0].file_id == "file_genetika"

    # Substring in original_filename
    results = await db.search_documents(SearchQuery(text="Malejcik"))
    assert len(results) == 1
    assert results[0].file_id == "file_genetika"


async def test_search_by_text_no_match(db: Database):
    """Text search returns empty when nothing matches."""
    await db.insert_document(make_doc(file_id="file_1", description="krvny obraz"))

    results = await db.search_documents(SearchQuery(text="nonexistent"))
    assert len(results) == 0


async def test_search_by_institution(db: Database):
    await db.insert_document(make_doc(file_id="file_1", institution="NOUonko"))
    await db.insert_document(make_doc(file_id="file_2", institution="OUSA"))

    results = await db.search_documents(SearchQuery(institution="OUSA"))
    assert len(results) == 1
    assert results[0].file_id == "file_2"


async def test_search_by_category(db: Database):
    await db.insert_document(make_doc(file_id="file_1", category=DocumentCategory.LABS))
    await db.insert_document(make_doc(file_id="file_2", category=DocumentCategory.IMAGING))

    results = await db.search_documents(SearchQuery(category=DocumentCategory.IMAGING))
    assert len(results) == 1
    assert results[0].file_id == "file_2"


async def test_search_by_date_range(db: Database):
    await db.insert_document(make_doc(file_id="file_1", document_date=date(2024, 1, 1)))
    await db.insert_document(make_doc(file_id="file_2", document_date=date(2024, 6, 1)))
    await db.insert_document(make_doc(file_id="file_3", document_date=date(2024, 12, 1)))

    results = await db.search_documents(
        SearchQuery(date_from=date(2024, 3, 1), date_to=date(2024, 9, 1))
    )
    assert len(results) == 1
    assert results[0].file_id == "file_2"


async def test_get_latest_labs(db: Database):
    await db.insert_document(
        make_doc(
            file_id="file_lab1",
            category=DocumentCategory.LABS,
            document_date=date(2024, 1, 1),
        )
    )
    await db.insert_document(
        make_doc(
            file_id="file_lab2",
            category=DocumentCategory.LABS,
            document_date=date(2024, 6, 1),
        )
    )
    await db.insert_document(
        make_doc(
            file_id="file_img",
            category=DocumentCategory.IMAGING,
            document_date=date(2024, 12, 1),
        )
    )

    labs = await db.get_latest_labs(limit=5)
    assert len(labs) == 2
    assert labs[0].file_id == "file_lab2"  # newest first
    assert labs[1].file_id == "file_lab1"


async def test_get_treatment_timeline(db: Database):
    await db.insert_document(
        make_doc(
            file_id="file_surgery",
            category=DocumentCategory.SURGERY,
            document_date=date(2024, 1, 10),
        )
    )
    await db.insert_document(
        make_doc(
            file_id="file_labs",
            category=DocumentCategory.LABS,
            document_date=date(2024, 3, 15),
        )
    )
    await db.insert_document(
        make_doc(
            file_id="file_other",
            category=DocumentCategory.OTHER,
            document_date=date(2024, 2, 1),
        )
    )

    timeline = await db.get_treatment_timeline()
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
            )
        )

    timeline = await db.get_treatment_timeline(limit=3)
    assert len(timeline) == 3


# ── OCR cache ───────────────────────────────────────────────────────────────


async def test_save_and_get_ocr_pages(db: Database):
    doc = await db.insert_document(make_doc())
    await db.save_ocr_page(doc.id, 1, "Page 1 text", "claude-haiku-4-5-20251001")
    await db.save_ocr_page(doc.id, 2, "Page 2 text", "claude-haiku-4-5-20251001")

    pages = await db.get_ocr_pages(doc.id)
    assert len(pages) == 2
    assert pages[0]["page_number"] == 1
    assert pages[0]["extracted_text"] == "Page 1 text"
    assert pages[1]["page_number"] == 2
    assert pages[1]["extracted_text"] == "Page 2 text"


async def test_has_ocr_text(db: Database):
    doc = await db.insert_document(make_doc())
    assert await db.has_ocr_text(doc.id) is False

    await db.save_ocr_page(doc.id, 1, "Some text", "claude-haiku-4-5-20251001")
    assert await db.has_ocr_text(doc.id) is True


async def test_delete_ocr_pages(db: Database):
    doc = await db.insert_document(make_doc())
    await db.save_ocr_page(doc.id, 1, "Text", "claude-haiku-4-5-20251001")
    assert await db.has_ocr_text(doc.id) is True

    deleted = await db.delete_ocr_pages(doc.id)
    assert deleted is True
    assert await db.has_ocr_text(doc.id) is False


async def test_delete_ocr_pages_nonexistent(db: Database):
    assert await db.delete_ocr_pages(999) is False


async def test_save_ocr_page_replace(db: Database):
    """INSERT OR REPLACE should update existing page text."""
    doc = await db.insert_document(make_doc())
    await db.save_ocr_page(doc.id, 1, "Old text", "claude-haiku-4-5-20251001")
    await db.save_ocr_page(doc.id, 1, "New text", "claude-haiku-4-5-20251001")

    pages = await db.get_ocr_pages(doc.id)
    assert len(pages) == 1
    assert pages[0]["extracted_text"] == "New text"


async def test_ocr_cascade_delete(db: Database):
    """Deleting a document should cascade-delete its OCR pages."""
    doc = await db.insert_document(make_doc())
    await db.save_ocr_page(doc.id, 1, "Text", "claude-haiku-4-5-20251001")

    await db.delete_document(doc.id)
    assert await db.has_ocr_text(doc.id) is False
