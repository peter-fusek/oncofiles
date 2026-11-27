"""Immediate local reading of holding-store documents (#950 phase 2).

A document a welcome@ trial patient sent is read right away (consent by
sending), with the LOCAL model only: ``curated.extract.extract`` on the held
bytes, inside ``cloud_guard.local_only("holding_extract")`` (also in its worker
thread), so any Anthropic / Files API call raises ``CloudForbiddenError``.
Each page of the result goes into ``document_pages`` (model
``HOLDING_MODEL``), so the dashboard, search and the MCP read tools see the
text as soon as the person signs in.

Rules (the #855 capacity rules, and the shared LM Studio, infra review):

* **Real patients first.** The reader never takes a model slot while curated
  work runs or waits (:func:`lm_busy`): a curated run, re-OCR or restore
  (``tools.curated.run_active``), ANY other job on the local model (re-OCR
  compare, LLM shadow/extraction previews, scorecards: every running job in
  ``tools.curated._reocr_jobs``), or a curated patient with runnable inbox
  work (``probe_stats``: pending minus parked; a signal unchanged for
  ``CURATED_PENDING_STALE_S`` is a stuck item and ignored, logged once and
  counted in :func:`reader_status`). It is checked before every document AND
  before every batch of ``PAGE_BATCH`` PDF pages; when it fires mid-document
  the document goes back to ``pending`` (not an attempt) and the run stops.
* One document at a time in the whole process (``_run_lock``), so the reader
  holds at most one LM Studio slot; across containers a document is claimed
  with a conditional update first (``db.claim_holding_extraction``,
  ``pending`` → ``reading``; a claim older than ``STALE_READING_MIN`` may be
  taken over). The claim is refreshed before every page batch
  (``db.refresh_holding_claim``), so a long document is never taken over
  while it is read, and the result is written only while that claim holds
  (the lease is part of the UPDATE's WHERE).
* Page budgets, checked with the document's page count BEFORE it is read
  (PDF page count, an image is one page): the per-patient UTC-day budget
  ``CURATED_DAILY_PAGE_BUDGET`` (curated pages + holding pages of the day)
  and a global cap for all trials together, ``GLOBAL_DAILY_PAGES`` (holding
  pages of the day). Over the patient's budget that patient waits until the
  next UTC day; over the global cap the run stops. A document longer than a
  whole day's budget is ``failed`` / ``too_many_pages``.
* After ``trial_ends_at`` nothing of that trial is read (the query skips it).
* Without a configured local model nothing is read (documents stay
  ``pending``). A model or transport error is retried on later runs, at most
  ``MAX_ATTEMPTS`` times, then the document is ``failed`` (shown as needing
  attention). A file the #815 exclusion refuses is ``excluded`` (from the
  extractor's ``Extraction.excluded`` code, never its text). ``error`` is
  always a code.
* The extractor is given a neutral name (``holding.<ext>``), never the
  original file name, so its own logs cannot carry it.

Runs: right after a document is stored (:func:`kick`) and every
``WORKER_INTERVAL_MIN`` minutes (the ``holding_extract`` lifespan loop,
``holding_loops``, ``local_only_task``; independent of APScheduler, #999).
DB calls stay on the event loop, one after the other;
only decryption, the page count and the extractor run in a worker thread.
Nothing is deleted. Logs: document ids, counts, reason codes and
``patient_id[:4]`` only.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

JOB = "holding_extract"
HOLDING_MODEL = "holding-local"
MAX_ATTEMPTS = 3
MAX_DOCS_PER_RUN = 20
WORKER_INTERVAL_MIN = 5
GLOBAL_DAILY_PAGES = 300  # all trials together, per UTC day
STALE_READING_MIN = 120  # a claim this old belongs to a reader that died

_RUN_LOCKS: dict[str, tuple] = {}


def _run_lock() -> asyncio.Lock:
    """One reader at a time per running event loop (production has one)."""
    loop = asyncio.get_running_loop()
    held = _RUN_LOCKS.get("current")
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _RUN_LOCKS["current"] = held
    return held[1]


_drain_task: asyncio.Task | None = None
_rekick = False
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

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


# A curated patient's runnable inbox work that has not changed for this long
# (one stuck, unparked item) no longer holds the trial reader back.
CURATED_PENDING_STALE_S = 6 * 3600
# PDF pages per model batch: the shared-model check and the claim refresh
# happen before every batch, not only per document.
PAGE_BATCH = 5

# pid -> ((pending, parked), first seen unchanged, monotonic seconds)
_pending_seen: dict[str, tuple[tuple[int, int], float]] = {}
_stale_logged: set[tuple[str, tuple[int, int]]] = set()
_status: dict = {"last_wait_reason": None, "last_wait_at": None, "stale_pending_ignored": 0}


def reader_status() -> dict:
    """Admin queue overview: why the reader last waited, and how often a stale
    curated inbox signal was ignored (codes and counts only)."""
    return dict(_status)


def reset_state() -> None:
    """Tests only."""
    _pending_seen.clear()
    _stale_logged.clear()
    _status.update(last_wait_reason=None, last_wait_at=None, stale_pending_ignored=0)


def lm_busy(now_mono: float | None = None) -> str | None:
    """Why the reader must leave the shared local model to real patients now.

    A curated patient's runnable inbox work (pending minus parked) counts
    until that signal has stayed the same for ``CURATED_PENDING_STALE_S``: a
    stuck, unparked item then no longer stops trial reading forever. Ignoring
    it is logged once per patient and signal and counted in
    :func:`reader_status` (``curated_pending_stale``).
    """
    import time

    from oncofiles.tools import curated

    now_mono = time.monotonic() if now_mono is None else now_mono
    if curated.run_active():
        return "curated_run_active"
    if any(j.get("state") == "running" for j in curated._reocr_jobs.values()):
        return "lm_job_active"
    for pid, st in curated.probe_stats().items():
        pending = int(st.get("pending_inbox_count") or 0)
        parked = int(st.get("parked_count") or 0)
        if pending - parked <= 0:
            _pending_seen.pop(pid, None)
            continue
        signal = (pending, parked)
        seen = _pending_seen.get(pid)
        if seen is None or seen[0] != signal:
            _pending_seen[pid] = (signal, now_mono)
            return "curated_pending"
        if now_mono - seen[1] <= CURATED_PENDING_STALE_S:
            return "curated_pending"
        if (pid, signal) not in _stale_logged:
            _stale_logged.add((pid, signal))
            logger.warning(
                "holding: curated inbox signal of patient %s unchanged for %d h; "
                "ignored for the trial reader (curated_pending_stale)",
                pid[:4],
                CURATED_PENDING_STALE_S // 3600,
            )
        _status["stale_pending_ignored"] += 1
    return None


def count_pages(data: bytes, mime: str) -> int:
    """Pages the model may read: a PDF's page count, 1 for an image, 0 for text."""
    mime = (mime or "").lower()
    if mime == "application/pdf":
        import pymupdf

        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception:
            return 1  # unreadable here: the extractor decides (and fails it)
        try:
            return max(1, int(doc.page_count))
        finally:
            doc.close()
    if mime.startswith("image/"):
        return 1
    return 0


def _batches(data: bytes, mime: str, pages: int) -> list[bytes]:
    """The document as model batches: a PDF of more than ``PAGE_BATCH`` pages
    is cut into PDFs of at most ``PAGE_BATCH`` pages; anything else is one."""
    if (mime or "").lower() != "application/pdf" or pages <= PAGE_BATCH:
        return [data]
    import pymupdf

    try:
        src = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        return [data]
    try:
        out = []
        for start in range(0, src.page_count, PAGE_BATCH):
            part = pymupdf.open()
            part.insert_pdf(src, from_page=start, to_page=min(start + PAGE_BATCH, pages) - 1)
            out.append(part.tobytes())
            part.close()
        return out
    finally:
        src.close()


def _decrypt_and_split(file_id: str, patient_id: str, mime: str) -> tuple[list[bytes], int]:
    """Worker thread: decrypt, count pages and cut into batches (no model)."""
    from oncofiles import holding_store

    data = holding_store.read(file_id, patient_id)
    pages = count_pages(data, mime)
    return _batches(data, mime, pages), pages


def _extract(data: bytes, mime: str):
    """Worker thread: read locally (inherits the local-only zone)."""
    from oncofiles.curated import extract as extract_mod

    return extract_mod.extract(neutral_name(mime), data, mime or "")


def error_code(error: str | None) -> str:
    """A stored code for an extractor error (never free text)."""
    head = str(error or "").split(":", 1)[0].strip()
    return head if _CODE_RE.match(head) else "extract_failed"


async def _budgets(db, patient_id: str, pages: int) -> str | None:
    """None when ``pages`` more pages fit today, else why not."""
    from oncofiles.tools.curated import _utc_day_start, pages_used_today, scheduling_settings

    day = _utc_day_start()
    budget, _ = scheduling_settings()
    if pages > GLOBAL_DAILY_PAGES or (budget > 0 and pages > budget):
        return "too_many_pages"
    used_all = await db.holding_model_pages_since(patient_id=None, since=day)
    if used_all + pages > GLOBAL_DAILY_PAGES:
        return "global_page_cap"
    if budget > 0:
        used = await pages_used_today(db, patient_id)
        used += await db.holding_model_pages_since(patient_id=patient_id, since=day)
        if used + pages > budget:
            return "daily_page_budget"
    return None


def _wait(counts: Counter, reason: str) -> None:
    counts[reason] += 1
    _status.update(last_wait_reason=reason, last_wait_at=_iso(_now()))
    logger.info("holding: reader waits reason=%s", reason)


async def run_pending(db, *, limit: int = MAX_DOCS_PER_RUN) -> Counter:
    """Read up to ``limit`` due holding documents, one at a time."""
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
    async with _run_lock():
        held: list[str] = []
        stop = False
        for _ in range(limit):
            if stop:
                break
            busy = lm_busy()
            if busy:
                _wait(counts, busy)
                break
            now = _now()
            stale_before = _iso(now - timedelta(minutes=STALE_READING_MIN))
            row = await db.next_holding_extraction(
                exclude_patients=held, now=_iso(now), stale_before=stale_before
            )
            if row is None:
                break
            pid, doc_id, mime = row["patient_id"], int(row["id"]), row["mime_type"]
            pp = pid[:4]
            claimed_at = _iso(now)
            if not await db.claim_holding_extraction(
                doc_id, patient_id=pid, claimed_at=claimed_at, stale_before=stale_before
            ):
                counts["lost_claim"] += 1
                continue
            try:
                state = dict(json.loads(row["structured_metadata"] or "{}").get("holding") or {})
            except (TypeError, ValueError, AttributeError):
                state = {}
            state["claimed_at"] = claimed_at
            try:
                batches, pages = await asyncio.to_thread(
                    _decrypt_and_split, row["file_id"], pid, mime
                )
            except Exception as exc:  # noqa: BLE001 - one document never stops the run
                logger.info("holding: doc=%d unreadable (%s)", doc_id, type(exc).__name__)
                state.update(extract="failed", error="unreadable", extracted_at=_iso(_now()))
                await db.set_holding_state(doc_id, patient_id=pid, holding=state)
                counts["failed"] += 1
                continue
            refusal = await _budgets(db, pid, pages)
            if refusal == "too_many_pages":
                state.update(extract="failed", error=refusal, extracted_at=_iso(_now()))
                await db.set_holding_state(doc_id, patient_id=pid, holding=state)
                counts["failed"] += 1
                continue
            if refusal:
                state["extract"] = "pending"  # release the claim; read on a later day
                await db.set_holding_state(doc_id, patient_id=pid, holding=state)
                counts[refusal] += 1
                logger.info("holding: patient %s held reason=%s", pp, refusal)
                if refusal == "global_page_cap":
                    break
                held.append(pid)
                continue
            texts: list[str] = []
            model_pages = 0
            methods: set[str] = set()
            ext = None
            error: str | None = None
            excluded: str | None = None
            interrupted = False
            lost = False
            for index, batch in enumerate(batches):
                if index:
                    # before every further batch: real patients first, and the
                    # claim moves forward so a long read is never taken over
                    busy = lm_busy()
                    if busy:
                        _wait(counts, busy)
                        interrupted = True
                        break
                    fresh = _iso(_now())
                    if fresh != claimed_at:
                        if not await db.refresh_holding_claim(
                            doc_id, patient_id=pid, claimed_at=claimed_at, new_claimed_at=fresh
                        ):
                            lost = True
                            break
                        claimed_at = state["claimed_at"] = fresh
                try:
                    with local_only(JOB):  # the worker thread inherits the zone
                        ext = await asyncio.to_thread(_extract, batch, mime)
                except Exception as exc:  # noqa: BLE001 - one document never stops the run
                    ext, error = None, type(exc).__name__
                    break
                if ext.excluded:
                    excluded = ext.excluded
                    break
                if not ext.ok:
                    error = ext.error
                    break
                texts.extend(split_pages(ext.text or "") or [""])
                model_pages += ext.pages if ext.method == "vision" else 0
                methods.add(ext.method)
            batches = []
            if lost:
                counts["lease_lost"] += 1
                continue
            if interrupted:
                # the shared model is needed by a real patient: back to pending,
                # not an attempt; read again from the start later
                state["extract"] = "pending"
                await db.set_holding_state(
                    doc_id, patient_id=pid, holding=state, claimed_at=claimed_at
                )
                stop = True
                continue
            state["attempts"] = int(state.get("attempts") or 0) + 1
            if excluded:
                state.update(
                    extract="excluded", error=error_code(excluded), extracted_at=_iso(_now())
                )
                counts["excluded"] += 1
            elif error is None and ext is not None:
                for number, text in enumerate(texts, start=1):
                    await db.save_ocr_page(doc_id, number, text, HOLDING_MODEL)
                state.update(
                    extract="done",
                    pages=len(texts),
                    model_pages=model_pages,
                    method="+".join(sorted(methods)),
                    extracted_at=_iso(_now()),
                    error=None,
                )
                counts["done"] += 1
            elif state["attempts"] >= MAX_ATTEMPTS:
                code = getattr(ext, "llm_failure", None) or "extract_failed"
                state.update(extract="failed", error=error_code(code), extracted_at=_iso(_now()))
                counts["failed"] += 1
            else:
                state.update(extract="pending", error="retry")
                counts["retry"] += 1
                held.append(pid)  # not again in this run
            if not await db.set_holding_state(
                doc_id, patient_id=pid, holding=state, claimed_at=claimed_at
            ):
                counts["lease_lost"] += 1
            logger.info(
                "holding: doc=%d patient %s extract=%s attempts=%d batches=%d",
                doc_id,
                pp,
                state.get("extract"),
                state["attempts"],
                max(1, (pages + PAGE_BATCH - 1) // PAGE_BATCH),
            )
    if counts:
        logger.info("holding: extraction run %s", dict(sorted(counts.items())))
    if counts.get("done"):
        # #950 phase 3: the first batch may be read now -> the one "ready" mail
        from oncofiles import trial_funnel

        try:
            await trial_funnel.send_due_ready_mails(db)
        except Exception as exc:  # noqa: BLE001 - mail never stops the reader
            logger.warning("holding: ready mails failed (%s)", type(exc).__name__)
    return counts
