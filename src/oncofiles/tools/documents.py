"""Document management tools."""

from __future__ import annotations

import asyncio
import io
import json
import logging

from fastmcp import Context

from oncofiles.filename_parser import parse_filename
from oncofiles.gdrive_folders import (
    ensure_folder_structure,
    ensure_year_month_folder,
    resolve_category_folder,
)
from oncofiles.models import Document, DocumentCategory, SearchQuery
from oncofiles.tools._helpers import (
    _clamp_limit,
    _doc_to_dict,
    _gdrive_url,
    _get_db,
    _get_files,
    _get_gdrive,
    _get_patient_id,
    _parse_date,
    _resolve_patient_id,
)

logger = logging.getLogger(__name__)

MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB


async def _get_doc_owned(db, doc_id: int, pid: str | None = None) -> Document | None:
    """Fetch a document by integer ID with patient ownership check.

    Returns None if the document doesn't exist or belongs to a different patient.
    This prevents cross-patient data leaks via enumerable integer IDs.

    ``pid`` may be passed explicitly by callers that already resolved it via
    ``_resolve_patient_id`` (Option A, #429). When omitted, falls back to the
    middleware-resolved ContextVar — preserves stdio + single-patient bearer
    flows.
    """
    if pid is None:
        pid = _get_patient_id()
    if not await db.check_document_ownership(doc_id, pid):
        return None
    return await db.get_document(doc_id)


async def upload_document(
    ctx: Context,
    content: str,
    filename: str,
    mime_type: str = "application/pdf",
    patient_slug: str | None = None,
) -> str:
    """Upload a medical document to persistent storage.

    The filename should follow the standard convention:
    YYYYMMDD_PatientName_Institution_Category_DescriptionEN.ext
    (e.g. 20260227_PatientName_NOU_Labs_BloodResultsBeforeCycle2.pdf)

    Legacy formats (space+dash, underscore-separated) are also accepted and
    auto-parsed. Separators: underscores only. Description: English, CamelCase.

    Args:
        content: Base64-encoded file content.
        filename: Document filename in standard or legacy format.
        mime_type: MIME type of the document.
        patient_slug: Optional — explicit patient slug (#429).
    """
    import base64
    import binascii

    if len(content) > MAX_UPLOAD_SIZE * 4 // 3 + 4:
        return json.dumps({"error": "File exceeds 100 MB limit."})

    try:
        file_bytes = base64.b64decode(content)
    except binascii.Error:
        return json.dumps({"error": "Invalid base64 content. Ensure the file is properly encoded."})

    if len(file_bytes) > MAX_UPLOAD_SIZE:
        return json.dumps({"error": "File exceeds 100 MB limit."})

    db = _get_db(ctx)
    files = _get_files(ctx)
    patient_id = await _resolve_patient_id(patient_slug, ctx)

    # FUP: check document limit per patient
    from oncofiles.config import MAX_DOCUMENTS_PER_PATIENT

    doc_count = await db.count_documents(patient_id=patient_id)
    if doc_count >= MAX_DOCUMENTS_PER_PATIENT:
        return json.dumps(
            {
                "error": f"Document limit reached ({MAX_DOCUMENTS_PER_PATIENT} files). "
                "Contact support to increase your limit.",
                "current_count": doc_count,
                "limit": MAX_DOCUMENTS_PER_PATIENT,
            }
        )

    # Upload to Files API
    await ctx.info(f"Uploading {filename} ({len(file_bytes)} bytes)...")
    try:
        metadata = files.upload(io.BytesIO(file_bytes), filename, mime_type)
    except Exception:
        logger.exception("Files API upload failed for %s", filename)
        return json.dumps({"error": "Files API upload failed. Check server logs."})

    # Parse filename for structured metadata
    parsed = parse_filename(filename)

    # Check for existing active document with the same filename (re-upload / new version)
    existing = await db.get_active_document_by_filename(filename, patient_id=patient_id)
    version = 1
    previous_version_id = None
    if existing:
        version = existing.version + 1
        previous_version_id = existing.id
        await db.delete_document(existing.id)
        await ctx.info(
            f"Superseded version {existing.version} (doc #{existing.id}) → new version {version}"
        )

    doc = Document(
        file_id=metadata.id,
        filename=filename,
        original_filename=filename,
        document_date=parsed.document_date,
        institution=parsed.institution,
        category=parsed.category,
        description=parsed.description,
        mime_type=metadata.mime_type,
        size_bytes=metadata.size_bytes,
        version=version,
        previous_version_id=previous_version_id,
    )

    doc = await db.insert_document(doc, patient_id=patient_id)

    # Notify oncoteam of new document (fire-and-forget)
    from oncofiles.webhook import notify_oncoteam

    notify_oncoteam(doc.id, doc.filename, doc.category.value)

    # Auto-sync to GDrive if available
    gdrive_id = None
    gdrive = await _get_gdrive(ctx)
    if gdrive:
        try:
            folder_id = ctx.request_context.lifespan_context.get("gdrive_folder_id")
            if folder_id:
                folder_map = await asyncio.to_thread(ensure_folder_structure, gdrive, folder_id)
                cat_folder = resolve_category_folder(folder_map, doc.category.value, folder_id)
                target_folder = cat_folder
                if doc.document_date:
                    date_str = doc.document_date.isoformat()
                    target_folder = await asyncio.to_thread(
                        ensure_year_month_folder, gdrive, cat_folder, date_str
                    )
                uploaded = await asyncio.to_thread(
                    gdrive.upload,
                    filename=doc.filename,
                    content_bytes=file_bytes,
                    mime_type=doc.mime_type,
                    folder_id=target_folder,
                    app_properties={"oncofiles_id": str(doc.id)},
                )
                gdrive_id = uploaded["id"]
                modified_time = uploaded.get("modifiedTime", "")
                await db.update_gdrive_id(doc.id, gdrive_id, modified_time)
        except Exception:
            logger.warning(
                "upload_document: GDrive auto-sync failed for %s — continuing",
                doc.filename,
                exc_info=True,
            )

    result = {
        "id": doc.id,
        "file_id": doc.file_id,
        "filename": doc.filename,
        "document_date": doc.document_date.isoformat() if doc.document_date else None,
        "institution": doc.institution,
        "category": doc.category.value,
        "version": doc.version,
        "gdrive_url": _gdrive_url(gdrive_id),
    }
    if doc.previous_version_id:
        result["previous_version_id"] = doc.previous_version_id
    return json.dumps(result)


async def list_documents(
    ctx: Context,
    limit: int = 50,
    offset: int = 0,
    patient_slug: str | None = None,
) -> str:
    """List all stored medical documents with metadata.

    Returns documents ordered by date (newest first).

    Args:
        limit: Maximum results to return.
        offset: Skip this many results (for pagination).
        patient_slug: Optional — explicit patient slug (e.g. 'q1b'). Required
            in stateless HTTP contexts (Claude.ai connector, ChatGPT) where
            select_patient() state does not persist across tool calls (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    docs = await db.list_documents(limit=limit, offset=offset, patient_id=pid)
    return json.dumps({"documents": [_doc_to_dict(d) for d in docs], "total": len(docs)})


async def search_documents(
    ctx: Context,
    text: str | None = None,
    institution: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
    patient_slug: str | None = None,
) -> str:
    """Search medical documents by text, institution, category, or date range.

    Multi-term queries (e.g. "CEA labs") use AND semantics — all terms must
    match somewhere. Results are ranked by relevance when text is provided:
    filename/description matches rank highest, then AI summaries, then tags.

    Args:
        text: Search query (searches filename, institution, description,
              AI summary, tags, and structured metadata). Multiple words
              are AND-ed together.
        institution: Filter by institution code (e.g. NOUonko, OUSA).
        category: Filter by category (labs, report, imaging,
                  pathology, genetics, surgery, surgical_report, prescription,
                  referral, discharge, discharge_summary, chemo_sheet,
                  vaccination, dental, preventive, other).
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        limit: Maximum results to return (max 200).
        offset: Skip this many results (for pagination).
    """
    from oncofiles.memory import query_slot

    try:
        db = _get_db(ctx)
        query = SearchQuery(
            text=text,
            institution=institution,
            category=DocumentCategory(category) if category else None,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            limit=_clamp_limit(limit),
            offset=max(0, offset),
        )
        pid = await _resolve_patient_id(patient_slug, ctx)
        async with query_slot("search_documents"):
            docs = await db.search_documents(query, patient_id=pid)
        from oncofiles.memory import update_peak_rss

        update_peak_rss()
        return json.dumps({"documents": [_doc_to_dict(d) for d in docs], "total": len(docs)})
    except ValueError as e:
        return json.dumps({"error": str(e)})


async def get_document(ctx: Context, file_id: str, patient_slug: str | None = None) -> str:
    """Get a document's metadata and file_id for Claude to analyze.

    Returns the file_id that can be used to reference the document in conversation.

    Args:
        file_id: The Anthropic Files API file_id.
        patient_slug: Optional — explicit patient slug. Required in stateless
            HTTP contexts (Claude.ai connector, ChatGPT). See #429.
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    doc = await db.get_document_by_file_id(file_id, patient_id=pid)
    if not doc:
        return json.dumps({"error": f"Document not found: {file_id}"})
    return json.dumps(
        {
            "id": doc.id,
            "file_id": doc.file_id,
            "filename": doc.filename,
            "document_date": doc.document_date.isoformat() if doc.document_date else None,
            "institution": doc.institution,
            "category": doc.category.value,
            "description": doc.description,
            "mime_type": doc.mime_type,
            "size_bytes": doc.size_bytes,
            "gdrive_url": _gdrive_url(doc.gdrive_id),
            "content_block": doc.content_block,
        }
    )


async def get_document_by_id(ctx: Context, doc_id: int, patient_slug: str | None = None) -> str:
    """Get a document's metadata by its integer database ID.

    Use this when you have the numeric document ID (e.g. from search results or lab values).

    Args:
        doc_id: The integer database ID of the document.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    doc = await _get_doc_owned(db, doc_id, pid=pid)
    if not doc:
        return json.dumps({"error": f"Document not found: {doc_id}"})
    return json.dumps(
        {
            "id": doc.id,
            "file_id": doc.file_id,
            "filename": doc.filename,
            "document_date": doc.document_date.isoformat() if doc.document_date else None,
            "institution": doc.institution,
            "category": doc.category.value,
            "description": doc.description,
            "mime_type": doc.mime_type,
            "size_bytes": doc.size_bytes,
            "gdrive_url": _gdrive_url(doc.gdrive_id),
            "content_block": doc.content_block,
        }
    )


async def delete_document(ctx: Context, file_id: str, patient_slug: str | None = None) -> str:
    """Soft-delete a document (moves to trash, recoverable for 30 days).

    The document is hidden from all listings and searches but can be restored
    using restore_document. Files API copy is also deleted.

    Args:
        file_id: The Anthropic Files API file_id to delete.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    files = _get_files(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)

    # Delete from Files API
    try:
        files.delete(file_id)
    except Exception as e:
        await ctx.warning(f"Files API deletion failed (may already be deleted): {e}")

    # Soft-delete in local database
    deleted = await db.delete_document_by_file_id(file_id, patient_id=pid)
    return json.dumps(
        {
            "deleted": deleted,
            "file_id": file_id,
            "message": "Moved to trash. Recoverable for 30 days via restore_document."
            if deleted
            else "Document not found or already deleted.",
        }
    )


async def restore_document(ctx: Context, doc_id: int, patient_slug: str | None = None) -> str:
    """Restore a soft-deleted document from trash.

    Args:
        doc_id: The local document ID to restore.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    # Ownership check: verify doc belongs to current patient (even if deleted)
    pid = await _resolve_patient_id(patient_slug, ctx)
    if not await db.check_document_ownership(doc_id, pid):
        return json.dumps(
            {"restored": False, "doc_id": doc_id, "message": "Document not found in trash."}
        )
    doc = await db.get_document(doc_id)
    restored = await db.restore_document(doc_id)
    if restored:
        return json.dumps(
            {
                "restored": True,
                "doc_id": doc_id,
                "filename": doc.filename,
                "message": "Document restored from trash.",
            }
        )
    return json.dumps(
        {
            "restored": False,
            "doc_id": doc_id,
            "message": "Document not found in trash.",
        }
    )


async def list_trash(ctx: Context, limit: int = 50, patient_slug: str | None = None) -> str:
    """List soft-deleted documents in trash.

    Args:
        limit: Maximum results to return (default 50, max 200).
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    limit = min(max(limit, 1), 200)
    pid = await _resolve_patient_id(patient_slug, ctx)
    docs = await db.list_trash(limit=limit, patient_id=pid)
    return json.dumps(
        {
            "trash": [
                {
                    "id": d.id,
                    "file_id": d.file_id,
                    "filename": d.filename,
                    "category": d.category.value,
                    "deleted_at": d.deleted_at.isoformat() if d.deleted_at else None,
                }
                for d in docs
            ],
            "total": len(docs),
        }
    )


async def find_duplicates(ctx: Context, patient_slug: str | None = None) -> str:
    """Detect potential duplicate documents based on original filename and file size.

    Returns groups of documents that share the same original_filename + size_bytes.
    Each group contains 2+ documents. Useful for cleanup after repeated imports.

    Args:
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    groups = await db.find_duplicates(patient_id=pid)
    result = []
    for group in groups:
        result.append(
            {
                "original_filename": group[0].original_filename,
                "size_bytes": group[0].size_bytes,
                "count": len(group),
                "documents": [
                    {
                        "id": d.id,
                        "file_id": d.file_id,
                        "filename": d.filename,
                        "document_date": d.document_date.isoformat() if d.document_date else None,
                        "gdrive_url": _gdrive_url(d.gdrive_id),
                        "created_at": d.created_at.isoformat() if d.created_at else None,
                    }
                    for d in group
                ],
            }
        )
    return json.dumps({"duplicate_groups": result, "total_groups": len(result)})


async def get_document_versions(ctx: Context, doc_id: int, patient_slug: str | None = None) -> str:
    """Get the version history chain for a document.

    Returns all versions (current and previous) ordered newest first.
    Works with any document ID in the chain — will find the full history.

    Args:
        doc_id: The integer database ID of any document in the version chain.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    doc = await _get_doc_owned(db, doc_id, pid=pid)
    if not doc:
        return json.dumps({"error": f"Document not found: {doc_id}"})

    chain = await db.get_document_version_chain(doc_id)
    return json.dumps(
        {
            "versions": [
                {
                    "id": d.id,
                    "file_id": d.file_id,
                    "filename": d.filename,
                    "version": d.version,
                    "previous_version_id": d.previous_version_id,
                    "document_date": d.document_date.isoformat() if d.document_date else None,
                    "size_bytes": d.size_bytes,
                    "gdrive_url": _gdrive_url(d.gdrive_id),
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                    "deleted_at": d.deleted_at.isoformat() if d.deleted_at else None,
                }
                for d in chain
            ],
            "total_versions": len(chain),
        }
    )


async def get_related_documents(ctx: Context, doc_id: int, patient_slug: str | None = None) -> str:
    """Get documents cross-referenced with the given document.

    Returns related documents found by shared visit dates, diagnoses,
    or explicit references. Each result includes the relationship type
    and a confidence score.

    Args:
        doc_id: The integer database ID of the document.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    doc = await _get_doc_owned(db, doc_id, pid=pid)
    if not doc:
        return json.dumps({"error": f"Document not found: {doc_id}"})

    refs = await db.get_cross_references(doc_id)

    # Batch-fetch all related documents in a single query
    related_ids = set()
    for ref in refs:
        is_source = ref["source_document_id"] == doc_id
        related_ids.add(ref["target_document_id"] if is_source else ref["source_document_id"])
    related_docs = await db.get_documents_by_ids(related_ids)

    items = []
    for ref in refs:
        is_source = ref["source_document_id"] == doc_id
        related_id = ref["target_document_id"] if is_source else ref["source_document_id"]
        related = related_docs.get(related_id)
        if not related or related.deleted_at:
            continue
        items.append(
            {
                "id": related.id,
                "file_id": related.file_id,
                "filename": related.filename,
                "document_date": related.document_date.isoformat()
                if related.document_date
                else None,
                "category": related.category.value,
                "institution": related.institution,
                "gdrive_url": _gdrive_url(related.gdrive_id),
                "relationship": ref["relationship"],
                "confidence": ref["confidence"],
            }
        )
    return json.dumps({"document_id": doc_id, "related": items, "total": len(items)})


async def get_document_group(ctx: Context, group_id: str, patient_slug: str | None = None) -> str:
    """Get all documents in a logical group (split siblings or consolidated parts).

    Documents that were split from a multi-document PDF or consolidated from
    multiple files share a group_id. Returns all parts ordered by part_number.

    Args:
        group_id: The UUID group identifier.
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    all_docs = await db.get_documents_by_group(group_id)
    # Filter to only documents owned by the current patient
    pid = await _resolve_patient_id(patient_slug, ctx)
    docs = []
    for d in all_docs:
        if d.id and await db.check_document_ownership(d.id, pid):
            docs.append(d)
    if not docs:
        return json.dumps({"error": f"No documents found for group: {group_id}"})
    return json.dumps(
        {
            "group_id": group_id,
            "total_parts": docs[0].total_parts,
            "documents": [_doc_to_dict(d) for d in docs],
        }
    )


async def update_document_category(
    ctx: Context, doc_id: int, category: str, patient_slug: str | None = None
) -> str:
    """Update the category of a document.

    Use this to recategorize documents (e.g. from 'other' to 'reference').

    Args:
        doc_id: The integer database ID of the document.
        category: New category (labs, report, imaging,
                  pathology, genetics, surgery, surgical_report, prescription,
                  referral, discharge, discharge_summary, chemo_sheet,
                  vaccination, dental, preventive,
                  reference, advocate, other).
        patient_slug: Optional — explicit patient slug (#429).
    """
    try:
        valid_category = DocumentCategory(category)
    except ValueError:
        valid_values = [c.value for c in DocumentCategory]
        return json.dumps({"error": f"Invalid category '{category}'. Valid: {valid_values}"})

    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    doc = await _get_doc_owned(db, doc_id, pid=pid)
    if not doc:
        return json.dumps({"error": f"Document not found: {doc_id}"})

    old_category = doc.category.value
    await db.update_document_category(doc_id, valid_category.value)

    # Immediately move file in GDrive to match new category
    gdrive_moved = False
    gdrive = await _get_gdrive(ctx)
    if gdrive and doc.gdrive_id and old_category != valid_category.value:
        try:
            folder_id = ctx.request_context.lifespan_context.get("gdrive_folder_id", "")
            if folder_id:
                # Refresh doc with new category for folder path calculation
                updated_doc = await db.get_document(doc_id)
                if updated_doc:
                    from oncofiles.tools.hygiene import _move_doc_to_correct_folder

                    await _move_doc_to_correct_folder(
                        gdrive,
                        updated_doc,
                        valid_category.value,
                        folder_id,
                        patient_id=pid,
                    )
                    gdrive_moved = True
        except Exception:
            logger.warning(
                "Category changed but GDrive move failed for %s", doc.filename, exc_info=True
            )

    return json.dumps(
        {
            "id": doc_id,
            "old_category": old_category,
            "new_category": valid_category.value,
            "filename": doc.filename,
            "gdrive_moved": gdrive_moved,
        }
    )


async def reassign_document(ctx: Context, doc_id: int, target_patient_slug: str) -> str:
    """Reassign a document to a different patient (admin data repair tool).

    Use this when a document was imported under the wrong patient during sync.
    Updates patient_id on the document record. Enforced admin-only (#487):
    requires either the static MCP_BEARER_TOKEN caller or an OAuth caller
    whose Google email is in DASHBOARD_ADMIN_EMAILS.

    Args:
        doc_id: The integer database ID of the document to reassign.
        target_patient_slug: Target patient slug or UUID (e.g. 'nora-antalova').
    """
    from oncofiles.tools._helpers import _require_admin_or_raise

    try:
        _require_admin_or_raise("reassign_document")
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    db = _get_db(ctx)

    # Verify document exists
    doc = await db.get_document(doc_id)
    if not doc:
        return json.dumps({"error": f"Document not found: {doc_id}"})

    # Resolve target patient
    target_pid = await db.resolve_patient_id(target_patient_slug)
    if not target_pid:
        return json.dumps({"error": f"Patient not found: {target_patient_slug}"})

    # Get current patient for logging
    old_pid_row = await db.db.execute("SELECT patient_id FROM documents WHERE id = ?", (doc_id,))
    old_row = await old_pid_row.fetchone()
    old_pid = old_row["patient_id"] if old_row else "unknown"

    if old_pid == target_pid:
        return json.dumps({"error": "Document already belongs to this patient"})

    # Reassign
    await db.db.execute(
        "UPDATE documents SET patient_id = ? WHERE id = ?",
        (target_pid, doc_id),
    )
    await db.db.commit()

    logger.info(
        "reassign_document: doc %d (%s) moved from %s to %s",
        doc_id,
        doc.filename,
        old_pid[:8],
        target_pid[:8],
    )

    return json.dumps(
        {
            "reassigned": True,
            "doc_id": doc_id,
            "filename": doc.filename,
            "from_patient": old_pid,
            "to_patient": target_pid,
        }
    )


async def audit_patient_isolation(ctx: Context) -> str:
    """Scan all patients for cross-patient filename contamination.

    Admin-only (#487): enumerates every patient's slug + display_name; must
    not be callable by patient-scoped tokens. Enforcement added after the
    2026-04-24 audit found the docstring already claimed admin-sounding
    scope while the code had zero check.

    Checks every document's filename against all patients' display names.
    Reports any document whose filename contains a DIFFERENT patient's name.
    Generic — works for any number of patients, no hardcoded names.
    """
    from oncofiles.tools._helpers import _require_admin_or_raise

    try:
        _require_admin_or_raise("audit_patient_isolation")
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    db = _get_db(ctx)
    patients = await db.list_patients(active_only=True)

    # Build name→patient mapping (compact names without spaces)
    name_to_patient: dict[str, str] = {}
    for p in patients:
        compact = p.display_name.replace(" ", "")
        if compact and len(compact) > 3:  # skip very short names
            name_to_patient[compact] = p.patient_id

    mismatches: list[dict] = []

    for p in patients:
        docs = await db.list_documents(limit=500, patient_id=p.patient_id)

        for doc in docs:
            if doc.deleted_at:
                continue
            for name, owner_pid in name_to_patient.items():
                if owner_pid == p.patient_id:
                    continue  # skip own name
                if name in doc.filename:
                    mismatches.append(
                        {
                            "doc_id": doc.id,
                            "filename": doc.filename,
                            "assigned_patient": p.slug,
                            "detected_name": name,
                            "name_belongs_to": next(
                                (pt.slug for pt in patients if pt.patient_id == owner_pid), "?"
                            ),
                        }
                    )

    return json.dumps(
        {
            "patients_scanned": len(patients),
            "mismatches": mismatches,
            "total_mismatches": len(mismatches),
            "clean": len(mismatches) == 0,
        }
    )


def register(mcp):
    mcp.tool()(upload_document)
    mcp.tool()(list_documents)
    mcp.tool()(search_documents)
    mcp.tool()(get_document)
    mcp.tool()(get_document_by_id)
    mcp.tool()(delete_document)
    mcp.tool()(restore_document)
    mcp.tool()(list_trash)
    mcp.tool()(find_duplicates)
    mcp.tool()(get_document_versions)
    mcp.tool()(get_related_documents)
    mcp.tool()(get_document_group)
    mcp.tool()(update_document_category)
    mcp.tool()(reassign_document)
    mcp.tool()(audit_patient_isolation)
