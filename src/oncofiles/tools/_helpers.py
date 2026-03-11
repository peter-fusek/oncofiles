"""Shared helper functions for tool modules."""

from __future__ import annotations

import json
import logging
from datetime import date

from fastmcp import Context
from fastmcp.utilities.types import Image

from oncofiles import patient_context
from oncofiles.database import Database
from oncofiles.files_api import FilesClient
from oncofiles.gdrive_client import GDriveClient
from oncofiles.models import Document
from oncofiles.ocr import OCR_MODEL, extract_text_from_image

logger = logging.getLogger(__name__)

GDRIVE_FILE_URL = "https://drive.google.com/file/d/{}/view"
PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/{}/"
CLINICALTRIALS_URL = "https://clinicaltrials.gov/study/{}"


# ── Source attribution helpers ───────────────────────────────────────────────


def _gdrive_url(gdrive_id: str | None) -> str | None:
    """Build a Google Drive view URL from a file ID."""
    return GDRIVE_FILE_URL.format(gdrive_id) if gdrive_id else None


def _research_source_url(source: str, external_id: str) -> str | None:
    """Build an external URL for a research entry based on source type."""
    if not external_id:
        return None
    source_lower = source.lower()
    if source_lower == "pubmed":
        # external_id may be "PMID:12345" or just "12345"
        numeric = external_id.replace("PMID:", "").replace("PMID", "").strip()
        if numeric.isdigit():
            return PUBMED_URL.format(numeric)
    elif source_lower in ("clinicaltrials", "clinicaltrials.gov"):
        # external_id is typically "NCT04123456"
        eid = external_id.strip()
        if eid.upper().startswith("NCT"):
            return CLINICALTRIALS_URL.format(eid)
    return None


# ── Patient context (delegated to patient_context module) ────────────────────

# Backward-compatible alias — returns the live context dict
PATIENT_CONTEXT = patient_context.get_context()


def _patient_context_text() -> str:
    return patient_context.format_context_text()


# ── Context accessors ────────────────────────────────────────────────────────


def _get_db(ctx: Context) -> Database:
    return ctx.request_context.lifespan_context["db"]


def _get_files(ctx: Context) -> FilesClient:
    return ctx.request_context.lifespan_context["files"]


def _get_gdrive(ctx: Context) -> GDriveClient | None:
    return ctx.request_context.lifespan_context.get("gdrive")


def _parse_date(value: str | None) -> date | None:
    """Parse a YYYY-MM-DD date string, raising ValueError with a friendly message."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid date format: '{value}'. Expected YYYY-MM-DD.") from None


def _clamp_limit(limit: int, max_val: int = 200) -> int:
    """Clamp limit to [1, max_val]."""
    return min(max(limit, 1), max_val)


def _doc_to_dict(d: Document) -> dict:
    """Convert a Document to a JSON-serializable dict for tool output."""
    result = {
        "id": d.id,
        "file_id": d.file_id,
        "filename": d.filename,
        "document_date": d.document_date.isoformat() if d.document_date else None,
        "institution": d.institution,
        "category": d.category.value,
        "description": d.description,
        "gdrive_url": _gdrive_url(d.gdrive_id),
    }
    if d.version > 1:
        result["version"] = d.version
    if d.previous_version_id:
        result["previous_version_id"] = d.previous_version_id
    if d.ai_summary:
        result["ai_summary"] = d.ai_summary
    if d.ai_tags:
        result["ai_tags"] = d.ai_tags
    if d.structured_metadata:
        result["structured_metadata"] = json.loads(d.structured_metadata)
    return result


def _doc_header(doc: Document) -> str:
    date_str = doc.document_date.isoformat() if doc.document_date else "unknown"
    return (
        f"**{doc.filename}** | {doc.category.value} | {date_str} | {doc.institution or 'unknown'}"
    )


def _pdf_to_images(content_bytes: bytes) -> list[Image]:
    """Convert PDF pages to JPEG images using pymupdf."""
    import pymupdf

    images = []
    doc = pymupdf.open(stream=content_bytes, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            images.append(Image(data=pix.tobytes("jpeg"), format="jpeg"))
    finally:
        doc.close()
    return images


def _inline_content(doc: Document, content_bytes: bytes) -> list[str | Image]:
    """Return the appropriate inline content for a document.

    Returns a list of content items. PDFs are converted to per-page JPEG images
    since Claude.ai connectors don't support EmbeddedResource (PDF) content.
    """
    if doc.mime_type and doc.mime_type.startswith("image/"):
        fmt = doc.mime_type.split("/")[1]  # jpeg, png, etc.
        return [Image(data=content_bytes, format=fmt)]
    elif doc.mime_type == "application/pdf":
        return _pdf_to_images(content_bytes)
    else:
        return [content_bytes.decode("utf-8", errors="replace")]


def _try_download(
    files: FilesClient,
    doc: Document,
    gdrive: GDriveClient | None = None,
) -> tuple[bool, list[str | Image], bytes | None]:
    """Try to download file content. Falls back to Google Drive if available.

    Returns (success, content_items, raw_bytes).
    """
    # 1. Try Files API
    try:
        content_bytes = files.download(doc.file_id)
        return True, _inline_content(doc, content_bytes), content_bytes
    except Exception:
        logger.debug("Files API download failed for %s", doc.file_id)

    # 2. Fallback: Google Drive
    if gdrive and doc.gdrive_id:
        try:
            content_bytes = gdrive.download(doc.gdrive_id)
            return True, _inline_content(doc, content_bytes), content_bytes
        except Exception as e:
            return False, [f"[GDrive download also failed: {e}]"], None

    if not doc.gdrive_id:
        return False, ["[Not downloadable. No gdrive_id for fallback — see #35]"], None
    return False, ["[Not downloadable. GDrive client not configured — see #35]"], None


def _extract_pdf_text(content_bytes: bytes) -> list[str] | None:
    """Try to extract embedded text from a PDF using pymupdf.

    Returns list of per-page text if PDF has substantial embedded text,
    or None if the PDF appears to be scanned (no text).
    """
    import pymupdf

    doc = pymupdf.open(stream=content_bytes, filetype="pdf")
    try:
        texts = []
        pages_with_text = 0
        for page in doc:
            text = page.get_text().strip()
            texts.append(text)
            if len(text) > 50:  # non-trivial text content
                pages_with_text += 1
        # If majority of pages have text, it's a text PDF
        if pages_with_text > len(texts) / 2:
            return texts
        return None
    finally:
        doc.close()


def _resize_image_if_needed(image: Image, max_b64_bytes: int = 5_200_000) -> Image:
    """Resize image if its base64 encoding would exceed API limit (5MB).

    JPEG recompression bloats sizes, so we scale aggressively to stay under limit.
    """
    import base64

    if len(base64.b64encode(image.data)) <= max_b64_bytes:
        return image
    import pymupdf

    pix = pymupdf.Pixmap(image.data)
    # Target 3MB raw JPEG (well under 5MB b64 even after recompression bloat)
    scale = min(0.7, (3_000_000 / len(image.data)) ** 0.5)
    new_w = int(pix.width * scale)
    new_h = int(pix.height * scale)
    pix2 = pymupdf.Pixmap(pix, new_w, new_h)
    return Image(data=pix2.tobytes("jpeg"), format="jpeg")


async def _ensure_ocr_text(
    db: Database,
    doc: Document,
    content_items: list[str | Image],
    content_bytes: bytes | None = None,
) -> list[str]:
    """Get text for a document: cache -> PDF native text -> Vision OCR.

    Returns a list of extracted text strings (one per page).
    """
    # 1. Check cache
    if await db.has_ocr_text(doc.id):
        pages = await db.get_ocr_pages(doc.id)
        return [p["extracted_text"] for p in pages]

    # 2. For PDFs, try native text extraction first (free, fast)
    if doc.mime_type == "application/pdf" and content_bytes:
        pdf_texts = _extract_pdf_text(content_bytes)
        if pdf_texts:
            for page_num, text in enumerate(pdf_texts, start=1):
                await db.save_ocr_page(doc.id, page_num, text, "pymupdf-native")
            return pdf_texts

    # 3. Fall back to Vision OCR for scanned docs / images
    images = [item for item in content_items if isinstance(item, Image)]
    if not images:
        return []

    texts = []
    for page_num, image in enumerate(images, start=1):
        resized = _resize_image_if_needed(image)
        text = extract_text_from_image(resized)
        await db.save_ocr_page(doc.id, page_num, text, OCR_MODEL)
        texts.append(text)

    return texts


async def _check_baseline_labs(db: Database) -> str | None:
    """Check if pre-treatment baseline labs exist. Returns warning if missing."""
    from oncofiles.models import TreatmentEventQuery

    events = await db.list_treatment_events(TreatmentEventQuery(event_type="chemo", limit=1))
    if not events:
        return None

    # Get earliest chemo event date
    all_chemo = await db.list_treatment_events(TreatmentEventQuery(event_type="chemo", limit=200))
    if not all_chemo:
        return None

    earliest = min(e.event_date for e in all_chemo)
    baseline_labs = await db.get_labs_before_date(earliest.isoformat())
    if not baseline_labs:
        return (
            f"**WARNING: BASELINE LABS MISSING** — No pre-treatment lab results found "
            f"before first chemo cycle ({earliest.isoformat()}). Baseline values are "
            f"essential for trend analysis and toxicity grading."
        )
    return None
