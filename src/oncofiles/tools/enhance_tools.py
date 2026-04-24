"""Enhancement and metadata extraction tools."""

from __future__ import annotations

import asyncio
import json
import logging

from fastmcp import Context

from oncofiles.tools._helpers import (
    _ensure_ocr_text,
    _get_db,
    _get_files,
    _get_gdrive,
    _get_patient_id,
    _resolve_patient_id,
    _try_download,
)

logger = logging.getLogger(__name__)


async def enhance_documents(
    ctx: Context,
    document_ids: str | None = None,
    limit: int = 0,
    patient_slug: str | None = None,
) -> str:
    """Run AI enhancement (summary + tags) on documents.

    If document_ids is omitted, processes all documents that haven't been enhanced yet.

    Args:
        document_ids: Comma-separated document IDs to enhance. If omitted, enhances all unprocessed.
        limit: Max documents to process (default 0 = no limit). Use smaller values
               (e.g. 10) to avoid MCP proxy timeouts on large patient records.
        patient_slug: Optional — explicit patient slug (e.g. 'mattias-cesnak'). Required
            in stateless HTTP contexts (#429) where select_patient does not persist.
    """
    from oncofiles.sync import enhance_documents as _enhance_documents

    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = await _get_gdrive(ctx)

    parsed_ids = (
        [int(d.strip()) for d in document_ids.split(",") if d.strip()] if document_ids else None
    )

    pid = await _resolve_patient_id(patient_slug, ctx)
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
    metadata = extract_structured_metadata(
        full_text, db=db, document_id=document_id, filename=doc.filename
    )
    metadata_json = json.dumps(metadata)

    await db.update_structured_metadata(document_id, metadata_json)

    # #465 — persist any extracted lab_values into the lab_values table so
    # the dashboard trend line picks them up even when the doc is classified
    # as chemo_sheet / consultation / discharge.
    from oncofiles.sync import _persist_lab_values_from_metadata

    lv_written = await _persist_lab_values_from_metadata(db, doc, metadata)

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
            "lab_values_persisted": lv_written,
        }
    )


async def extract_all_metadata(ctx: Context, patient_slug: str | None = None) -> str:
    """Backfill structured_metadata for all documents that have AI summaries but no metadata.

    Scans for documents where ai_processed_at is set but structured_metadata is empty,
    then extracts structured metadata from cached OCR text. Useful after adding the
    structured_metadata column to an existing database.

    Args:
        patient_slug: Optional — explicit patient slug. Required in stateless HTTP (#429).
    """
    from oncofiles.sync import extract_all_metadata as _extract_all_metadata

    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = await _get_gdrive(ctx)

    pid = await _resolve_patient_id(patient_slug, ctx)
    stats = await _extract_all_metadata(db, files, gdrive, patient_id=pid)
    return json.dumps(stats)


async def detect_and_split_documents(
    ctx: Context,
    dry_run: bool = True,
    limit: int = 10,
    patient_slug: str | None = None,
) -> str:
    """Scan documents for multi-document PDFs and split them.

    AI analyzes each PDF's content to detect when one file contains multiple
    distinct documents (different dates, institutions, or document types).
    Detected multi-document PDFs are split into separate files on Google Drive.

    Args:
        dry_run: If True (default), only report what would be split without making changes.
        limit: Max documents to scan per call (default 10). Use to avoid MCP proxy timeouts.
        patient_slug: Optional — explicit patient slug. Required in stateless HTTP (#429).
    """
    from oncofiles.doc_analysis import analyze_document_composition

    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
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


async def backfill_lab_values_from_metadata(
    ctx: Context,
    patient_slug: str | None = None,
    category_in: list[str] | None = None,
    dry_run: bool = True,
    batch_size: int = 10,
) -> str:
    """Re-run lab-value extraction on non-`labs` documents (#465).

    The AI classifier routes pre-chemo blood-count tables under
    `chemo_sheet` / `consultation` / `discharge_summary` — not unreasonable
    given the bundled form — but the original metadata extractor used a
    generic schema and never pulled the CBC into `lab_values`. The dashboard
    trend line therefore goes flat on dates where the actual labs were only
    recorded as part of a chemo-administration sheet.

    This tool scans documents in the configured categories, detects embedded
    lab tables via `has_lab_table`, re-extracts with the labs-specific
    Haiku prompt, and (when `dry_run=False`) persists the rows into
    `lab_values` so `get_lab_trends` returns them.

    Chunked-batch pattern per CLAUDE.md ("Bulk data fixes MUST NOT run as
    startup migrations"): each call processes up to `batch_size` documents
    and returns `remaining`; caller loops until `remaining == 0`.

    Cap-bypass: this is an explicit user-invoked tool (force=True semantics)
    so `DAILY_AI_DOC_CAP` does not apply — same convention as
    `enhance_documents`, `extract_document_metadata`,
    `detect_and_split_documents`.

    Args:
        patient_slug: Optional — explicit patient slug. Required in stateless HTTP.
        category_in: Categories to scan (default:
            ['chemo_sheet', 'consultation', 'discharge_summary', 'discharge']).
        dry_run: If True (default), only report what would be extracted
            without writing to `lab_values`.
        batch_size: Max documents to process per call (1-100, default 10).

    Returns JSON with:
        processed: docs examined this batch
        lab_values_added: rows persisted (0 when dry_run)
        detected_lab_tables: docs where the heuristic fired
        extracted_nonempty: docs where the AI returned >=1 value
        remaining: docs still matching the scan after this batch
        dry_run: bool
    """
    from oncofiles.enhance import extract_lab_values, has_lab_table
    from oncofiles.sync import _persist_lab_values_from_metadata

    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)

    cats = category_in or ["chemo_sheet", "consultation", "discharge_summary", "discharge"]
    # Validate categories — silently drop unknown tokens rather than hit DB
    # with a bogus IN clause.
    from oncofiles.models import DocumentCategory

    valid: list[str] = []
    for c in cats:
        try:
            DocumentCategory(c)
            valid.append(c)
        except ValueError:
            continue
    if not valid:
        return json.dumps({"error": "no valid categories in category_in"})

    batch_size = max(1, min(int(batch_size), 100))

    # Build the scan set: docs in the given categories where
    # structured_metadata is set but does NOT carry a "lab_values" block.
    # Using LIKE '%lab_values%' as a negative filter is cheap and sqlite-safe.
    placeholders = ",".join("?" for _ in valid)
    count_sql = (
        f"SELECT COUNT(*) AS cnt FROM documents "
        f"WHERE patient_id = ? AND deleted_at IS NULL "
        f"AND category IN ({placeholders}) "
        f"AND structured_metadata IS NOT NULL "
        f"AND structured_metadata NOT LIKE '%\"lab_values\"%'"
    )
    async with db.db.execute(count_sql, [pid, *valid]) as cur:
        row = await cur.fetchone()
        total_matching = dict(row)["cnt"] if row else 0

    if total_matching == 0:
        return json.dumps(
            {
                "processed": 0,
                "lab_values_added": 0,
                "detected_lab_tables": 0,
                "extracted_nonempty": 0,
                "remaining": 0,
                "dry_run": dry_run,
                "categories": valid,
                "status": "complete",
            }
        )

    batch_sql = (
        f"SELECT * FROM documents "
        f"WHERE patient_id = ? AND deleted_at IS NULL "
        f"AND category IN ({placeholders}) "
        f"AND structured_metadata IS NOT NULL "
        f"AND structured_metadata NOT LIKE '%\"lab_values\"%' "
        f"ORDER BY document_date DESC NULLS LAST, id DESC LIMIT ?"
    )
    from oncofiles.database._documents import _safe_row_to_document

    async with db.db.execute(batch_sql, [pid, *valid, batch_size]) as cur:
        rows = await cur.fetchall()

    docs = [d for r in rows if (d := _safe_row_to_document(r)) is not None]

    processed = 0
    detected = 0
    extracted_nonempty = 0
    lab_values_added = 0

    import json as _json

    async def _mark_scanned(doc, scan_result: str, lv_block: dict | None = None) -> None:
        """Write a sentinel lab_values block so the NOT LIKE filter excludes
        this doc on the next batch — prevents the pagination infinite loop
        where docs without lab tables kept reappearing and remaining never
        decremented. See #465 session 3 bug.
        """
        if dry_run:
            return
        try:
            meta = _json.loads(doc.structured_metadata) if doc.structured_metadata else {}
        except (ValueError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["lab_values"] = lv_block or {"values": [], "scan_result": scan_result}
        await db.update_structured_metadata(doc.id, _json.dumps(meta, ensure_ascii=False))

    for doc in docs:
        processed += 1
        # Pull text from OCR cache — no download fallback, if no text we skip.
        if not await db.has_ocr_text(doc.id):
            await _mark_scanned(doc, "no_ocr_text")
            continue
        pages = await db.get_ocr_pages(doc.id)
        full_text = "\n\n".join(p["extracted_text"] for p in pages if p.get("extracted_text"))
        if not full_text.strip():
            await _mark_scanned(doc, "empty_ocr_text")
            continue

        if not has_lab_table(full_text):
            await _mark_scanned(doc, "no_table_detected")
            continue
        detected += 1

        lv_block = extract_lab_values(full_text, db=db, document_id=doc.id)
        if not lv_block or not lv_block.get("values"):
            await _mark_scanned(doc, "ai_extracted_empty")
            continue
        extracted_nonempty += 1

        # Merge the populated lab_values block into the existing metadata JSON.
        try:
            meta = _json.loads(doc.structured_metadata) if doc.structured_metadata else {}
        except (ValueError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["lab_values"] = lv_block

        if dry_run:
            continue

        await db.update_structured_metadata(doc.id, _json.dumps(meta, ensure_ascii=False))
        written = await _persist_lab_values_from_metadata(db, doc, meta)
        lab_values_added += written

    remaining = total_matching - processed if dry_run else total_matching - processed

    return json.dumps(
        {
            "processed": processed,
            "lab_values_added": lab_values_added,
            "detected_lab_tables": detected,
            "extracted_nonempty": extracted_nonempty,
            "remaining": max(0, remaining),
            "dry_run": dry_run,
            "categories": valid,
            "batch_size": batch_size,
            "status": "partial" if remaining > 0 else "complete",
            "_next_call": (
                f"Call again with dry_run={dry_run} to process next {batch_size} docs"
                if remaining > 0
                else "All matching documents processed"
            ),
        }
    )


async def detect_and_consolidate_documents(
    ctx: Context,
    dry_run: bool = True,
    patient_slug: str | None = None,
) -> str:
    """Detect related multi-file documents and group them.

    AI compares content across files to find documents that are parts of the
    same logical document (e.g., a multi-page report scanned as separate PDFs).

    Args:
        dry_run: If True (default), only report what would be consolidated.
        patient_slug: Optional — explicit patient slug. Required in stateless HTTP (#429).
    """
    from oncofiles.doc_analysis import analyze_consolidation

    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)

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


async def backfill_ai_classification(
    ctx: Context,
    dry_run: bool = True,
    limit: int = 10,
    patient_slug: str | None = None,
) -> str:
    """Re-run AI classification on documents with missing institution, category, or date.

    Uses AI to read full document content (letterhead, stamps, addresses) to infer
    institution codes, correct categories, and extract document dates — replacing
    keyword-based heuristics with semantic understanding.

    Args:
        dry_run: If True (default), only report what would change without making updates.
        limit: Max documents to process per call (default 10). Use smaller values
               to avoid MCP proxy timeouts on large patient records.
        patient_slug: Optional — explicit patient slug. Required in stateless HTTP (#429).
    """
    from oncofiles.backfill_splits import backfill_ai_classification as _backfill

    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
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


async def repair_broken_groups(
    ctx: Context,
    dry_run: bool = True,
    patient_slug: str | None = None,
) -> str:
    """Un-group documents whose existing consolidation violates current guardrails.

    Scans all groups assigned to this patient and reports (or, when dry_run=False,
    resets) the ones that would now be rejected by :func:`consolidate_documents`:
    members span >7d by document_date, or they originate from distinct
    institutions. Rationale: #428 / #456 showed the AI happily grouped
    cross-month, cross-institution files into one "Part N of M" — the new
    guardrails block future occurrences, but existing data needs a one-shot
    cleanup.

    Each member of a rejected group has group_id / part_number / total_parts
    cleared and the trailing ``_PartNofN`` suffix stripped from the filename.
    Soft-deleted documents are untouched. GDrive file names are NOT mutated —
    the downstream rename pipeline will re-converge them on the next
    sync_to_gdrive run.

    Args:
        dry_run: If True (default), only report what would be reset.
        patient_slug: Optional — explicit patient slug. Required in stateless HTTP (#429).
    """
    import re

    from oncofiles.consolidate import (
        CONSOLIDATE_MAX_DATE_SPAN_DAYS,
        _dates_within_span,
        _institutions_compatible,
    )

    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)

    all_docs = await db.list_documents(limit=10000, patient_id=pid)
    grouped = [d for d in all_docs if d.group_id and d.deleted_at is None]

    groups: dict[str, list] = {}
    for d in grouped:
        groups.setdefault(d.group_id, []).append(d)

    part_suffix_re = re.compile(r"_?Part\d+of\d+(?=\.[^.]+$|$)")
    inspected = 0
    broken_groups: list[dict] = []
    reset_ids: list[int] = []

    for gid, members in groups.items():
        inspected += 1
        if len(members) < 2:
            # Orphan single-member group — also broken. Consolidation never
            # produces 1-member groups; this is leftover from an aborted split
            # or partial rollback (#456).
            reason = "single_member"
        elif not _dates_within_span(
            [d.document_date for d in members], CONSOLIDATE_MAX_DATE_SPAN_DAYS
        ):
            reason = "date_span_too_large"
        elif not _institutions_compatible([d.institution for d in members]):
            reason = "institutions_differ"
        else:
            continue  # group still passes guardrails

        broken_groups.append(
            {
                "group_id": gid,
                "reason": reason,
                "member_count": len(members),
                "member_ids": [d.id for d in members],
                "dates": [
                    d.document_date.isoformat() if d.document_date else None for d in members
                ],
                "institutions": [d.institution for d in members],
            }
        )

        if dry_run:
            continue

        for d in members:
            stripped_name = part_suffix_re.sub("", d.filename)
            await db.db.execute(
                "UPDATE documents SET group_id = NULL, part_number = NULL, "
                "total_parts = NULL, filename = ? WHERE id = ?",
                (stripped_name, d.id),
            )
            reset_ids.append(d.id)
        await db.db.commit()

    return json.dumps(
        {
            "dry_run": dry_run,
            "patient_id": pid,
            "groups_inspected": inspected,
            "broken_groups": broken_groups,
            "reset_document_ids": reset_ids,
        },
        default=str,
    )


async def detect_and_clone_vaccinations(
    ctx: Context,
    dry_run: bool = True,
    patient_slug: str | None = None,
) -> str:
    """Clone vaccination-log documents into every YYYY-MM folder they record (#460).

    For each document in the patient's ``vaccination`` category, AI parses
    the OCR text to enumerate individual vaccination events (date + product
    + dose label). For each event, a ``document_references`` row is upserted
    (UNIQUE on source_document_id + event_date + event_label, so repeated
    calls are idempotent); when ``dry_run=False`` AND the patient has GDrive
    wired, a GDrive shortcut file is also created in the target YYYY-MM
    folder of the ``vaccination`` category.

    The original document is never duplicated or moved. Each reference is a
    pointer — browsing GDrive per month surfaces the vaccine; the file bytes
    stay canonical.

    Args:
        dry_run: If True (default), report what would be created without
            touching the DB or GDrive.
        patient_slug: Optional — explicit patient slug (#429).
    """
    import json as _json
    import re
    import uuid as _uuid

    from oncofiles.doc_analysis import analyze_vaccination_events
    from oncofiles.gdrive_folders import (
        bilingual_name,
        ensure_year_month_folder,
        resolve_category_folder,
    )
    from oncofiles.models import DocumentCategory

    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)

    all_docs = await db.list_documents(limit=10000, patient_id=pid)
    vax_docs = [
        d for d in all_docs if d.deleted_at is None and d.category == DocumentCategory.VACCINATION
    ]
    if not vax_docs:
        return _json.dumps(
            {
                "dry_run": dry_run,
                "patient_id": pid,
                "message": "No vaccination-category documents for this patient.",
                "scanned": 0,
                "events_found": 0,
                "references_created": 0,
            }
        )

    # Resolve GDrive + folder_map only if we will write. For dry_run we skip
    # the gdrive plumbing entirely — it's a read-only simulation.
    gdrive = None
    folder_map: dict[str, str] | None = None
    root_folder_id: str | None = None
    if not dry_run:
        token = await db.get_oauth_token(patient_id=pid)
        root_folder_id = token.gdrive_folder_id if token else None
        if root_folder_id:
            gdrive = await _get_gdrive(ctx)
            if gdrive is not None:
                try:
                    from oncofiles import patient_context as _pctx
                    from oncofiles.gdrive_folders import ensure_folder_structure

                    ptype = (_pctx.get_context(pid) or {}).get("patient_type", "oncology")
                    folder_map = await asyncio.to_thread(
                        ensure_folder_structure, gdrive, root_folder_id, patient_type=ptype
                    )
                except Exception:
                    logger.exception("detect_and_clone_vaccinations: folder structure setup failed")
                    folder_map = None

    ym_re = re.compile(r"^\d{4}-\d{2}")
    scanned = 0
    all_events: list[dict] = []
    created_refs: list[dict] = []
    skipped_existing = 0
    created_shortcuts = 0
    shortcut_errors = 0

    for doc in vax_docs:
        scanned += 1
        # Collect OCR text for this doc; skip if none.
        try:
            if not await db.has_ocr_text(doc.id):
                continue
            pages = await db.get_ocr_pages(doc.id)
        except Exception:
            logger.debug("detect_and_clone_vaccinations: OCR fetch failed for %s", doc.id)
            continue
        text = "\n\n".join(p["extracted_text"] for p in pages)
        if not text.strip():
            continue

        events = analyze_vaccination_events(text, db=db, document_id=doc.id)
        for ev in events:
            date_str = ev.get("date", "")
            if not ym_re.match(date_str):
                continue  # AI returned malformed date — skip
            label_base = ev.get("vaccine_name") or "vaccine"
            dose = ev.get("dose_label") or ""
            event_label = f"{label_base}:{dose}" if dose else label_base

            entry = {
                "source_document_id": doc.id,
                "event_date": date_str,
                "event_label": event_label,
                "reasoning": ev.get("reasoning", ""),
            }
            all_events.append(entry)

            if dry_run:
                continue

            # Check for existing (respects UNIQUE constraint but also lets us
            # report duplicates before calling the GDrive API).
            async with db.db.execute(
                "SELECT id FROM document_references "
                "WHERE source_document_id = ? AND event_date = ? AND event_label = ?",
                (doc.id, date_str, event_label),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                skipped_existing += 1
                continue

            shortcut_id = None
            target_folder_id = None
            if gdrive is not None and folder_map and root_folder_id:
                try:
                    cat_folder = resolve_category_folder(
                        folder_map, DocumentCategory.VACCINATION.value, root_folder_id
                    )
                    target_folder_id = await asyncio.to_thread(
                        ensure_year_month_folder, gdrive, cat_folder, date_str
                    )
                    label_for_file = re.sub(r"[^A-Za-z0-9._-]+", "_", event_label)
                    shortcut_name = f"{date_str}_{label_for_file}"
                    result = await asyncio.to_thread(
                        gdrive.create_shortcut,
                        doc.gdrive_id,
                        shortcut_name,
                        target_folder_id,
                        {"oncofiles_clone_of": str(doc.id)},
                    )
                    shortcut_id = result.get("id")
                    created_shortcuts += 1
                except Exception:
                    shortcut_errors += 1
                    logger.exception(
                        "detect_and_clone_vaccinations: shortcut creation failed "
                        "for doc %d event %s",
                        doc.id,
                        event_label,
                    )

            try:
                await db.db.execute(
                    "INSERT INTO document_references "
                    "(patient_id, source_document_id, event_date, event_label, kind, "
                    " gdrive_shortcut_id, target_folder_id, metadata_json) "
                    "VALUES (?, ?, ?, ?, 'vaccination', ?, ?, ?)",
                    (
                        pid,
                        doc.id,
                        date_str,
                        event_label,
                        shortcut_id,
                        target_folder_id,
                        _json.dumps({"reasoning": ev.get("reasoning", "")}),
                    ),
                )
                await db.db.commit()
                created_refs.append(entry | {"shortcut_id": shortcut_id})
            except Exception:
                logger.exception(
                    "detect_and_clone_vaccinations: INSERT failed for doc %d event %s",
                    doc.id,
                    event_label,
                )
                shortcut_errors += 1

    # Note: the bilingual_name/uuid imports above are conservatively retained
    # for future expansion (kind='dental', etc.) but not used in this v1.
    _ = (bilingual_name, _uuid)

    return _json.dumps(
        {
            "dry_run": dry_run,
            "patient_id": pid,
            "scanned": scanned,
            "events_found": len(all_events),
            "events": all_events,
            "references_created": len(created_refs),
            "references": created_refs,
            "skipped_existing": skipped_existing,
            "shortcuts_created": created_shortcuts,
            "shortcut_errors": shortcut_errors,
        },
        default=str,
    )


def register(mcp):
    mcp.tool()(enhance_documents)
    mcp.tool()(extract_document_metadata)
    mcp.tool()(extract_all_metadata)
    mcp.tool()(detect_and_split_documents)
    mcp.tool()(detect_and_consolidate_documents)
    mcp.tool()(backfill_ai_classification)
    mcp.tool()(backfill_lab_values_from_metadata)
    mcp.tool()(unblock_stuck_documents)
    mcp.tool()(repair_broken_groups)
    mcp.tool()(detect_and_clone_vaccinations)
