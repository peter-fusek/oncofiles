"""Tests for analysis tools (view_document, analyze_labs, compare_labs)."""

from datetime import date
from unittest.mock import MagicMock

import pymupdf
from fastmcp.utilities.types import Image

from erika_files_mcp.database import Database
from erika_files_mcp.models import DocumentCategory
from erika_files_mcp.server import analyze_labs, compare_labs, view_document
from tests.helpers import make_doc


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
    await db.insert_document(doc)

    mock_files = MagicMock()
    mock_files.download.return_value = b"fake-jpeg-bytes"
    ctx = _mock_ctx(db, mock_files)

    result = await view_document(ctx, file_id="file_img")
    assert len(result) == 2
    assert "file_img" not in result[0]  # header uses filename, not file_id
    assert isinstance(result[1], Image)
    mock_files.download.assert_called_once_with("file_img")


async def test_view_document_pdf(db: Database):
    doc = make_doc(file_id="file_pdf", mime_type="application/pdf")
    await db.insert_document(doc)

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    result = await view_document(ctx, file_id="file_pdf")
    assert len(result) >= 2  # header + at least 1 page image
    assert isinstance(result[1], Image)  # PDF pages converted to JPEG images


async def test_view_document_not_found(db: Database):
    ctx = _mock_ctx(db)
    result = await view_document(ctx, file_id="file_nope")
    assert len(result) == 1
    assert "not found" in result[0].lower()


# ── analyze_labs ─────────────────────────────────────────────────────────────


async def test_analyze_labs_returns_content(db: Database):
    await db.insert_document(make_doc(
        file_id="file_lab1", category=DocumentCategory.LABS, document_date=date(2024, 6, 1),
    ))
    await db.insert_document(make_doc(
        file_id="file_lab2", category=DocumentCategory.LABS, document_date=date(2024, 7, 1),
    ))

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    result = await analyze_labs(ctx)
    # patient context + (header + content) * 2 + instructions
    assert len(result) == 6
    assert "Erika Fusekova" in result[0]
    assert "Instructions" in result[-1]
    assert mock_files.download.call_count == 2


async def test_analyze_labs_specific_file_id(db: Database):
    await db.insert_document(
        make_doc(file_id="file_lab_x", category=DocumentCategory.LABS)
    )

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    result = await analyze_labs(ctx, file_id="file_lab_x")
    # patient context + header + content + instructions
    assert len(result) == 4
    mock_files.download.assert_called_once_with("file_lab_x")


async def test_analyze_labs_wrong_category(db: Database):
    await db.insert_document(
        make_doc(file_id="file_img", category=DocumentCategory.IMAGING)
    )
    ctx = _mock_ctx(db)
    result = await analyze_labs(ctx, file_id="file_img")
    assert "not a lab result" in result[0].lower()


# ── compare_labs ─────────────────────────────────────────────────────────────


async def test_compare_labs_specific_ids(db: Database):
    await db.insert_document(
        make_doc(file_id="file_a", category=DocumentCategory.LABS, document_date=date(2024, 1, 1))
    )
    await db.insert_document(
        make_doc(file_id="file_b", category=DocumentCategory.LABS, document_date=date(2024, 6, 1))
    )

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    result = await compare_labs(ctx, file_id_a="file_a", file_id_b="file_b")
    # patient context + (header + content) * 2 + instructions
    assert len(result) == 6
    assert "Instructions" in result[-1]
    assert mock_files.download.call_count == 2


async def test_compare_labs_date_range(db: Database):
    await db.insert_document(make_doc(
        file_id="file_jan", category=DocumentCategory.LABS, document_date=date(2024, 1, 15),
    ))
    await db.insert_document(make_doc(
        file_id="file_jun", category=DocumentCategory.LABS, document_date=date(2024, 6, 15),
    ))
    await db.insert_document(make_doc(
        file_id="file_dec", category=DocumentCategory.LABS, document_date=date(2024, 12, 1),
    ))

    mock_files = MagicMock()
    mock_files.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files)

    result = await compare_labs(ctx, date_from="2024-01-01", date_to="2024-07-01")
    # patient context + (header + content) * 2 + instructions (jan + jun, dec excluded)
    assert len(result) == 6
    # Verify chronological order (oldest first in headers)
    assert "2024-01-15" in result[1]
    assert "2024-06-15" in result[3]


async def test_compare_labs_not_found(db: Database):
    ctx = _mock_ctx(db)
    result = await compare_labs(ctx, file_id_a="file_nope")
    assert "not found" in result[0].lower()


# ── download failure handling ────────────────────────────────────────────────


async def test_view_document_download_fails(db: Database):
    await db.insert_document(make_doc(file_id="file_fail"))

    mock_files = MagicMock()
    mock_files.download.side_effect = Exception("not_found_error: file not found")
    ctx = _mock_ctx(db, mock_files)

    result = await view_document(ctx, file_id="file_fail")
    assert len(result) == 2
    assert "not downloadable" in result[1].lower()


async def test_analyze_labs_all_downloads_fail(db: Database):
    await db.insert_document(make_doc(
        file_id="file_lab_fail", category=DocumentCategory.LABS,
    ))

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
        make_doc(file_id="file_fb1", gdrive_id="gdrive_abc123")
    )

    mock_files = MagicMock()
    mock_files.download.side_effect = Exception("not downloadable")
    mock_gdrive = MagicMock()
    mock_gdrive.download.return_value = _make_test_pdf()
    ctx = _mock_ctx(db, mock_files, mock_gdrive)

    result = await view_document(ctx, file_id="file_fb1")
    assert len(result) >= 2
    assert isinstance(result[1], Image)  # PDF pages converted to images
    mock_gdrive.download.assert_called_once_with("gdrive_abc123")


async def test_fallback_both_fail(db: Database):
    """When both Files API and GDrive fail, error message should be returned."""
    await db.insert_document(
        make_doc(file_id="file_fb2", gdrive_id="gdrive_fail")
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
    await db.insert_document(make_doc(file_id="file_fb3"))  # no gdrive_id

    mock_files = MagicMock()
    mock_files.download.side_effect = Exception("not downloadable")
    ctx = _mock_ctx(db, mock_files)

    result = await view_document(ctx, file_id="file_fb3")
    assert len(result) >= 2
    assert "no gdrive_id" in result[1].lower()


async def test_fallback_no_gdrive_client(db: Database):
    """When Files API fails, gdrive_id exists, but no GDrive client configured."""
    await db.insert_document(
        make_doc(file_id="file_fb4", gdrive_id="gdrive_xyz")
    )

    mock_files = MagicMock()
    mock_files.download.side_effect = Exception("not downloadable")
    ctx = _mock_ctx(db, mock_files, gdrive=None)

    result = await view_document(ctx, file_id="file_fb4")
    assert len(result) >= 2
    assert "not configured" in result[1].lower()


async def test_analyze_labs_gdrive_fallback(db: Database):
    """analyze_labs should use GDrive fallback when Files API fails."""
    await db.insert_document(make_doc(
        file_id="file_lab_fb", category=DocumentCategory.LABS, gdrive_id="gdrive_lab1",
    ))

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
