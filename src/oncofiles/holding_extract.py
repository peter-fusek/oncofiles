"""Immediate local reading of holding-store documents (#950 phase 2).

A document a welcome@ trial patient sent is read right away (consent by
sending), with the LOCAL model only: ``curated.extract.extract`` on the held
bytes, inside ``cloud_guard.local_only("holding_extract")`` (also in its worker
thread), so any Anthropic / Files API call raises ``CloudForbiddenError``.
Each page of the result goes into ``document_pages`` (model
``HOLDING_MODEL``), so the dashboard, search and the MCP read tools see the
text as soon as the person signs in.

Rules (the #855 capacity rules where they fit a patient without Drive):

* One document at a time in the whole process (``_RUN_LOCK``), so the
  holding reading holds at most one LM Studio slot.
* The per-patient UTC-day page budget ``CURATED_DAILY_PAGE_BUDGET``
  (``tools.curated.scheduling_settings``) counts the curated runs' pages
  (``pages_used_today``) plus the holding pages of the day; a patient over it
  waits until the next UTC day (its documents stay ``pending``).
* The document cap ``MAX_DOCUMENTS_PER_PATIENT`` is enforced when the row is
  inserted (``insert_document``).
* Without a configured local model nothing is read (documents stay
  ``pending``). A model or transport error is retried on later runs, at most
  ``MAX_ATTEMPTS`` times, then the document is ``failed`` (shown as needing
  attention). A file the #815 exclusion refuses is ``excluded`` (never read).
* The extractor is given a neutral name (``holding.<ext>``), never the
  original file name, so its own logs cannot carry it.

Runs: right after a document is stored (:func:`kick`) and every
``WORKER_INTERVAL_MIN`` minutes (scheduler job ``holding_extract``,
``local_only_task``). DB calls stay on the event loop, one after the other;
only decryption and the extractor run in a worker thread. Nothing is deleted.
Logs: document ids, counts, reason codes and ``patient_id[:4]`` only.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

JOB = "holding_extract"
HOLDING_MODEL = "holding-local"
MAX_ATTEMPTS = 3
MAX_DOCS_PER_RUN = 20
WORKER_INTERVAL_MIN = 5

_RUN_LOCK = asyncio.Lock()
_drain_task: asyncio.Task | None = None
_rekick = False

_EXT_BY_MIME = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tif",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heic",
    "text/plain": "txt",
    "text/markdown": "md",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def neutral_name(mime: str) -> str:
    """The name the extractor sees: only the type, never the sender's name."""
    return f"holding.{_EXT_BY_MIME.get((mime or '').lower(), 'bin')}"


def kick(db) -> None:
    """Start reading pending holding documents in the background (one drain)."""
    global _drain_task, _rekick
    if (
        _drain_task is not None
        and not _drain_task.done()
        and _drain_task.get_loop() is asyncio.get_running_loop()
    ):
        _rekick = True
        return
    from oncofiles import inbound_email

    _drain_task = asyncio.create_task(_drain(db))
    inbound_email._BACKGROUND.add(_drain_task)
    _drain_task.add_done_callback(inbound_email._BACKGROUND.discard)


async def _drain(db) -> None:
    global _rekick
    from oncofiles.cloud_guard import local_only

    try:
        with local_only(JOB):
            while True:
                _rekick = False
                await run_pending(db)
                if not _rekick:
                    return
    except Exception as exc:  # noqa: BLE001 - a background task never raises
        logger.error("holding: extraction drain failed (%s)", type(exc).__name__)


async def _budget_left(db, patient_id: str) -> bool:
    from oncofiles.tools.curated import _utc_day_start, pages_used_today, scheduling_settings

    budget, _ = scheduling_settings()
    if budget <= 0:
        return True
    used = await pages_used_today(db, patient_id)
    used += await db.holding_model_pages_since(patient_id=patient_id, since=_utc_day_start())
    return used < budget


def _read_and_extract(file_id: str, mime: str):
    """Worker thread: decrypt and read locally (inherits the local-only zone)."""
    from oncofiles import holding_store
    from oncofiles.curated import extract as extract_mod

    data = holding_store.read(file_id)
    return extract_mod.extract(neutral_name(mime), data, mime or "")


async def run_pending(db, *, limit: int = MAX_DOCS_PER_RUN) -> Counter:
    """Read up to ``limit`` pending holding documents, one at a time."""
    import json

    from oncofiles import holding_store
    from oncofiles.cloud_guard import local_only
    from oncofiles.curated import llm
    from oncofiles.curated.indexer import split_pages

    counts: Counter = Counter()
    if not holding_store.configured():
        return counts
    if not llm.configured():
        return counts  # stays pending until the local model is configured
    async with _RUN_LOCK:
        held: list[str] = []
        for _ in range(limit):
            row = await db.next_holding_extraction(exclude_patients=held)
            if row is None:
                break
            pid, doc_id = row["patient_id"], int(row["id"])
            pp = pid[:4]
            if not await _budget_left(db, pid):
                held.append(pid)
                counts["daily_page_budget"] += 1
                logger.info("holding: patient %s held reason=daily_page_budget", pp)
                continue
            try:
                state = dict(json.loads(row["structured_metadata"] or "{}").get("holding") or {})
            except (TypeError, ValueError, AttributeError):
                state = {}
            state["attempts"] = int(state.get("attempts") or 0) + 1
            try:
                with local_only(JOB):  # the worker thread inherits the zone
                    ext = await asyncio.to_thread(
                        _read_and_extract, row["file_id"], row["mime_type"]
                    )
            except Exception as exc:  # noqa: BLE001 - one document never stops the run
                ext = None
                error = type(exc).__name__
            else:
                error = ext.error
            if ext is not None and ext.ok:
                pages = split_pages(ext.text or "") or [""]
                for number, text in enumerate(pages, start=1):
                    await db.save_ocr_page(doc_id, number, text, HOLDING_MODEL)
                state.update(
                    extract="done",
                    pages=len(pages),
                    model_pages=ext.pages if ext.method == "vision" else 0,
                    method=ext.method,
                    extracted_at=_iso(_now()),
                    error=None,
                )
                counts["done"] += 1
            elif error and "not sent to the model" in str(error):
                state.update(extract="excluded", error="excluded", extracted_at=_iso(_now()))
                counts["excluded"] += 1
            elif state["attempts"] >= MAX_ATTEMPTS:
                code = getattr(ext, "llm_failure", None) or "extract_failed"
                state.update(extract="failed", error=str(code)[:40], extracted_at=_iso(_now()))
                counts["failed"] += 1
            else:
                state.update(error="retry")
                counts["retry"] += 1
                held.append(pid)  # not again in this run
            await db.set_holding_state(doc_id, patient_id=pid, holding=state)
            logger.info(
                "holding: doc=%d patient %s extract=%s attempts=%d",
                doc_id,
                pp,
                state.get("extract"),
                state["attempts"],
            )
    if counts:
        logger.info("holding: extraction run %s", dict(sorted(counts.items())))
    return counts
