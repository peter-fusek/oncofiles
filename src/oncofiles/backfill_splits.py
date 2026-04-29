"""Retroactive backfill for document splitting, consolidation, and cross-references."""

from __future__ import annotations

import gc
import logging
from datetime import UTC, datetime, timedelta

from oncofiles.database import Database
from oncofiles.files_api import FilesClient
from oncofiles.gdrive_client import GDriveClient

logger = logging.getLogger(__name__)


def _is_new(created, max_age_hours: int) -> bool:
    """Return True if created_at falls within the last max_age_hours. See #433."""
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created >= datetime.now(UTC) - timedelta(hours=max_age_hours)


async def backfill_multi_document_splits(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient | None,
    *,
    patient_id: str,
    folder_id: str = "",
    folder_map: dict[str, str] | None = None,
    dry_run: bool = True,
    only_new: bool = False,
    max_age_hours: int | None = None,
) -> dict:
    """Scan all documents for multi-document PDFs and split them.

    Args:
        dry_run: If True, only report findings without making changes.
        only_new: If True, only consider docs created within max_age_hours. See #433.
        max_age_hours: Sliding window for only_new (default AI_REPROCESS_MAX_AGE_HOURS).

    Returns stats dict.
    """
    import json as _json

    from oncofiles.doc_analysis import analyze_document_composition
    from oncofiles.models import PromptLogQuery
    from oncofiles.split import split_document

    stats = {
        "scanned": 0,
        "multi_doc": 0,
        "splits_created": 0,
        "skipped": 0,
        "skipped_cached": 0,
        "errors": 0,
    }

    all_docs = await db.list_documents(limit=200, patient_id=patient_id)

    if only_new:
        from oncofiles.config import AI_REPROCESS_MAX_AGE_HOURS

        hours = max_age_hours if max_age_hours is not None else AI_REPROCESS_MAX_AGE_HOURS
        all_docs = [d for d in all_docs if _is_new(d.created_at, hours)]

    batch_count = 0

    for doc in all_docs:
        if doc.group_id or doc.deleted_at or doc.mime_type != "application/pdf":
            continue
        if not await db.has_ocr_text(doc.id):
            stats["skipped"] += 1
            continue

        pages = await db.get_ocr_pages(doc.id)
        if len(pages) < 2:
            continue

        # Idempotency guard (#406 Finding 1): if we already ran composition
        # analysis on this doc and the previous result was single-doc, skip.
        # OCR pages don't change after ingest, so re-asking the same model
        # the same question gives the same answer. Production showed the
        # same doc being re-analysed 3+ times per hour, wasting AI spend.
        try:
            prior = await db.search_prompt_log(
                PromptLogQuery(
                    call_type="doc_composition",
                    document_id=doc.id,
                    status="ok",
                    limit=1,
                )
            )
            if prior:
                raw = prior[0].raw_response or ""
                # Strip ```json ... ``` fences the model sometimes emits.
                if "```" in raw:
                    raw = raw.split("```", 2)[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                parsed_prior = _json.loads(raw) if raw else {}
                if parsed_prior.get("document_count") == 1:
                    stats["skipped_cached"] += 1
                    continue
        except Exception:
            # Parsing failures shouldn't block re-analysis; just fall through.
            pass

        stats["scanned"] += 1
        try:
            full_text = "\n\n".join(p["extracted_text"] for p in pages)
            sub_docs = analyze_document_composition(full_text, db=db, document_id=doc.id)

            if len(sub_docs) > 1:
                stats["multi_doc"] += 1
                logger.info(
                    "backfill_splits: doc %d (%s) has %d sub-documents",
                    doc.id,
                    doc.filename,
                    len(sub_docs),
                )

                if not dry_run:
                    created = await split_document(
                        db,
                        files,
                        gdrive,
                        doc,
                        sub_docs,
                        patient_id=patient_id,
                        folder_id=folder_id,
                        folder_map=folder_map or {},
                    )
                    stats["splits_created"] += len(created)

        except Exception:
            logger.warning("backfill_splits: error on doc %d", doc.id, exc_info=True)
            stats["errors"] += 1

        batch_count += 1
        if batch_count % 5 == 0:
            gc.collect()

    logger.info("backfill_multi_document_splits: %s (dry_run=%s)", stats, dry_run)
    return stats


async def backfill_consolidation(
    db: Database,
    gdrive: GDriveClient | None,
    *,
    patient_id: str,
    dry_run: bool = True,
    only_new: bool = False,
    max_age_hours: int | None = None,
) -> dict:
    """Scan documents for multi-file logical documents and consolidate them.

    Args:
        dry_run: If True, only report findings without making changes.
        only_new: If True, only consider docs created within max_age_hours. See #433.
        max_age_hours: Sliding window for only_new (default AI_REPROCESS_MAX_AGE_HOURS).

    Returns stats dict.
    """
    from oncofiles.consolidate import consolidate_documents
    from oncofiles.doc_analysis import analyze_consolidation

    stats = {"analyzed": 0, "groups_found": 0, "consolidated": 0, "errors": 0}

    all_docs = await db.list_documents(limit=200, patient_id=patient_id)

    if only_new:
        from oncofiles.config import AI_REPROCESS_MAX_AGE_HOURS

        hours = max_age_hours if max_age_hours is not None else AI_REPROCESS_MAX_AGE_HOURS
        all_docs = [d for d in all_docs if _is_new(d.created_at, hours)]

    ungrouped = [d for d in all_docs if d.group_id is None and d.deleted_at is None]
    stats["analyzed"] = len(ungrouped)

    if len(ungrouped) < 2:
        return stats

    doc_texts = []
    for doc in ungrouped:
        text = ""
        if await db.has_ocr_text(doc.id):
            pages = await db.get_ocr_pages(doc.id)
            text = "\n\n".join(p["extracted_text"] for p in pages)
        doc_texts.append((doc, text))

    try:
        groups = analyze_consolidation(doc_texts, db=db)
        stats["groups_found"] = len(groups)

        if not dry_run:
            for group in groups:
                if len(group.get("document_ids", [])) >= 2:
                    try:
                        await consolidate_documents(db, gdrive, group, patient_id=patient_id)
                        stats["consolidated"] += 1
                    except Exception:
                        logger.warning("backfill_consolidation: error", exc_info=True)
                        stats["errors"] += 1

    except Exception:
        logger.warning("backfill_consolidation: AI analysis failed", exc_info=True)
        stats["errors"] += 1

    logger.info("backfill_consolidation: %s (dry_run=%s)", stats, dry_run)
    return stats


async def rebuild_cross_references(
    db: Database,
    *,
    patient_id: str,
) -> dict:
    """Rebuild all cross-references using AI analysis.

    Clears existing cross-references and regenerates them using
    AI-powered document relationship analysis.

    Returns stats dict.
    """
    from oncofiles.doc_analysis import analyze_document_relationships

    stats = {"documents": 0, "refs_created": 0, "errors": 0}

    # Clear existing cross-references for this patient's documents
    all_docs = await db.list_documents(limit=200, patient_id=patient_id)
    active_docs = [d for d in all_docs if d.deleted_at is None]
    stats["documents"] = len(active_docs)

    for doc in active_docs:
        existing_refs = await db.get_cross_references(doc.id, patient_id=patient_id)
        for ref in existing_refs:
            await db.db.execute("DELETE FROM document_cross_references WHERE id = ?", (ref["id"],))
    await db.db.commit()

    # Rebuild with AI
    batch_count = 0
    for doc in active_docs:
        doc_text = ""
        if await db.has_ocr_text(doc.id):
            pages = await db.get_ocr_pages(doc.id)
            doc_text = "\n\n".join(p["extracted_text"] for p in pages)

        if not doc_text:
            continue

        candidates = [
            {
                "id": c.id,
                "filename": c.filename,
                "document_date": c.document_date.isoformat() if c.document_date else None,
                "institution": c.institution,
                "category": c.category.value,
                "ai_summary": c.ai_summary,
            }
            for c in active_docs
            if c.id != doc.id
        ]

        try:
            relationships = analyze_document_relationships(doc_text, doc.id, candidates, db=db)
            refs = []
            for rel in relationships:
                target_id = rel.get("target_id")
                rel_type = rel.get("relationship", "related")
                confidence = rel.get("confidence", 0.5)
                if target_id and target_id != doc.id:
                    refs.append((doc.id, target_id, rel_type, confidence))

            if refs:
                count = await db.bulk_insert_cross_references(refs)
                stats["refs_created"] += count

        except Exception:
            logger.warning("rebuild_cross_refs: error on doc %d", doc.id, exc_info=True)
            stats["errors"] += 1

        batch_count += 1
        if batch_count % 5 == 0:
            gc.collect()

    logger.info("rebuild_cross_references: %s", stats)
    return stats


async def backfill_ai_classification(
    db: Database,
    *,
    patient_id: str,
    dry_run: bool = True,
    limit: int = 10,
) -> dict:
    """Re-run AI metadata extraction on docs with missing institution/category.

    Uses the expanded extract_structured_metadata prompt that returns
    institution_code, category, and document_date from full document context.

    Args:
        dry_run: If True, only report what would change without making updates.
        limit: Max number of documents to process per call (default 10).

    Returns stats dict.
    """
    from oncofiles.enhance import classify_document

    stats = {
        "scanned": 0,
        "institution_fixed": 0,
        "category_fixed": 0,
        "date_fixed": 0,
        "skipped": 0,
        "errors": 0,
        "changes": [],
        "limit": limit,
    }

    # Proactive reconnect before batch to avoid stale replica (#378)
    await db.reconnect_if_stale(timeout=10.0)

    all_docs = await db.list_documents(limit=200, patient_id=patient_id)
    batch_count = 0

    for doc in all_docs:
        if doc.deleted_at:
            continue

        needs_work = (
            doc.institution is None or doc.category.value == "other" or doc.document_date is None
        )
        if not needs_work:
            continue

        if not await db.has_ocr_text(doc.id):
            stats["skipped"] += 1
            continue

        if stats["scanned"] >= limit:
            break

        stats["scanned"] += 1
        pages = await db.get_ocr_pages(doc.id)
        full_text = "\n\n".join(p["extracted_text"] for p in pages)

        try:
            classification = classify_document(full_text, db=db, document_id=doc.id)

            change = {"doc_id": doc.id, "filename": doc.filename, "updates": {}}

            # Institution from AI — primary path.
            ai_inst = classification.get("institution_code")
            # Fallback: if AI declined to map, reuse the provider keyword map
            # from structured_metadata.providers[] (#406 Finding 2). Doesn't
            # invent data — only projects already-extracted provider names
            # onto the canonical institution codes.
            if not ai_inst and doc.structured_metadata:
                try:
                    import json as _json

                    from oncofiles.enhance import infer_institution_from_providers

                    meta = _json.loads(doc.structured_metadata)
                    providers = meta.get("providers", []) or []
                    inferred = infer_institution_from_providers(providers)
                    if inferred:
                        ai_inst = inferred
                except Exception:
                    pass
            if doc.institution is None and ai_inst:
                change["updates"]["institution"] = {"old": None, "new": ai_inst}
                stats["institution_fixed"] += 1
                if not dry_run:
                    await db.db.execute(
                        "UPDATE documents SET institution = ? WHERE id = ?",
                        (ai_inst, doc.id),
                    )

            # Category from AI
            ai_cat = classification.get("category")
            if doc.category.value == "other" and ai_cat and ai_cat != "other":
                from oncofiles.models import DocumentCategory as _DocCat

                try:
                    _DocCat(ai_cat)  # validate
                    change["updates"]["category"] = {
                        "old": "other",
                        "new": ai_cat,
                    }
                    stats["category_fixed"] += 1
                    if not dry_run:
                        await db.update_document_category(doc.id, ai_cat)
                except ValueError:
                    pass

            # Date from AI
            ai_date = classification.get("document_date")
            if doc.document_date is None and ai_date:
                from datetime import date as _date

                try:
                    parsed = _date.fromisoformat(ai_date)
                    if 1900 <= parsed.year <= 2030:
                        change["updates"]["document_date"] = {
                            "old": None,
                            "new": ai_date,
                        }
                        stats["date_fixed"] += 1
                        if not dry_run:
                            await db.db.execute(
                                "UPDATE documents SET document_date = ? WHERE id = ?",
                                (ai_date, doc.id),
                            )
                except (ValueError, TypeError):
                    pass

            if change["updates"]:
                stats["changes"].append(change)
                logger.info(
                    "backfill_ai: doc %d (%s) — %s",
                    doc.id,
                    doc.filename,
                    change["updates"],
                )

        except Exception:
            logger.warning("backfill_ai: error on doc %d", doc.id, exc_info=True)
            stats["errors"] += 1

        batch_count += 1
        if batch_count % 5 == 0:
            gc.collect()

    if not dry_run:
        await db.db.commit()

    logger.info("backfill_ai_classification: %s (dry_run=%s)", stats, dry_run)
    return stats
