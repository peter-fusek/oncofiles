"""Naming convention migration tools."""

from __future__ import annotations

import asyncio
import json
import logging

from fastmcp import Context

from oncofiles.filename_parser import is_corrupted_filename, is_standard_format, rename_to_standard
from oncofiles.tools._helpers import _get_db, _get_gdrive, _get_patient_id

logger = logging.getLogger(__name__)


async def rename_documents_to_standard(
    ctx: Context,
    dry_run: bool = True,
    en_descriptions: str | None = None,
) -> str:
    """Rename documents from old naming conventions to the standard format.

    Standard format: YYYYMMDD_PatientName_Institution_Category_DescriptionEN.ext

    In dry_run mode (default), returns a preview of proposed renames without
    making any changes. Set dry_run=False to execute the rename.

    Args:
        dry_run: If True (default), only preview changes. Set False to execute.
        en_descriptions: Optional JSON object mapping doc_id → English description
                        (e.g. '{"15": "BloodResultsPreCycle3", "42": "CTAbdomen"}').
                        If not provided, keeps existing descriptions.
    """
    import time

    from oncofiles.sync import _SYNC_LOCK_TIMEOUT, _sync_lock, _sync_lock_acquired_at

    db = _get_db(ctx)
    gdrive = _get_gdrive(ctx)

    # Parse en_descriptions if provided
    desc_map: dict[int, str] = {}
    if en_descriptions:
        try:
            desc_map = {int(k): v for k, v in json.loads(en_descriptions).items()}
        except (json.JSONDecodeError, ValueError) as e:
            return json.dumps({"error": f"Invalid en_descriptions JSON: {e}"})

    docs = await db.list_documents(limit=500, patient_id=_get_patient_id())

    stats = {"total": len(docs), "already_standard": 0, "renamed": 0, "skipped": 0, "errors": 0}
    renames = []

    for doc in docs:
        if is_standard_format(doc.filename):
            stats["already_standard"] += 1
            continue

        en_desc = desc_map.get(doc.id)

        # Handle corrupted filenames using DB metadata
        if is_corrupted_filename(doc.filename):
            import re

            from oncofiles.filename_parser import CATEGORY_FILENAME_TOKENS
            from oncofiles.patient_context import get_patient_name

            patient = get_patient_name().replace(" ", "") or "Patient"
            cat_token = CATEGORY_FILENAME_TOKENS.get(doc.category, "Other")
            if doc.document_date:
                date_str = doc.document_date.strftime("%Y%m%d")
            elif doc.created_at:
                date_str = doc.created_at.strftime("%Y%m%d")
            else:
                date_str = "20260201"
            inst = doc.institution or "Unknown"
            desc = en_desc or doc.description or "Document"
            desc = re.sub(r"[^a-zA-Z0-9]", "", desc)[:60]
            ext = "." + doc.filename.rsplit(".", 1)[-1] if "." in doc.filename else ".pdf"
            new_name = f"{date_str}_{patient}_{inst}_{cat_token}_{desc}{ext}"
        else:
            new_name = rename_to_standard(
                doc.filename,
                category=doc.category.value,
                en_description=en_desc,
            )

        if new_name == doc.filename:
            stats["skipped"] += 1
            continue

        renames.append(
            {
                "id": doc.id,
                "old": doc.filename,
                "new": new_name,
                "category": doc.category.value,
                "has_gdrive": bool(doc.gdrive_id),
            }
        )

    if dry_run:
        return json.dumps(
            {
                "dry_run": True,
                "stats": stats | {"would_rename": len(renames)},
                "renames": renames,
            }
        )

    # Execute renames — acquire sync lock to prevent concurrent sync
    lock_acquired = False
    try:
        # Check if lock is stale
        if _sync_lock.locked():
            elapsed = time.time() - _sync_lock_acquired_at
            if elapsed > _SYNC_LOCK_TIMEOUT:
                logger.warning("Sync lock stale (%.0fs) — forcing release for rename", elapsed)
                _sync_lock.release()

        try:
            await asyncio.wait_for(_sync_lock.acquire(), timeout=30)
            lock_acquired = True
        except TimeoutError:
            return json.dumps(
                {"error": "Could not acquire sync lock — sync in progress. Try later."}
            )

        await ctx.info(f"Renaming {len(renames)} documents to standard format...")

        for item in renames:
            doc_id = item["id"]
            new_name = item["new"]

            try:
                doc = await db.get_document(doc_id)
                if not doc:
                    stats["errors"] += 1
                    continue

                # Rename on GDrive if available
                if doc.gdrive_id and gdrive:
                    await asyncio.to_thread(gdrive.rename_file, doc.gdrive_id, new_name)

                    # Rename OCR companion
                    old_stem = (
                        doc.filename.rsplit(".", 1)[0] if "." in doc.filename else doc.filename
                    )
                    new_stem = new_name.rsplit(".", 1)[0] if "." in new_name else new_name
                    old_ocr = f"{old_stem}_OCR.txt"
                    new_ocr = f"{new_stem}_OCR.txt"
                    try:
                        parents = await asyncio.to_thread(gdrive.get_file_parents, doc.gdrive_id)
                        if parents:
                            siblings = await asyncio.to_thread(
                                gdrive.list_folder, parents[0], recursive=False
                            )
                            for sib in siblings:
                                if sib["name"] == old_ocr:
                                    await asyncio.to_thread(gdrive.rename_file, sib["id"], new_ocr)
                                    break
                    except Exception:
                        logger.warning("OCR rename failed for doc %d", doc_id)

                # Update DB filename (keep original_filename for rollback)
                await db.update_document_filename(doc_id, new_name)
                logger.info("Renamed doc %d: '%s' → '%s'", doc_id, doc.filename, new_name)
                stats["renamed"] += 1

            except Exception:
                logger.exception("Error renaming doc %d", doc_id)
                stats["errors"] += 1

    finally:
        if lock_acquired:
            _sync_lock.release()

    return json.dumps(
        {
            "dry_run": False,
            "stats": stats,
            "renames": [
                {"id": r["id"], "old": r["old"], "new": r["new"]}
                for r in renames[:50]  # Limit output size
            ],
        }
    )


def register(mcp):
    mcp.tool()(rename_documents_to_standard)
