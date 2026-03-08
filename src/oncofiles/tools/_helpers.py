"""Shared helper functions for tool modules."""

from __future__ import annotations

import json
import logging
from datetime import date

from fastmcp import Context
from fastmcp.utilities.types import Image

from oncofiles.database import Database
from oncofiles.files_api import FilesClient
from oncofiles.gdrive_client import GDriveClient
from oncofiles.models import Document
from oncofiles.ocr import OCR_MODEL, extract_text_from_image

logger = logging.getLogger(__name__)

# ── Patient context ──────────────────────────────────────────────────────────

PATIENT_CONTEXT = {
    "name": "Erika Fusekova",
    "diagnosis": "AdenoCa colon sigmoideum, G3, mCRC (C18.7)",
    "staging": "IV (liver mets, peritoneal carcinomatosis, LN, Krukenberg tumor l.dx.)",
    "histology": "Adenocarcinoma Grade 3",
    "tumor_site": "Sigmoid colon (left-sided)",
    "diagnosis_date": "2025-12-01",
    "biomarkers": {
        "KRAS": "mutant G12S (c.34G>A, p.(Gly12Ser))",
        "KRAS_G12C": False,
        "NRAS": "wild-type",
        "BRAF_V600E": "wild-type",
        "HER2": "negative (FISH ratio 1.3, avg copy 3)",
        "MSI": "pMMR / MSS",
        "anti_EGFR_eligible": False,
    },
    "treatment": {
        "regimen": "mFOLFOX6 90%",
        "current_cycle": 2,
        "institution": "NOU (Narodny onkologicky ustav), Bratislava",
    },
    "metastases": [
        "liver ([CODE_REDACTED])",
        "peritoneum (C78.6)",
        "retroperitoneum",
        "Krukenberg (ovary l.dx., C79.6)",
        "mediastinal LN",
        "hilar LN",
        "retrocrural LN",
        "portal LN (C77.8)",
        "pulmonary nodules (<=5mm, monitor)",
    ],
    "comorbidities": ["[CLINICAL_REDACTED] (active, [MEDICATION_REDACTED] 0.6ml SC 2x/day)"],
    "surgeries": [
        {
            "date": "2026-01-18",
            "institution": "Bory Nemocnica",
            "type": "palliative resection",
            "result": "AdenoCa G3",
        }
    ],
    "physicians": {
        "treating": "MUDr. Stefan Porsok, PhD., MPH — primar OKO G, NOU Bratislava",
        "admitting": "MUDr. Natalia Pazderova — NOU Bratislava",
    },
    "excluded_therapies": [
        "anti-EGFR (cetuximab, panitumumab) — [BIOMARKER_REDACTED]",
        "checkpoint monotherapy (pembrolizumab, nivolumab) — [BIOMARKER_REDACTED]",
        "HER2-targeted (trastuzumab, pertuzumab) — [BIOMARKER_REDACTED]",
        "BRAF inhibitors (encorafenib) — BRAF wild-type",
        "KRAS G12C-specific (sotorasib, adagrasib) — patient has G12S, not G12C",
    ],
    "note": (
        "Lab values should be interpreted considering active chemotherapy. "
        "Key markers: CEA, CA 19-9, liver (ALT, AST, bilirubin), "
        "renal (creatinine, urea), blood counts (WBC, neutrophils, Hb, platelets). "
        "[CLINICAL_REDACTED] — bevacizumab is HIGH RISK."
    ),
}


def _patient_context_text() -> str:
    bio = PATIENT_CONTEXT["biomarkers"]
    biomarkers = "\n".join(f"  - {k}: {v}" for k, v in bio.items())
    mets = ", ".join(PATIENT_CONTEXT["metastases"])
    comorb = ", ".join(PATIENT_CONTEXT["comorbidities"])
    excluded = "\n".join(f"  - {t}" for t in PATIENT_CONTEXT["excluded_therapies"])
    tx = PATIENT_CONTEXT["treatment"]
    phys = PATIENT_CONTEXT["physicians"]
    return (
        f"**Patient:** {PATIENT_CONTEXT['name']}\n"
        f"**Diagnosis:** {PATIENT_CONTEXT['diagnosis']}\n"
        f"**Staging:** {PATIENT_CONTEXT['staging']}\n"
        f"**Histology:** {PATIENT_CONTEXT['histology']}\n"
        f"**Tumor site:** {PATIENT_CONTEXT['tumor_site']}\n"
        f"**Biomarkers:**\n{biomarkers}\n"
        f"**Treatment:** {tx['regimen']} (cycle {tx['current_cycle']}) at {tx['institution']}\n"
        f"**Metastases:** {mets}\n"
        f"**Comorbidities:** {comorb}\n"
        f"**Physicians:** {phys['treating']}; {phys['admitting']}\n"
        f"**Excluded therapies:**\n{excluded}\n"
        f"**Note:** {PATIENT_CONTEXT['note']}"
    )


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
    }
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
