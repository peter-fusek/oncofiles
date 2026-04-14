"""Document consolidation engine — groups related multi-file documents."""

from __future__ import annotations

import logging
import re
from uuid import uuid4

from oncofiles.database import Database
from oncofiles.gdrive_client import GDriveClient
from oncofiles.models import Document

logger = logging.getLogger(__name__)


def _add_part_suffix(filename: str, part: int, total: int) -> str:
    """Add Part{N}of{M} suffix to a filename's description portion.

    Standard format: YYYYMMDD_Patient_Institution_Category_Description.ext
    Result: YYYYMMDD_Patient_Institution_Category_DescriptionPart1of3.ext
    """
    # Split off extension
    base, _, ext = filename.rpartition(".")
    if not base:
        base = filename
        ext = ""

    # Remove any existing PartNofM suffix
    base = re.sub(r"_?Part\d+of\d+$", "", base)

    suffix = f"Part{part}of{total}"
    new_base = f"{base}_{suffix}" if base else suffix
    return f"{new_base}.{ext}" if ext else new_base


async def consolidate_documents(
    db: Database,
    gdrive: GDriveClient | None,
    group: dict,
    *,
    patient_id: str,
) -> str | None:
    """Consolidate multiple files into a logical document group.

    Args:
        db: Database instance.
        gdrive: Google Drive client (None if not connected).
        group: Consolidation group from AI analysis with:
            document_ids (list[int]), reasoning (str), confidence (float).
        patient_id: Patient UUID.

    Returns:
        group_id if consolidation succeeded, None otherwise.
    """
    doc_ids = group.get("document_ids", [])
    if len(doc_ids) < 2:
        logger.warning("consolidate_documents called with %d docs, skipping", len(doc_ids))
        return None

    confidence = group.get("confidence", 0.0)
    reasoning = group.get("reasoning", "")

    # Fetch all documents
    docs: list[Document] = []
    for doc_id in doc_ids:
        doc = await db.get_document(doc_id)
        if doc and not doc.deleted_at:
            docs.append(doc)

    if len(docs) < 2:
        logger.warning("Only %d active docs found for consolidation, skipping", len(docs))
        return None

    # Skip if already grouped
    if any(d.group_id for d in docs):
        logger.info("Some docs already grouped, skipping consolidation for %s", doc_ids)
        return None

    group_id = str(uuid4())
    total_parts = len(docs)

    logger.info(
        "Consolidating %d documents into group %s (confidence=%.2f): %s",
        total_parts,
        group_id,
        confidence,
        reasoning,
    )

    for idx, doc in enumerate(docs, start=1):
        part_number = idx

        # Update filename with Part suffix
        new_filename = _add_part_suffix(doc.filename, part_number, total_parts)

        # Update DB record
        await db.db.execute(
            """
            UPDATE documents
            SET group_id = ?, part_number = ?, total_parts = ?, filename = ?
            WHERE id = ?
            """,
            (group_id, part_number, total_parts, new_filename, doc.id),
        )

        # Rename on GDrive
        if gdrive and doc.gdrive_id and new_filename != doc.filename:
            try:
                gdrive.rename_file(doc.gdrive_id, new_filename)
            except Exception:
                logger.warning(
                    "Failed to rename GDrive file %s for consolidation",
                    doc.gdrive_id,
                    exc_info=True,
                )

        logger.info(
            "Consolidated doc %d → part %d/%d: %s → %s",
            doc.id,
            part_number,
            total_parts,
            doc.filename,
            new_filename,
        )

    await db.db.commit()
    logger.info("Consolidation complete: group_id=%s, %d parts", group_id, total_parts)
    return group_id
