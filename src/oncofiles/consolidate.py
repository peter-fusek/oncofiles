"""Document consolidation engine — groups related multi-file documents."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from uuid import uuid4

from oncofiles.database import Database
from oncofiles.gdrive_client import GDriveClient
from oncofiles.models import Document

logger = logging.getLogger(__name__)

# Guardrails against AI over-grouping unrelated documents (#456, #428, part of
# #454). The composition AI sometimes hallucinates that cross-date or
# cross-institution files belong together. Reject any group that fails all of:
#
# 1. AI confidence >= CONSOLIDATE_MIN_CONFIDENCE
# 2. All document_dates within CONSOLIDATE_MAX_DATE_SPAN_DAYS of each other
# 3. Institutions are compatible (all same, OR at most one distinct known code)
#
# These are OR-gated AT the level of "all must pass". We err on the conservative
# side — a rejected real-split can always be retried by the user; a bad
# consolidation silently corrupts filenames + group_ids across the entire
# patient record.
CONSOLIDATE_MIN_CONFIDENCE: float = 0.7
CONSOLIDATE_MAX_DATE_SPAN_DAYS: int = 7


def _dates_within_span(dates: list[date | None], max_span_days: int) -> bool:
    """Return True iff the non-None dates span at most max_span_days.

    All-None or single-date lists trivially pass. Mixed None/real dates pass
    (a missing date can't be proven too-far from anything).
    """
    real = [d for d in dates if d is not None]
    if len(real) < 2:
        return True
    return (max(real) - min(real)) <= timedelta(days=max_span_days)


def _institutions_compatible(institutions: list[str | None]) -> bool:
    """Return True iff institutions are consistent across the group.

    Compatible = at most one distinct non-None institution value. Rationale: a
    single logical document scanned into multiple files would originate from
    one institution. Mixing NOU + BoryNemocnica in a single "group" is the
    hallmark of #428.
    """
    known = {i.strip() for i in institutions if i and i.strip()}
    return len(known) <= 1


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

    confidence = float(group.get("confidence", 0.0) or 0.0)
    reasoning = group.get("reasoning", "")

    # Guardrail 1: AI confidence floor. Haiku composition prompts occasionally
    # emit low-confidence speculative groups — don't persist them.
    if confidence < CONSOLIDATE_MIN_CONFIDENCE:
        logger.warning(
            "consolidate_documents: rejecting group (confidence=%.2f < %.2f): %s",
            confidence,
            CONSOLIDATE_MIN_CONFIDENCE,
            reasoning,
        )
        return None

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

    # Guardrail 2: date proximity. Parts of one logical document share a date
    # (or a very small window). A span >7d across members is almost always the
    # AI grouping unrelated visits (#428 Erika genetics Feb 12 + Mar 15).
    if not _dates_within_span([d.document_date for d in docs], CONSOLIDATE_MAX_DATE_SPAN_DAYS):
        logger.warning(
            "consolidate_documents: rejecting group (dates span >%dd): ids=%s dates=%s",
            CONSOLIDATE_MAX_DATE_SPAN_DAYS,
            [d.id for d in docs],
            [d.document_date.isoformat() if d.document_date else None for d in docs],
        )
        return None

    # Guardrail 3: same institution. Multi-part scans from one encounter share
    # the issuing institution. Bory + NOU in a single "group" = hallucination.
    if not _institutions_compatible([d.institution for d in docs]):
        logger.warning(
            "consolidate_documents: rejecting group (institutions differ): ids=%s insts=%s",
            [d.id for d in docs],
            [d.institution for d in docs],
        )
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
