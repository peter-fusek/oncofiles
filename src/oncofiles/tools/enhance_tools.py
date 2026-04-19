"""Enhancement and metadata extraction tools."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.tools._helpers import (
    _ensure_ocr_text,
    _get_db,
    _get_files,
    _get_gdrive,
    _get_patient_id,
    _try_download,
)


async def enhance_documents(
    ctx: Context,
    document_ids: str | None = None,
    limit: int = 0,
) -> str:
    """Run AI enhancement (summary + tags) on documents.

    If document_ids is omitted, processes all documents that haven't been enhanced yet.

    Args:
        document_ids: Comma-separated document IDs to enhance. If omitted, enhances all unprocessed.
        limit: Max documents to process (default 0 = no limit). Use smaller values
               (e.g. 10) to avoid MCP proxy timeouts on large patient records.
    """
    from oncofiles.sync import enhance_documents as _enhance_documents

    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = await _get_gdrive(ctx)

    parsed_ids = (
        [int(d.strip()) for d in document_ids.split(",") if d.strip()] if document_ids else None
    )

    pid = _get_patient_id()
    # force=True — user invoked the tool explicitly, bypass ai_processed_at guard (#433).
    stats = await _enhance_documents(
        db, files, gdrive, document_ids=parsed_ids, patient_id=pid, limit=limit, force=True
    )
    return json.dumps(stats)


async def extract_document_metadata(
    ctx: Context,
    document_id: int,
) -> str:
    """Extract and store structured medical metadata from a document.

    Uses AI to analyze the document text and extract findings, diagnoses,
    medications, providers, and a patient-friendly summary. Results are
    persisted in the structured_metadata column.

    Args:
        document_id: The local document ID to extract metadata from.
    """
    from oncofiles.enhance import extract_structured_metadata

    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = await _get_gdrive(ctx)

    # Patient isolation: verify caller owns this document
    pid = _get_patient_id()
    if not await db.check_document_ownership(document_id, pid):
        return json.dumps({"error": f"Document not found: {document_id}"})
    doc = await db.get_document(document_id)
    if not doc:
        return json.dumps({"error": f"Document not found: {document_id}"})

    # Get document text
    ok, content, raw_bytes = await _try_download(files, doc, gdrive)
    if not ok:
        return json.dumps({"error": "Cannot download document for text extraction"})

    texts = await _ensure_ocr_text(db, doc, content, raw_bytes)
    if not texts:
        return json.dumps({"error": "No text could be extracted from document"})

    full_text = "\n\n".join(texts)
    metadata = extract_structured_metadata(full_text, db=db, document_id=document_id)
    metadata_json = json.dumps(metadata)

    await db.update_structured_metadata(document_id, metadata_json)

    # Also run AI classification for institution/category/date
    from oncofiles.enhance import classify_document

    classification = classify_document(full_text, db=db, document_id=document_id)
    updates = {}

    ai_inst = classification.get("institution_code")
    if not doc.institution and ai_inst:
        await db.db.execute(
            "UPDATE documents SET institution = ? WHERE id = ?", (ai_inst, document_id)
        )
        await db.db.commit()
        updates["institution"] = ai_inst

    ai_cat = classification.get("category")
    if doc.category.value == "other" and ai_cat and ai_cat != "other":
        from oncofiles.models import DocumentCategory

        try:
            DocumentCategory(ai_cat)
            await db.update_document_category(document_id, ai_cat)
            updates["category"] = ai_cat
        except ValueError:
            pass

    ai_date = classification.get("document_date")
    if not doc.document_date and ai_date:
        from datetime import date

        try:
            parsed = date.fromisoformat(ai_date)
            if 1900 <= parsed.year <= 2030:
                await db.db.execute(
                    "UPDATE documents SET document_date = ? WHERE id = ?",
                    (ai_date, document_id),
                )
                await db.db.commit()
                updates["document_date"] = ai_date
        except (ValueError, TypeError):
            pass

    return json.dumps(
        {
            "document_id": document_id,
            "filename": doc.filename,
            "structured_metadata": metadata,
            "classification": classification,
            "updates_applied": updates,
        }
    )


async def extract_all_metadata(ctx: Context) -> str:
    """Backfill structured_metadata for all documents that have AI summaries but no metadata.

    Scans for documents where ai_processed_at is set but structured_metadata is empty,
    then extracts structured metadata from cached OCR text. Useful after adding the
    structured_metadata column to an existing database.
    """
    from oncofiles.sync import extract_all_metadata as _extract_all_metadata

    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = await _get_gdrive(ctx)

    pid = _get_patient_id()
    stats = await _extract_all_metadata(db, files, gdrive, patient_id=pid)
    return json.dumps(stats)


async def detect_and_split_documents(ctx: Context, dry_run: bool = True, limit: int = 10) -> str:
    """Scan documents for multi-document PDFs and split them.

    AI analyzes each PDF's content to detect when one file contains multiple
    distinct documents (different dates, institutions, or document types).
    Detected multi-document PDFs are split into separate files on Google Drive.

    Args:
        dry_run: If True (default), only report what would be split without making changes.
        limit: Max documents to scan per call (default 10). Use to avoid MCP proxy timeouts.
    """
    from oncofiles.doc_analysis import analyze_document_composition

    db = _get_db(ctx)
    pid = _get_patient_id()
    results = {"scanned": 0, "multi_doc": 0, "splits_created": 0, "limit": limit, "details": []}

    all_docs = await db.list_documents(limit=200, patient_id=pid)
    for doc in all_docs:
        if doc.group_id or doc.deleted_at or doc.mime_type != "application/pdf":
            continue
        if not await db.has_ocr_text(doc.id):
            continue

        pages = await db.get_ocr_pages(doc.id)
        if len(pages) < 2:
            continue

        if results["scanned"] >= limit:
            break

        results["scanned"] += 1
        full_text = "\n\n".join(p["extracted_text"] for p in pages)
        sub_docs = analyze_document_composition(full_text, db=db, document_id=doc.id)

        if len(sub_docs) > 1:
            results["multi_doc"] += 1
            detail = {
                "doc_id": doc.id,
                "filename": doc.filename,
                "sub_documents": len(sub_docs),
                "details": sub_docs,
            }
            results["details"].append(detail)

            if not dry_run:
                from oncofiles.split import split_document

                files = _get_files(ctx)
                gdrive = await _get_gdrive(ctx)
                created = await split_document(
                    db,
                    files,
                    gdrive,
                    doc,
                    sub_docs,
                    patient_id=pid,
                    folder_id="",
                    folder_map={},
                )
                results["splits_created"] += len(created)

    return json.dumps(results, default=str)


async def detect_and_consolidate_documents(ctx: Context, dry_run: bool = True) -> str:
    """Detect related multi-file documents and group them.

    AI compares content across files to find documents that are parts of the
    same logical document (e.g., a multi-page report scanned as separate PDFs).

    Args:
        dry_run: If True (default), only report what would be consolidated.
    """
    from oncofiles.doc_analysis import analyze_consolidation

    db = _get_db(ctx)
    pid = _get_patient_id()

    all_docs = await db.list_documents(limit=200, patient_id=pid)
    ungrouped = [d for d in all_docs if d.group_id is None and d.deleted_at is None]

    if len(ungrouped) < 2:
        return json.dumps({"message": "Not enough ungrouped documents to analyze."})

    doc_texts = []
    for doc in ungrouped:
        text = ""
        if await db.has_ocr_text(doc.id):
            pages = await db.get_ocr_pages(doc.id)
            text = "\n\n".join(p["extracted_text"] for p in pages)
        doc_texts.append((doc, text))

    groups = analyze_consolidation(doc_texts, db=db)
    results = {"analyzed": len(ungrouped), "groups_found": len(groups), "groups": groups}

    if not dry_run and groups:
        from oncofiles.consolidate import consolidate_documents

        gdrive = await _get_gdrive(ctx)
        for group in groups:
            if len(group.get("document_ids", [])) >= 2:
                await consolidate_documents(db, gdrive, group, patient_id=pid)
        results["consolidated"] = True

    return json.dumps(results, default=str)


async def backfill_ai_classification(ctx: Context, dry_run: bool = True, limit: int = 10) -> str:
    """Re-run AI classification on documents with missing institution, category, or date.

    Uses AI to read full document content (letterhead, stamps, addresses) to infer
    institution codes, correct categories, and extract document dates — replacing
    keyword-based heuristics with semantic understanding.

    Args:
        dry_run: If True (default), only report what would change without making updates.
        limit: Max documents to process per call (default 10). Use smaller values
               to avoid MCP proxy timeouts on large patient records.
    """
    from oncofiles.backfill_splits import backfill_ai_classification as _backfill

    db = _get_db(ctx)
    pid = _get_patient_id()
    stats = await _backfill(db, patient_id=pid, dry_run=dry_run, limit=limit)
    return json.dumps(stats, default=str)


async def unblock_stuck_documents(ctx: Context, dry_run: bool = True) -> str:
    """Unblock documents stuck in the institution + rename loop (#404).

    Fallback institution inference for docs that the normal backfill can't resolve —
    pulls the patient's primary treating oncology clinic from patient_context and
    applies it to safe categories (chemo_sheet, prescription, discharge) where
    provider letterhead is typically absent. Then reports how many filenames can
    be re-rendered to standard.

    Args:
        dry_run: If True (default), only report what would change. Set False to apply.
    """
    from oncofiles.enhance import backfill_institution_from_patient_context

    db = _get_db(ctx)
    pid = _get_patient_id()
    stats = await backfill_institution_from_patient_context(db.db, patient_id=pid, dry_run=dry_run)

    next_steps: list[str] = []
    if stats.get("updated", 0) > 0 and dry_run:
        next_steps.append(
            f"Run unblock_stuck_documents(dry_run=False) to apply "
            f"{stats['updated']} institution updates."
        )
    if stats.get("updated", 0) > 0 and not dry_run:
        next_steps.append(
            "Run rename_documents_to_standard(dry_run=False) to rewrite "
            "filenames now that institution is set."
        )
    if stats.get("skipped_no_context_institution"):
        next_steps.append(
            "patient_context.treatment.institution is empty — set it via update_patient_context "
            'first, e.g. update_patient_context(\'{"treatment":{"institution":"NOU"}}\').'
        )
    if stats.get("skipped_unsafe_category"):
        next_steps.append(
            f"{stats['skipped_unsafe_category']} docs in categories outside the safe fallback "
            "list (labs/imaging/pathology/etc.) were left alone — use reassign_document for those."
        )

    return json.dumps({"stats": stats, "patient_id": pid, "next_steps": next_steps})


def register(mcp):
    mcp.tool()(enhance_documents)
    mcp.tool()(extract_document_metadata)
    mcp.tool()(extract_all_metadata)
    mcp.tool()(detect_and_split_documents)
    mcp.tool()(detect_and_consolidate_documents)
    mcp.tool()(backfill_ai_classification)
    mcp.tool()(unblock_stuck_documents)
