"""Document splitting engine — creates N documents from a multi-document PDF."""

from __future__ import annotations

import io
import logging
from datetime import date, datetime
from uuid import uuid4

from oncofiles.config import MAX_DOCUMENTS_PER_PATIENT
from oncofiles.database import Database
from oncofiles.filename_parser import CATEGORY_FILENAME_TOKENS
from oncofiles.files_api import FilesClient
from oncofiles.gdrive_client import GDriveClient
from oncofiles.gdrive_folders import ensure_year_month_folder, resolve_category_folder
from oncofiles.models import Document, DocumentCategory
from oncofiles.patient_context import get_patient_name

logger = logging.getLogger(__name__)

# Category mappings from AI detection labels to DocumentCategory
_CATEGORY_MAP = {cat.value: cat for cat in DocumentCategory}


def _parse_date(date_str: str | None) -> date | None:
    """Parse a date string from AI output, returning None on failure."""
    if not date_str:
        return None
    try:
        return date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None


async def split_document(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient | None,
    doc: Document,
    sub_docs: list[dict],
    *,
    patient_id: str,
    folder_id: str | None = None,
    folder_map: dict[str, str] | None = None,
) -> list[Document]:
    """Split a multi-document PDF into N separate documents.

    Args:
        db: Database instance.
        files: Anthropic Files API client.
        gdrive: Google Drive client (None if not connected).
        doc: The original document to split.
        sub_docs: List of sub-document dicts from AI analysis, each with:
            page_range, document_date, institution, category, description, confidence.
        patient_id: Patient UUID.
        folder_id: Root GDrive folder ID for this patient.
        folder_map: Category → folder ID mapping from GDrive folder structure.

    Returns:
        List of newly created Document objects.
    """
    if len(sub_docs) < 2:
        logger.warning("split_document called with %d sub_docs, skipping", len(sub_docs))
        return []

    # Check FUP headroom
    if MAX_DOCUMENTS_PER_PATIENT > 0:
        count = await db.count_documents(patient_id=patient_id)
        needed = len(sub_docs) - 1  # original gets soft-deleted, N new ones created
        if count + needed > MAX_DOCUMENTS_PER_PATIENT:
            logger.warning(
                "Split would exceed document limit (%d + %d > %d), skipping doc %d",
                count,
                needed,
                MAX_DOCUMENTS_PER_PATIENT,
                doc.id,
            )
            return []

    group_id = str(uuid4())
    total_parts = len(sub_docs)
    created_docs: list[Document] = []

    # Download content once — needed for Files API re-upload
    content_bytes: bytes | None = None
    if gdrive and doc.gdrive_id:
        try:
            content_bytes = gdrive.download(doc.gdrive_id)
        except Exception:
            logger.warning("Failed to download from GDrive for split, doc %d", doc.id)

    for idx, sub in enumerate(sub_docs, start=1):
        part_number = idx
        sub_date = _parse_date(sub.get("document_date"))
        sub_institution = sub.get("institution")
        sub_category_str = sub.get("category", "other")
        sub_category = _CATEGORY_MAP.get(sub_category_str, DocumentCategory.OTHER)
        sub_description = sub.get("description", "")

        # Build filename directly from the AI-detected sub-document fields.
        # We can't go through `rename_to_standard` here because that helper
        # derives the date from the filename stem — for splits the date comes
        # from AI analysis, not the (usually ambiguous) parent PDF name.
        patient_compact = (get_patient_name(patient_id) or "").replace(" ", "") or "Patient"
        cat_token = CATEGORY_FILENAME_TOKENS.get(sub_category, "Other")
        institution = sub_institution or doc.institution or "Unknown"
        effective_date = sub_date or doc.document_date
        _, _, ext = doc.filename.rpartition(".")
        ext_with_dot = f".{ext}" if ext else ""

        if effective_date:
            date_str = effective_date.strftime("%Y%m%d")
            name_parts = [date_str, patient_compact, institution, cat_token]
            if sub_description:
                name_parts.append(sub_description)
            name_parts.append(f"Part{part_number}of{total_parts}")
            new_filename = f"{'_'.join(name_parts)}{ext_with_dot}"
        else:
            # Undated fallback — keep original stem but tag the part.
            base, _, ext2 = doc.filename.rpartition(".")
            new_filename = f"{base}_Part{part_number}of{total_parts}"
            if ext2:
                new_filename = f"{new_filename}.{ext2}"

        # Upload to Files API. If this fails mid-loop, previously we hit
        # `continue` which silently dropped the part — `total_parts` stayed at
        # the original count, so callers saw "Part N of M" but only (M - k)
        # files ever existed in the DB. Classic #456 symptom. We now abort the
        # whole split on any error and soft-delete the parts we already wrote
        # so the caller can retry cleanly.
        new_file_id = f"split_{doc.file_id}_{part_number}"  # placeholder
        if content_bytes:
            try:
                result = files.upload(
                    io.BytesIO(content_bytes),
                    new_filename,
                    doc.mime_type,
                )
                new_file_id = result.id
            except Exception:
                logger.exception(
                    "split_document: Files API upload failed on part %d/%d of doc %d — "
                    "aborting split (rolling back %d earlier parts)",
                    part_number,
                    total_parts,
                    doc.id,
                    len(created_docs),
                )
                for created in created_docs:
                    try:
                        await db.delete_document(created.id)
                    except Exception:
                        logger.warning("split_document: failed to rollback part %d", created.id)
                return []

        # Copy on GDrive
        new_gdrive_id = None
        new_gdrive_modified = None
        new_gdrive_md5 = None
        if gdrive and doc.gdrive_id and folder_id and folder_map:
            try:
                # Resolve target folder
                cat_folder = resolve_category_folder(folder_map, sub_category_str, folder_id)
                if sub_date:
                    target_folder = ensure_year_month_folder(
                        gdrive, cat_folder, sub_date.isoformat()
                    )
                else:
                    target_folder = cat_folder

                app_props = {"oncofiles_id": "pending"}
                copy_result = gdrive.copy_file(
                    doc.gdrive_id, new_filename, target_folder, app_props
                )
                new_gdrive_id = copy_result.get("id")
                new_gdrive_modified = copy_result.get("modifiedTime")
                new_gdrive_md5 = copy_result.get("md5Checksum")
            except Exception:
                logger.warning(
                    "Failed to copy to GDrive for split part %d of doc %d",
                    part_number,
                    doc.id,
                    exc_info=True,
                )

        # Create DB record
        new_doc = Document(
            file_id=new_file_id,
            filename=new_filename,
            original_filename=doc.original_filename,
            document_date=sub_date or doc.document_date,
            institution=sub_institution or doc.institution,
            category=sub_category,
            description=sub_description or doc.description,
            mime_type=doc.mime_type,
            size_bytes=doc.size_bytes,
            gdrive_id=new_gdrive_id,
            gdrive_modified_time=(
                datetime.fromisoformat(new_gdrive_modified.replace("Z", "+00:00"))
                if new_gdrive_modified
                else None
            ),
            gdrive_md5=new_gdrive_md5,
            sync_state="synced",
            group_id=group_id,
            part_number=part_number,
            total_parts=total_parts,
            split_source_doc_id=doc.id,
        )
        new_doc = await db.insert_document(new_doc, patient_id=patient_id)
        created_docs.append(new_doc)

        # Update appProperties with actual doc ID
        if gdrive and new_gdrive_id and new_doc.id:
            try:
                gdrive.set_app_properties(new_gdrive_id, {"oncofiles_id": str(new_doc.id)})
            except Exception:
                logger.warning("Failed to set appProperties on GDrive copy %s", new_gdrive_id)

        logger.info(
            "Split doc %d → part %d/%d: %s (id=%d, gdrive=%s)",
            doc.id,
            part_number,
            total_parts,
            new_filename,
            new_doc.id,
            new_gdrive_id,
        )

    # Soft-delete the original document
    if created_docs:
        await db.delete_document(doc.id)
        logger.info(
            "Split complete: doc %d → %d parts (group_id=%s)",
            doc.id,
            len(created_docs),
            group_id,
        )

    return created_docs
