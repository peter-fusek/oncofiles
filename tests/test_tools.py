"""Tests for analysis tools (view_document, analyze_labs, compare_labs)."""

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pymupdf
import pytest
from fastmcp.utilities.types import Image

from oncofiles.database import Database
from oncofiles.models import DocumentCategory
from oncofiles.server import (
    analyze_labs,
    compare_labs,
    get_conversation,
    get_journey_timeline,
    log_conversation,
    search_conversations,
    view_document,
)
from oncofiles.tools.documents import (
    find_duplicates,
    list_trash,
    restore_document,
    update_document_category,
)
from tests.helpers import make_doc


@pytest.fixture(autouse=True)
def _mock_ocr():
    """Auto-mock OCR extraction for all tool tests (avoid real API calls)."""
    with patch("oncofiles.tools._helpers.extract_text_from_image", return_value="OCR text") as m:
        yield m


def _make_test_pdf() -> bytes:
    """Create a minimal valid PDF for testing."""
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    page.insert_text((10, 50), "Test")
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def _mock_ctx(
    db: Database,
    files: MagicMock | None = None,
    gdrive: MagicMock | None = None,
) -> MagicMock:
    """Create a mock Context with db, files, and gdrive in lifespan_context."""
    if files is None:
        files = MagicMock()
        files.download.return_value = b"fake-file-bytes"
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": files, "gdrive": gdrive}
    return ctx


# ── view_document ────────────────────────────────────────────────────────────


async def test_view_document_image(db: Database):
    doc = make_doc(file_id="file_img", mime_type="image/jpeg")
    await db.insert_document(doc, patient_id="erika")

    mock_files = MagicMock()
    mock_files.download.return_value = b"fake-jpeg-bytes"
    ctx = _mock_ctx(db, mock_files)

    result = await view_document(ctx, file_id="file_img")
    assert "file_img" not in result[0]  # header uses filename, not file_id
    assert any(isinstance(item, Image) for item in result)
    assert "--- Extracted Text ---" in result  # OCR text section
    assert "--- Document Images ---" in result
    mock_files.download.assert_called_once_with("file_img")


async def test_view_document_pdf(db: Database):
    doc = make_doc(file_id="file_pdf", mime_type="application/pdf")
    await db.insert_document(doc, patient_id="erika")

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    result = await view_document(ctx, file_id="file_pdf")
    assert any(isinstance(item, Image) for item in result)  # PDF pages as images
    assert "--- Extracted Text ---" in result  # OCR text section


async def test_view_document_not_found(db: Database):
    ctx = _mock_ctx(db)
    result = await view_document(ctx, file_id="file_nope")
    assert len(result) == 1
    assert "not found" in result[0].lower()


# ── analyze_labs ─────────────────────────────────────────────────────────────


async def test_analyze_labs_returns_content(db: Database):
    await db.insert_document(
        make_doc(
            file_id="file_lab1",
            category=DocumentCategory.LABS,
            document_date=date(2024, 6, 1),
        ),
        patient_id="erika",
    )
    await db.insert_document(
        make_doc(
            file_id="file_lab2",
            category=DocumentCategory.LABS,
            document_date=date(2024, 7, 1),
        ),
        patient_id="erika",
    )

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    result = await analyze_labs(ctx)
    assert "Patient" in result[0]  # patient context header
    assert "Instructions" in result[-1]
    assert any("--- Extracted Text ---" in str(item) for item in result)
    assert mock_files.download.call_count == 2


async def test_analyze_labs_specific_file_id(db: Database):
    await db.insert_document(
        make_doc(file_id="file_lab_x", category=DocumentCategory.LABS), patient_id="erika"
    )

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    result = await analyze_labs(ctx, file_id="file_lab_x")
    assert "Instructions" in result[-1]
    assert "--- Extracted Text ---" in result
    mock_files.download.assert_called_once_with("file_lab_x")


async def test_analyze_labs_wrong_category(db: Database):
    await db.insert_document(
        make_doc(file_id="file_img", category=DocumentCategory.IMAGING), patient_id="erika"
    )
    ctx = _mock_ctx(db)
    result = await analyze_labs(ctx, file_id="file_img")
    assert "not a lab result" in result[0].lower()


# ── compare_labs ─────────────────────────────────────────────────────────────


async def test_compare_labs_specific_ids(db: Database):
    await db.insert_document(
        make_doc(file_id="file_a", category=DocumentCategory.LABS, document_date=date(2024, 1, 1)),
        patient_id="erika",
    )
    await db.insert_document(
        make_doc(file_id="file_b", category=DocumentCategory.LABS, document_date=date(2024, 6, 1)),
        patient_id="erika",
    )

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    result = await compare_labs(ctx, file_id_a="file_a", file_id_b="file_b")
    assert "Instructions" in result[-1]
    assert any("--- Extracted Text ---" in str(item) for item in result)
    assert mock_files.download.call_count == 2


async def test_compare_labs_date_range(db: Database):
    await db.insert_document(
        make_doc(
            file_id="file_jan",
            category=DocumentCategory.LABS,
            document_date=date(2024, 1, 15),
        ),
        patient_id="erika",
    )
    await db.insert_document(
        make_doc(
            file_id="file_jun",
            category=DocumentCategory.LABS,
            document_date=date(2024, 6, 15),
        ),
        patient_id="erika",
    )
    await db.insert_document(
        make_doc(
            file_id="file_dec",
            category=DocumentCategory.LABS,
            document_date=date(2024, 12, 1),
        ),
        patient_id="erika",
    )

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    result = await compare_labs(ctx, date_from="2024-01-01", date_to="2024-07-01")
    # Verify chronological order — find headers by date substring
    str_items = [item for item in result if isinstance(item, str)]
    header_indices = [i for i, s in enumerate(str_items) if "2024-01" in s or "2024-06" in s]
    assert len(header_indices) == 2
    # Jan header before Jun header
    jan_idx = next(i for i, s in enumerate(str_items) if "2024-01-15" in s)
    jun_idx = next(i for i, s in enumerate(str_items) if "2024-06-15" in s)
    assert jan_idx < jun_idx


async def test_compare_labs_not_found(db: Database):
    ctx = _mock_ctx(db)
    result = await compare_labs(ctx, file_id_a="file_nope")
    assert "not found" in result[0].lower()


# ── download failure handling ────────────────────────────────────────────────


async def test_view_document_download_fails(db: Database):
    await db.insert_document(make_doc(file_id="file_fail"), patient_id="erika")

    mock_files = MagicMock()
    mock_files.download.side_effect = Exception("not_found_error: file not found")
    ctx = _mock_ctx(db, mock_files)

    result = await view_document(ctx, file_id="file_fail")
    assert len(result) == 2
    assert "not downloadable" in result[1].lower()


async def test_analyze_labs_all_downloads_fail(db: Database):
    await db.insert_document(
        make_doc(
            file_id="file_lab_fail",
            category=DocumentCategory.LABS,
        ),
        patient_id="erika",
    )

    mock_files = MagicMock()
    mock_files.download.side_effect = Exception("not downloadable")
    ctx = _mock_ctx(db, mock_files)

    result = await analyze_labs(ctx)
    assert any("error" in item.lower() for item in result if isinstance(item, str))
    assert any("re-imported" in item.lower() for item in result if isinstance(item, str))


# ── GDrive fallback chain ─────────────────────────────────────────────────


async def test_fallback_files_api_fails_gdrive_succeeds(db: Database):
    """When Files API fails but GDrive works, content should be returned."""
    await db.insert_document(
        make_doc(file_id="file_fb1", gdrive_id="gdrive_abc123"), patient_id="erika"
    )

    mock_files = MagicMock()
    mock_files.download.side_effect = Exception("not downloadable")
    mock_gdrive = MagicMock()
    mock_gdrive.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files, mock_gdrive)

    result = await view_document(ctx, file_id="file_fb1")
    assert any(isinstance(item, Image) for item in result)  # PDF pages as images
    assert "--- Extracted Text ---" in result  # OCR text present
    mock_gdrive.download.assert_called_once_with("gdrive_abc123")


async def test_fallback_both_fail(db: Database):
    """When both Files API and GDrive fail, error message should be returned."""
    await db.insert_document(
        make_doc(file_id="file_fb2", gdrive_id="gdrive_fail"), patient_id="erika"
    )

    mock_files = MagicMock()
    mock_files.download.side_effect = Exception("not downloadable")
    mock_gdrive = MagicMock()
    mock_gdrive.download.side_effect = Exception("GDrive 403 forbidden")
    ctx = _mock_ctx(db, mock_files, mock_gdrive)

    result = await view_document(ctx, file_id="file_fb2")
    assert len(result) >= 2
    assert "gdrive download also failed" in result[1].lower()


async def test_fallback_no_gdrive_id(db: Database):
    """When Files API fails and doc has no gdrive_id, show appropriate error."""
    await db.insert_document(make_doc(file_id="file_fb3"), patient_id="erika")  # no gdrive_id

    mock_files = MagicMock()
    mock_files.download.side_effect = Exception("not downloadable")
    ctx = _mock_ctx(db, mock_files)

    result = await view_document(ctx, file_id="file_fb3")
    assert len(result) >= 2
    assert "no gdrive_id" in result[1].lower()


async def test_fallback_no_gdrive_client(db: Database):
    """When Files API fails, gdrive_id exists, but no GDrive client configured."""
    await db.insert_document(
        make_doc(file_id="file_fb4", gdrive_id="gdrive_xyz"), patient_id="erika"
    )

    mock_files = MagicMock()
    mock_files.download.side_effect = Exception("not downloadable")
    ctx = _mock_ctx(db, mock_files, gdrive=None)

    result = await view_document(ctx, file_id="file_fb4")
    assert len(result) >= 2
    assert "not configured" in result[1].lower()


async def test_analyze_labs_gdrive_fallback(db: Database):
    """analyze_labs should use GDrive fallback when Files API fails."""
    await db.insert_document(
        make_doc(
            file_id="file_lab_fb",
            category=DocumentCategory.LABS,
            gdrive_id="gdrive_lab1",
        ),
        patient_id="erika",
    )

    mock_files = MagicMock()
    mock_files.download.side_effect = Exception("not downloadable")
    mock_gdrive = MagicMock()
    mock_gdrive.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files, mock_gdrive)

    result = await analyze_labs(ctx)
    # patient context + header + page image(s) + instructions (no error)
    assert len(result) >= 4
    assert "Instructions" in result[-1]
    mock_gdrive.download.assert_called_once_with("gdrive_lab1")


# ── OCR integration ──────────────────────────────────────────────────────────


async def test_view_document_includes_ocr_text(db: Database):
    """view_document should include extracted OCR text before images."""
    doc = make_doc(file_id="file_ocr1", mime_type="application/pdf")
    await db.insert_document(doc, patient_id="erika")

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    with patch("oncofiles.tools._helpers.extract_text_from_image") as mock_ocr:
        mock_ocr.return_value = "Hemoglobín: 135 g/L"
        result = await view_document(ctx, file_id="file_ocr1")

    # header + "--- Extracted Text ---" + text + "--- Document Images ---" + image(s)
    assert "--- Extracted Text ---" in result
    assert "Hemoglobín: 135 g/L" in result
    assert "--- Document Images ---" in result
    assert any(isinstance(item, Image) for item in result)
    mock_ocr.assert_called_once()


async def test_view_document_ocr_cache_hit(db: Database):
    """Second call should use cached OCR text, not call extract again."""
    doc = make_doc(file_id="file_ocr2", mime_type="application/pdf")
    inserted = await db.insert_document(doc, patient_id="erika")

    # Pre-populate cache
    await db.save_ocr_page(inserted.id, 1, "Cached text", "claude-haiku-4-5-20251001")

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    with patch("oncofiles.tools._helpers.extract_text_from_image") as mock_ocr:
        result = await view_document(ctx, file_id="file_ocr2")

    # Should NOT call OCR (cache hit)
    mock_ocr.assert_not_called()
    assert "Cached text" in result
    assert "--- Extracted Text ---" in result


async def test_analyze_labs_includes_ocr_text(db: Database):
    """analyze_labs should include OCR text for each lab."""
    await db.insert_document(
        make_doc(
            file_id="file_lab_ocr",
            category=DocumentCategory.LABS,
        ),
        patient_id="erika",
    )

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    with patch("oncofiles.tools._helpers.extract_text_from_image") as mock_ocr:
        mock_ocr.return_value = "WBC: 5.2 x10^9/L"
        result = await analyze_labs(ctx)

    assert "--- Extracted Text ---" in result
    assert "WBC: 5.2 x10^9/L" in result
    assert "Instructions" in result[-1]


# ── Conversation archive tools (#37) ────────────────────────────────────────


async def test_log_conversation(db: Database):
    ctx = _mock_ctx(db)
    # Mock session_id access
    ctx.session_id = "test-session-123"

    result = await log_conversation(
        ctx,
        title="FOLFOX cycle 3 started",
        content="Started FOLFOX cycle 3 today. Nausea managed well.",
        entry_date="2025-03-01",
        entry_type="progress",
        tags="chemo,FOLFOX",
    )
    import json

    data = json.loads(result)
    assert data["id"] is not None
    assert data["title"] == "FOLFOX cycle 3 started"
    assert data["entry_type"] == "progress"
    assert data["tags"] == ["chemo", "FOLFOX"]


async def test_search_conversations(db: Database):
    ctx = _mock_ctx(db)
    ctx.session_id = None

    # Create two entries
    await log_conversation(
        ctx,
        title="Lab review discussion",
        content="Discussed blood counts with oncologist.",
        entry_date="2025-03-01",
    )
    await log_conversation(
        ctx,
        title="FOLFOX decision",
        content="Decided to continue FOLFOX protocol.",
        entry_date="2025-03-02",
        entry_type="decision",
    )

    result = await search_conversations(ctx, text="FOLFOX")
    import json

    data = json.loads(result)
    assert "entries" in data
    assert data["total"] == 1
    assert "FOLFOX" in data["entries"][0]["title"]


async def test_get_conversation(db: Database):
    ctx = _mock_ctx(db)
    ctx.session_id = None

    result = await log_conversation(
        ctx,
        title="Test entry",
        content="Full content here with lots of detail.",
    )
    import json

    entry_id = json.loads(result)["id"]

    full = await get_conversation(ctx, entry_id=entry_id)
    data = json.loads(full)
    assert data["content"] == "Full content here with lots of detail."
    assert data["source"] == "live"


async def test_get_journey_timeline(db: Database):
    ctx = _mock_ctx(db)
    ctx.session_id = None

    # Create a document + a conversation entry
    await db.insert_document(
        make_doc(file_id="file_timeline", document_date=date(2025, 2, 1)), patient_id="erika"
    )
    await log_conversation(
        ctx,
        title="Treatment discussion",
        content="Discussed treatment plan.",
        entry_date="2025-02-15",
    )

    result = await get_journey_timeline(ctx)
    import json

    timeline = json.loads(result)
    assert len(timeline) == 2
    # Check chronological order
    assert timeline[0]["date"] <= timeline[1]["date"]
    types = {item["type"] for item in timeline}
    assert "document" in types
    assert "conversation" in types


# ── find_duplicates tool (#v3.2.0) ───────────────────────────────────────


async def test_find_duplicates_no_dupes(db: Database):
    ctx = _mock_ctx(db)
    await db.insert_document(
        make_doc(file_id="file_a", filename="20240115_labs.pdf"), patient_id="erika"
    )
    result = json.loads(await find_duplicates(ctx))
    assert result["total_groups"] == 0


async def test_find_duplicates_with_dupes(db: Database):
    ctx = _mock_ctx(db)
    await db.insert_document(
        make_doc(
            file_id="file_a", filename="dup.pdf", original_filename="orig.pdf", size_bytes=100
        ),
        patient_id="erika",
    )
    await db.insert_document(
        make_doc(
            file_id="file_b", filename="dup2.pdf", original_filename="orig.pdf", size_bytes=100
        ),
        patient_id="erika",
    )
    result = json.loads(await find_duplicates(ctx))
    assert result["total_groups"] == 1
    assert result["duplicate_groups"][0]["count"] == 2


# ── restore_document tool (#v3.2.0) ─────────────────────────────────────


async def test_restore_document_success(db: Database):
    ctx = _mock_ctx(db)
    doc = await db.insert_document(make_doc(file_id="file_del"), patient_id="erika")
    await db.delete_document_by_file_id("file_del", patient_id="erika")

    result = json.loads(await restore_document(ctx, doc_id=doc.id))
    assert result["restored"] is True
    assert result["doc_id"] == doc.id


async def test_restore_document_not_found(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await restore_document(ctx, doc_id=9999))
    assert result["restored"] is False


# ── list_trash tool (#v3.2.0) ────────────────────────────────────────────


async def test_list_trash_empty(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await list_trash(ctx))
    assert result["total"] == 0
    assert result["trash"] == []


async def test_list_trash_with_items(db: Database):
    ctx = _mock_ctx(db)
    await db.insert_document(make_doc(file_id="file_trash1"), patient_id="erika")
    await db.delete_document_by_file_id("file_trash1", patient_id="erika")

    result = json.loads(await list_trash(ctx))
    assert result["total"] == 1
    assert result["trash"][0]["file_id"] == "file_trash1"


# ── upload_document Files API error ──────────────────────────────────────


async def test_upload_document_files_api_error(db: Database):
    """upload_document returns JSON error when Files API fails."""
    import base64

    from oncofiles.tools.documents import upload_document

    mock_files = MagicMock()
    mock_files.upload.side_effect = Exception("Files API 500 error")
    ctx = _mock_ctx(db, mock_files)
    ctx.info = AsyncMock()

    content = base64.b64encode(b"fake pdf content").decode()
    result = json.loads(await upload_document(ctx, content=content, filename="test.pdf"))
    assert "error" in result
    assert "Files API upload failed" in result["error"]


# ── log_conversation invalid document_ids ────────────────────────────────


async def test_log_conversation_invalid_document_ids(db: Database):
    """log_conversation returns error on non-numeric document_ids."""
    ctx = _mock_ctx(db)
    ctx.session_id = None

    result = json.loads(
        await log_conversation(
            ctx,
            title="Test",
            content="Test content",
            document_ids="abc,xyz",
        )
    )
    assert "error" in result
    assert "Invalid document_ids" in result["error"]


# ── extract_all_metadata ──────────────────────────────────────────────────


async def test_extract_all_metadata(db: Database):
    """extract_all_metadata tool calls sync function and returns stats."""
    from oncofiles.tools.enhance_tools import extract_all_metadata

    ctx = _mock_ctx(db)

    with patch(
        "oncofiles.sync.extract_all_metadata",
        new_callable=AsyncMock,
        return_value={"processed": 3, "skipped": 1, "errors": 0},
    ) as mock_fn:
        result = json.loads(await extract_all_metadata(ctx))
        assert result == {"processed": 3, "skipped": 1, "errors": 0}
        mock_fn.assert_called_once()


# ── upload_document auto-sync to GDrive ──────────────────────────────────


async def test_upload_document_auto_syncs_to_gdrive(db: Database):
    """upload_document auto-syncs to GDrive when client is available."""
    import base64

    from oncofiles.tools.documents import upload_document

    mock_files = MagicMock()
    mock_files.upload.return_value = MagicMock(
        id="file_123", mime_type="application/pdf", size_bytes=100
    )

    mock_gdrive = MagicMock()
    mock_gdrive.upload.return_value = {"id": "gdrive_456", "modifiedTime": "2026-03-09T10:00:00Z"}

    ctx = _mock_ctx(db, mock_files, mock_gdrive)
    ctx.request_context.lifespan_context["gdrive_folder_id"] = "root_folder"
    ctx.info = AsyncMock()

    # Mock ensure_folder_structure to return a simple map
    with patch(
        "oncofiles.tools.documents.ensure_folder_structure",
        return_value={"other": "other_folder_id"},
    ):
        content = base64.b64encode(b"fake pdf content").decode()
        result = json.loads(
            await upload_document(ctx, content=content, filename="20260301_test.pdf")
        )

    assert result["id"] is not None
    assert result["gdrive_url"] == "https://drive.google.com/file/d/gdrive_456/view"
    mock_gdrive.upload.assert_called_once()


async def test_upload_document_gdrive_failure_nonfatal(db: Database):
    """upload_document succeeds even when GDrive sync fails."""
    import base64

    from oncofiles.tools.documents import upload_document

    mock_files = MagicMock()
    mock_files.upload.return_value = MagicMock(
        id="file_789", mime_type="application/pdf", size_bytes=100
    )

    mock_gdrive = MagicMock()
    mock_gdrive.upload.side_effect = Exception("GDrive API error")

    ctx = _mock_ctx(db, mock_files, mock_gdrive)
    ctx.request_context.lifespan_context["gdrive_folder_id"] = "root_folder"
    ctx.info = AsyncMock()

    with patch(
        "oncofiles.tools.documents.ensure_folder_structure",
        return_value={"other": "other_folder_id"},
    ):
        content = base64.b64encode(b"fake pdf content").decode()
        result = json.loads(
            await upload_document(ctx, content=content, filename="20260301_test.pdf")
        )

    # Upload still succeeded despite GDrive failure
    assert result["id"] is not None
    assert "gdrive_id" not in result
    assert "error" not in result


# ── update_document_category tool ─────────────────────────────────────────


async def test_update_document_category_success(db: Database):
    """update_document_category changes category and returns old/new."""
    ctx = _mock_ctx(db)
    doc = await db.insert_document(
        make_doc(file_id="file_cat", category=DocumentCategory.OTHER), patient_id="erika"
    )

    result = json.loads(await update_document_category(ctx, doc_id=doc.id, category="reference"))
    assert result["old_category"] == "other"
    assert result["new_category"] == "reference"
    assert result["id"] == doc.id

    # Verify persisted
    updated = await db.get_document(doc.id)
    assert updated.category == DocumentCategory.REFERENCE


async def test_update_document_category_invalid(db: Database):
    """update_document_category rejects invalid category."""
    ctx = _mock_ctx(db)
    doc = await db.insert_document(make_doc(file_id="file_cat2"), patient_id="erika")

    result = json.loads(await update_document_category(ctx, doc_id=doc.id, category="bogus"))
    assert "error" in result
    assert "Invalid category" in result["error"]


async def test_update_document_category_not_found(db: Database):
    """update_document_category returns error for missing doc."""
    ctx = _mock_ctx(db)
    result = json.loads(await update_document_category(ctx, doc_id=9999, category="reference"))
    assert "error" in result
    assert "not found" in result["error"]
