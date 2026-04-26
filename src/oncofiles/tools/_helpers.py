"""Shared helper functions for tool modules."""

from __future__ import annotations

import asyncio
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


# ── Clinical-response disclaimer (#400) ───────────────────────────────────────
#
# Oncofiles is an information tool for patients and caregivers. It is NOT an
# oncologist and does not diagnose or recommend treatment. Every clinical tool
# that returns lab values, safety flags, checklists, or trial eligibility MUST
# carry this disclaimer so chat clients and dashboards have a traceable "verify
# with your physician" signal next to the data.

CLINICAL_DISCLAIMER_SK = (
    "Informatívny nástroj pre pacienta a opatrovateľa. Nenahrádza onkológa. "
    "Pred akýmkoľvek rozhodnutím o liečbe overte u ošetrujúceho lekára."
)
CLINICAL_DISCLAIMER_EN = (
    "Informational tool for the patient and caregiver. Does not replace your "
    "oncologist. Verify with your treating physician before any treatment decision."
)


def _with_clinical_disclaimer(payload: dict | list) -> dict:
    """Wrap a clinical tool response with the standard disclaimer.

    For dict payloads the disclaimer is merged in-place; for list payloads the
    list is nested under ``data`` so the disclaimer sits alongside it.
    """
    if isinstance(payload, list):
        return {
            "data": payload,
            "disclaimer": {"sk": CLINICAL_DISCLAIMER_SK, "en": CLINICAL_DISCLAIMER_EN},
        }
    out = dict(payload)
    out["disclaimer"] = {"sk": CLINICAL_DISCLAIMER_SK, "en": CLINICAL_DISCLAIMER_EN}
    return out


# ── Patient context (delegated to patient_context module) ────────────────────


# Backward-compatible alias — lazy to avoid capturing stale import-time state
def PATIENT_CONTEXT() -> dict:  # noqa: N802
    return patient_context.get_context()


def _patient_context_text() -> str:
    return patient_context.format_context_text()


# ── Context accessors ────────────────────────────────────────────────────────


def _get_patient_id(*, required: bool = True) -> str:
    """Get the current patient_id (set by PatientResolutionMiddleware).

    Args:
        required: If True (default), raises ValueError when no patient is
            selected OR when resolution returned the no-access sentinel.
            Set to False for bootstrapping tools (list_patients,
            select_patient) that must work without a patient.

    Raises:
        ValueError: with a caller-actionable message distinguishing the
            three no-patient states: unauthorized caller (sentinel),
            sentinel-from-unauthorized-OAuth, and never-set.
    """
    from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
    from oncofiles.patient_middleware import get_current_patient_id

    pid = get_current_patient_id()
    if pid == NO_PATIENT_ACCESS_SENTINEL:
        if required:
            # Caller authenticated (OAuth or bearer) but we couldn't bind
            # them to a patient. Distinct message from "no patient selected"
            # so the UX can prompt them to (a) sign in on the dashboard and
            # make sure their Google email matches caregiver_email, OR
            # (b) pass an explicit patient_slug in their tool call if they
            # know their slug.
            raise ValueError(
                "No patient access resolved for your account. "
                "This usually means your Google email does not match the "
                "caregiver_email of any patient, or you are not signed in "
                "on the dashboard yet. "
                "Fix: (1) sign in at https://oncofiles.com/dashboard with "
                "the Google account you want to use, (2) create a patient "
                "or verify your email appears in its caregiver_email, then "
                "(3) in claude.ai reconnect the Oncofiles connector (Settings "
                "→ Connectors → Oncofiles → Remove → Add again). "
                "Alternatively pass patient_slug=<your-slug> in this tool call."
            )
        return ""  # treat sentinel as "no patient" for bootstrapping callers
    if not pid and required:
        raise ValueError(
            "No patient selected. "
            "Use list_patients() to see available patients, "
            "then select_patient(patient_id) to choose one. "
            "If you have no patients yet, create one from the dashboard at https://oncofiles.com/dashboard"
        )
    return pid


async def _resolve_patient_id(
    patient_slug: str | None,
    ctx: Context,
    *,
    required: bool = True,
) -> str:
    """Resolve patient identity for a tool call (Option A per #429) with ACL gate (#497/#498).

    Stateless-HTTP safe: every patient-scoped tool should accept a
    `patient_slug` parameter and resolve via this helper. Falls back to the
    middleware-resolved current patient when slug is omitted (preserves
    backwards-compat for bearer-token flows where the token already binds
    a specific patient).

    ACL gate (added 2026-04-26 for #497/#498). Before this gate, a caller
    bearer-bound to patient X could pass `patient_slug=Y` and the resolver
    would happily return Y's patient_id — every patient-scoped tool then
    operated under Y's scope, bypassing the entire isolation layer. The fix:

    - **Admin callers** (static `MCP_BEARER_TOKEN`, OAuth caller whose Google
      email is in `DASHBOARD_ADMIN_EMAILS`) bypass the ACL — they can resolve
      any slug.
    - **Non-admin callers** must satisfy ONE of:
        * their bearer-bound `patient_id` (set by `verify_token` /
          `PatientResolutionMiddleware`) equals the resolved `patient_id`, OR
        * their OAuth-bound `caregiver_email` equals the patient's
          `caregiver_email` (case-insensitive).
      Otherwise the resolver raises `ValueError("access denied …")`. The
      message does not echo the patient's owning email or any other PHI —
      that would be a secondary leak.

    Stdio dev note: stdio callers default to non-admin and the middleware
    binds them to the first active patient. Multi-patient stdio dev that
    needs cross-patient access should set `_verified_caller_is_admin=True`
    in the dev session OR use the static `MCP_BEARER_TOKEN` (which is admin).

    Args:
        patient_slug: Explicit slug from the caller (e.g. 'q1b'). Preferred.
        ctx: FastMCP request context.
        required: If True (default), raises ValueError when neither slug nor
            middleware-resolved patient is available.
    """
    if not patient_slug:
        return _get_patient_id(required=required)

    db = _get_db(ctx)
    patient = await db.get_patient_by_slug(patient_slug)
    if not patient:
        raise ValueError(
            f"Patient not found: {patient_slug!r}. Use list_patients() to see available slugs."
        )
    pid = patient.patient_id

    if _is_admin_caller():
        return pid

    from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
    from oncofiles.patient_middleware import get_current_patient_id

    bound_pid = get_current_patient_id()
    bearer_match = bool(bound_pid) and bound_pid != NO_PATIENT_ACCESS_SENTINEL and bound_pid == pid

    caller_email = _caller_email().strip().lower()
    pat_caregiver = (patient.caregiver_email or "").strip().lower()
    caregiver_match = bool(caller_email) and caller_email == pat_caregiver

    if bearer_match or caregiver_match:
        return pid

    raise ValueError(
        f"access denied for patient_slug={patient_slug!r}. "
        "Your bearer token or OAuth identity is not authorized for that patient. "
        "Use list_patients() to see patients available to you."
    )


# ── Admin scope & ownership (#487 / v5.15 Phase 3) ──────────────────────────


def _is_admin_caller() -> bool:
    """Return True if the current request has admin scope.

    Admin is either: (a) the static `MCP_BEARER_TOKEN` caller (operator /
    Oncoteam / dev), or (b) an OAuth caller whose Google email matches
    `DASHBOARD_ADMIN_EMAILS`. `verify_token()` in persistent_oauth.py sets
    the `_verified_caller_is_admin` ContextVar for both cases.

    The ContextVar defaults to False; local stdio invocations (direct
    function calls from tests or stdio transport) need to opt in if they
    want admin behavior. Returning False here for stdio is the safe
    default — tests that need admin should set the ContextVar explicitly.
    """
    try:
        from oncofiles.persistent_oauth import _verified_caller_is_admin

        return bool(_verified_caller_is_admin.get())
    except Exception:
        return False


def _caller_email() -> str:
    """Return the caller's OAuth-bound Google email, or empty string.

    Populated by `verify_token()` for MCP OAuth tokens (#478 email binding);
    empty for static-bearer / patient-bearer / stdio callers.
    """
    try:
        from oncofiles.persistent_oauth import _verified_caller_email

        return _verified_caller_email.get() or ""
    except Exception:
        return ""


def _require_admin_or_raise(tool_name: str) -> None:
    """Raise ValueError if the current caller is not admin.

    Used by admin-only MCP tools per #487. The ValueError surfaces to the
    MCP client as a JSON-encoded error (wrapped by the tool's try/except),
    so callers see a clear reason rather than a silent cross-patient action.
    """
    if not _is_admin_caller():
        raise ValueError(
            f"{tool_name!r} requires admin scope. "
            "This tool can mutate or audit data across patients and is restricted "
            "to the static MCP_BEARER_TOKEN caller or an OAuth caller whose Google "
            "email is in DASHBOARD_ADMIN_EMAILS."
        )


def _check_ownership_or_admin(
    entity_name: str, entity_id: int | str, owner_pid: str | None, caller_pid: str
) -> str | None:
    """Verify the caller owns `entity_id` OR has admin scope.

    Returns None on success, or an error string for the tool to JSON-encode.
    Admin callers bypass the pid check; non-admin callers must match the
    entity's `patient_id` exactly.

    Used by read-by-id MCP tools that take an integer id (get_email,
    get_calendar_event, get_document_by_id, etc.) to close the #487 C1
    cross-patient enumeration finding.
    """
    if _is_admin_caller():
        return None
    if not owner_pid:
        return f"{entity_name} {entity_id}: no patient_id on record; cannot verify ownership."
    if not caller_pid:
        return (
            f"{entity_name} {entity_id}: access denied — no authenticated patient. "
            "Sign in or pass patient_slug."
        )
    if owner_pid != caller_pid:
        # Do NOT echo the real owner's pid — that would be a secondary leak.
        return f"{entity_name} {entity_id}: access denied (not owned by the calling patient)."
    return None


def _safe_error(exc: BaseException, category: str = "internal_error") -> dict[str, str]:
    """Build a sanitized error payload for MCP tool responses (#489).

    Replaces `{"error": str(exc)}` patterns. Raw exception messages can
    contain cross-patient UUIDs (from SQL bind params), internal URL paths,
    or stack-frame fragments — per Felix Vítek's oncoteam#438 Bug 2 analysis
    and our parallel scan.

    Returns a two-key dict: a stable `error` category (safe to match against
    in clients) and the exception class name for debugging. Always logs the
    full exception server-side with stack trace.

    Usage:
        try:
            ...
        except Exception as exc:
            return json.dumps(_safe_error(exc, "lab_fetch_failed"))
    """
    logger.exception("tool error [%s]", category)
    return {"error": category, "kind": type(exc).__name__}


def _get_db(ctx: Context) -> Database:
    return ctx.request_context.lifespan_context["db"]


def _get_files(ctx: Context) -> FilesClient:
    return ctx.request_context.lifespan_context["files"]


async def _get_gdrive(ctx: Context) -> GDriveClient | None:
    """Get GDrive client for the current patient (per-patient isolation)."""
    clients = await _get_patient_clients(ctx)
    if clients:
        return clients[0]
    return ctx.request_context.lifespan_context.get("gdrive")


async def _get_gmail_client(ctx: Context):
    """Get Gmail client for the current patient (per-patient isolation)."""
    clients = await _get_patient_clients(ctx)
    if clients:
        return clients[1]
    return ctx.request_context.lifespan_context.get("gmail_client")


async def _get_calendar_client(ctx: Context):
    """Get Calendar client for the current patient (per-patient isolation)."""
    clients = await _get_patient_clients(ctx)
    if clients:
        return clients[2]
    return ctx.request_context.lifespan_context.get("calendar_client")


async def _get_patient_clients(ctx: Context) -> tuple | None:
    """Load per-patient GDrive/Gmail/Calendar clients via _create_patient_clients."""
    pid = _get_patient_id(required=False)
    if not pid:
        return None
    db = _get_db(ctx)
    # Import here to avoid circular imports
    from oncofiles.server import _create_patient_clients

    return await _create_patient_clients(db, pid)


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
    if d.group_id:
        result["group_id"] = d.group_id
        result["part_number"] = d.part_number
        result["total_parts"] = d.total_parts
    if d.split_source_doc_id:
        result["split_source_doc_id"] = d.split_source_doc_id
    return result


def _doc_header(doc: Document) -> str:
    date_str = doc.document_date.isoformat() if doc.document_date else "unknown"
    return (
        f"**{doc.filename}** | {doc.category.value} | {date_str} | {doc.institution or 'unknown'}"
    )


def _pdf_to_images(content_bytes: bytes) -> list[Image]:
    """Convert PDF pages to JPEG images using pymupdf.

    #426 cleanup: explicitly `del pix` after extracting bytes. A 200-DPI A4
    pixmap is 4-8 MB of native (MuPDF C-heap) memory that Python GC can't
    reclaim until the Python wrapper goes out of scope. Without the explicit
    del, a 10-page PDF view can leave ~50 MB pinned until the surrounding
    function returns, and on a long-running process with many view_document
    calls this accumulates. The nightly pipeline's own fitz path in sync.py
    already does this; this brings the view path to parity.
    """
    import pymupdf

    images: list[Image] = []
    doc = pymupdf.open(stream=content_bytes, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            try:
                jpeg_bytes = pix.tobytes("jpeg")
            finally:
                del pix
            images.append(Image(data=jpeg_bytes, format="jpeg"))
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


async def _try_download(
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
            content_bytes = await asyncio.to_thread(gdrive.download, doc.gdrive_id)
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
        text = extract_text_from_image(resized, db=db, document_id=doc.id)
        await db.save_ocr_page(doc.id, page_num, text, OCR_MODEL)
        texts.append(text)

    return texts


async def _check_baseline_labs(db: Database, patient_id: str | None = None) -> str | None:
    """Check if pre-treatment baseline labs exist. Returns warning if missing."""
    from oncofiles.models import TreatmentEventQuery

    pid = patient_id if patient_id is not None else _get_patient_id()
    events = await db.list_treatment_events(
        TreatmentEventQuery(event_type="chemo", limit=1), patient_id=pid
    )
    if not events:
        return None

    # Get earliest chemo event date
    all_chemo = await db.list_treatment_events(
        TreatmentEventQuery(event_type="chemo", limit=200), patient_id=pid
    )
    if not all_chemo:
        return None

    earliest = min(e.event_date for e in all_chemo)
    baseline_labs = await db.get_labs_before_date(earliest.isoformat(), patient_id=pid)
    if not baseline_labs:
        return (
            f"**WARNING: BASELINE LABS MISSING** — No pre-treatment lab results found "
            f"before first chemo cycle ({earliest.isoformat()}). Baseline values are "
            f"essential for trend analysis and toxicity grading."
        )
    return None
