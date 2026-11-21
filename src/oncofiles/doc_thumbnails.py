"""Document previews for the dashboard (#948): ``GET /api/doc-thumbnails``.

The route (``server.api_doc_thumbnails``) answers a batch of document ids
with ``{"thumbnails": {"<id>": {"src": "data:image/...;base64,..."} | null}}``.

Rules (infra review of #948):

* **Patient scope.** The patient comes from ``_get_dashboard_patient_id``
  (#867); every id is loaded with ``db.get_document(doc_id, patient_id=pid)``.
  An unknown id, another patient's id, a soft-deleted row and an own document
  without a preview are all ``null``: the answers cannot be told apart (#590).
  A Drive file id is never taken from the request, only from the row.
* **Source.** Only the document's own Drive copy (``gdrive_id``, or the id in
  a ``gdrive:<id>`` file id): Drive's own thumbnail, fetched server-side with
  the patient's credentials (``GDriveClient.get_thumbnail``); when Drive holds
  none, a local render of page 1 with PyMuPDF from a capped download. A
  holding-store document (#950 phase 2, ``holding:<uuid>``) is rendered the
  same way from its held, decrypted bytes, also while Google is off. Never
  the Files API, never a model, no Drive or DB write. The ``thumbnailLink``
  (short-lived, access-bearing) never leaves ``GDriveClient``: only image
  bytes are proxied, as a data URI.
* **Excluded documents are never loaded.** A row the extraction exclusion
  rule refuses (``curated/llm_input.extraction_exclusion``: raw genotype
  export, ``exclude_extract: true``) and any raw text/CSV/VCF export
  (``is_raw_export``) is ``null`` before any Drive call.
* **Bounded Drive load.** At most ``DRIVE_CONCURRENCY`` Drive requests at a
  time across all requests of the process, one attempt each (no retry loop),
  ``DRIVE_TIMEOUT_S`` per request and ``BATCH_DEADLINE_S`` per batch; a 429,
  an HTTP error or a timeout makes that id ``null``. The heavy fallback
  (download + render) runs one at a time and never while a curated run or
  re-OCR is active (``tools.curated.run_active``, the signal behind
  ``/health.curated_run_active``).
* **Cache.** In memory only, LRU, at most ``CACHE_MAX_BYTES`` of image bytes
  and ``CACHE_TTL_S`` per entry, keyed by patient + document + size + the
  Drive copy (id, modifiedTime and md5 as recorded on the row). It holds
  bytes, never URLs; transient failures are not cached.
* **Logs.** Document ids, counts, reason codes and timings only — never a
  file name, a Drive name, a link or an exception message.

Lock tests: ``tests/test_948_doc_thumbnails.py``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import json
import logging
import re
import struct
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Any

from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL

logger = logging.getLogger(__name__)

# ── request contract ─────────────────────────────────────────────────────
SIZES_PX = {"s": 220, "l": 1000}  # list/grid tile, large preview
MAX_IDS = 24  # ids per call (a dashboard batch)
MAX_IDS_LARGE = 1  # ``size=l`` is one document at a time
_ID_RE = re.compile(r"[0-9]{1,15}")

ERR_IDS_REQUIRED = "ids_required"
ERR_INVALID_IDS = "invalid_ids"
ERR_TOO_MANY_IDS = "too_many_ids"
ERR_INVALID_SIZE = "invalid_size"
ERR_TOO_MANY_IDS_FOR_SIZE = "too_many_ids_for_size"

# ── cache ────────────────────────────────────────────────────────────────
# 24 MB of image bytes: about 1,000 small tiles (Drive's s220 thumbnails are a
# few kB to ~30 kB) or a dozen large previews; far below the memory restart
# threshold (MEMORY_RESTART_THRESHOLD_MB, default 420).
CACHE_MAX_BYTES = 24 * 1024 * 1024
CACHE_TTL_S = 3600.0  # a changed file or a revoked share shows up within an hour
CACHE_ENTRY_OVERHEAD = 256  # bytes counted per entry besides the image (key, bookkeeping)

# ── Drive load ───────────────────────────────────────────────────────────
DRIVE_CONCURRENCY = 4  # Drive requests in flight at once, whole process
FALLBACK_CONCURRENCY = 1  # download + local render, whole process
DRIVE_TIMEOUT_S = 8.0  # each Drive request, on its own short-lived connection
BATCH_DEADLINE_S = 20.0  # an id still pending then is null for this call

# ── images ───────────────────────────────────────────────────────────────
MAX_IMAGE_BYTES = {"s": 160 * 1024, "l": 1536 * 1024}  # bigger answers are dropped
FALLBACK_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # never download more to render page 1
FALLBACK_MAX_PIXELS = 25_000_000  # a photo bigger than this is not decoded
FALLBACK_JPEG_QUALITY = 72
FALLBACK_MIMES = frozenset({"application/pdf", "image/jpeg", "image/png"})
GOOGLE_EXPORTABLE = frozenset({
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.drawing",
})  # fmt: skip

_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")

# Outcomes that are a property of the file, not of the moment: cached as
# "no preview" for the TTL. Everything else (timeouts, 429, run active, no
# client) is retried by the next request.
_CACHEABLE_MISSES = frozenset(
    {"no_preview", "too_large", "size_unknown", "render_failed", "drive_trashed"}
)


class BadRequestError(ValueError):
    """A malformed query; ``code`` is the ``error`` of the 400 answer."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_query(ids_raw: str | None, size_raw: str | None) -> tuple[list[int], str]:
    """(unique ids in request order, size) or ``BadRequestError``."""
    size = (size_raw or "s").strip().lower()
    if size not in SIZES_PX:
        raise BadRequestError(ERR_INVALID_SIZE)
    tokens = [t.strip() for t in (ids_raw or "").split(",") if t.strip()]
    if not tokens:
        raise BadRequestError(ERR_IDS_REQUIRED)
    if len(tokens) > MAX_IDS:
        raise BadRequestError(ERR_TOO_MANY_IDS)
    ids: list[int] = []
    for token in tokens:
        if not _ID_RE.fullmatch(token) or int(token) < 1:
            raise BadRequestError(ERR_INVALID_IDS)
        if int(token) not in ids:
            ids.append(int(token))
    if size == "l" and len(ids) > MAX_IDS_LARGE:
        raise BadRequestError(ERR_TOO_MANY_IDS_FOR_SIZE)
    return ids, size


# ── cache ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CacheEntry:
    image: bytes | None  # None: the file has no preview (a cached miss)
    mime: str | None
    expires: float
    cost: int


class PreviewCache:
    """LRU of preview bytes with a byte cap and a TTL. Event loop only."""

    def __init__(
        self,
        max_bytes: int = CACHE_MAX_BYTES,
        ttl_s: float = CACHE_TTL_S,
        clock=time.monotonic,
    ) -> None:
        self.max_bytes = max_bytes
        self.ttl_s = ttl_s
        self._clock = clock
        self._items: OrderedDict[tuple, CacheEntry] = OrderedDict()
        self.bytes = 0

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: tuple) -> CacheEntry | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        if entry.expires <= self._clock():
            self._drop(key)
            return None
        self._items.move_to_end(key)
        return entry

    def put(self, key: tuple, image: bytes | None, mime: str | None) -> None:
        cost = (len(image) if image else 0) + CACHE_ENTRY_OVERHEAD
        if key in self._items:
            self._drop(key)
        if cost > self.max_bytes:
            return
        self._items[key] = CacheEntry(image, mime, self._clock() + self.ttl_s, cost)
        self.bytes += cost
        while self.bytes > self.max_bytes and self._items:
            self._drop(next(iter(self._items)))

    def clear(self) -> None:
        self._items.clear()
        self.bytes = 0

    def _drop(self, key: tuple) -> None:
        entry = self._items.pop(key, None)
        if entry is not None:
            self.bytes -= entry.cost


_cache = PreviewCache()

# One pair of semaphores per running event loop (production has one loop, so
# this is the module-wide limit; tests run each case on a fresh loop).
_semaphores_by_loop: dict[str, tuple[Any, asyncio.Semaphore, asyncio.Semaphore]] = {}


def _semaphores() -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
    loop = asyncio.get_running_loop()
    held = _semaphores_by_loop.get("current")
    if held is None or held[0] is not loop:
        held = (
            loop,
            asyncio.Semaphore(DRIVE_CONCURRENCY),
            asyncio.Semaphore(FALLBACK_CONCURRENCY),
        )
        _semaphores_by_loop["current"] = held
    return held[1], held[2]


# ── which documents may have a preview ───────────────────────────────────


def drive_id_of(doc) -> str | None:
    """The Drive id of the document's own copy, from the row only."""
    from oncofiles.curated.indexer import CURATED_FILE_PREFIX

    file_id = doc.file_id or ""
    candidates = (
        doc.gdrive_id,
        file_id[len(CURATED_FILE_PREFIX) :] if file_id.startswith(CURATED_FILE_PREFIX) else None,
    )
    return next((c for c in candidates if c and _DRIVE_ID_RE.match(c)), None)


def excluded(doc) -> bool:
    """True for a row that is never loaded for a preview: what the extraction
    exclusion rule refuses (raw genotype data, ``exclude_extract: true``) and
    any raw text / CSV / TSV / VCF export, whose picture would be its content."""
    from oncofiles.curated.llm_input import extraction_exclusion, is_raw_export

    meta: Any = doc.structured_metadata
    if meta:
        # left as text when unreadable: counts as unsure (excluded for a raw export)
        with contextlib.suppress(TypeError, ValueError):
            meta = json.loads(meta)
    category = getattr(doc.category, "value", doc.category)
    for name in {doc.filename, doc.original_filename}:
        row = {
            "filename": name or "",
            "mime_type": doc.mime_type,
            "category": category,
            "structured_metadata": meta,
        }
        if extraction_exclusion(row) or is_raw_export(row):
            return True
    return False


def _source(doc) -> tuple[str | None, str | None]:
    """(reason it has no preview, None) or (None, Drive id)."""
    if doc is None or doc.deleted_at is not None:
        return "not_found", None  # unknown, foreign and deleted alike
    if doc.sync_state == "deleted_remote":
        return "drive_missing", None
    if excluded(doc):
        return "excluded", None
    drive_id = drive_id_of(doc)
    if not drive_id:
        return "no_drive_copy", None
    return None, drive_id


def _version(doc) -> str:
    """The Drive copy as recorded on the row: a changed file gets a new key."""
    modified = doc.gdrive_modified_time.isoformat() if doc.gdrive_modified_time else ""
    return f"{modified}|{doc.gdrive_md5 or ''}"


# ── images ───────────────────────────────────────────────────────────────


def sniff_image_mime(data: bytes) -> str | None:
    """The image type by its magic bytes (headers are not trusted); None for
    anything that is not a JPEG, PNG, WebP or GIF (never SVG)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _image_pixels(data: bytes, mime: str) -> int | None:
    """Pixel count of a JPEG / PNG from its header, without decoding it."""
    if mime == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return width * height
    if mime == "image/jpeg":
        from oncofiles.curated.orientation import jpeg_info

        size = jpeg_info(data)[1]
        return size[0] * size[1] if size else None
    return None


def render_first_page(data: bytes, mime: str, width_px: int) -> bytes | None:
    """Page 1 of a PDF (or the photo) as a JPEG about ``width_px`` wide, or
    None. Local PyMuPDF only; a photo over ``FALLBACK_MAX_PIXELS``, an
    encrypted PDF or anything unreadable gives None."""
    import pymupdf

    is_pdf = mime == "application/pdf" or mime in GOOGLE_EXPORTABLE
    if not is_pdf:
        pixels = _image_pixels(data, mime)
        if pixels is None or pixels > FALLBACK_MAX_PIXELS:
            return None
    try:
        doc = pymupdf.open(stream=data, filetype="pdf" if is_pdf else mime.split("/", 1)[1])
    except Exception:
        return None
    try:
        if doc.needs_pass or doc.page_count < 1:
            return None
        rect = doc[0].rect
        if rect.width <= 0 or rect.height <= 0:
            return None
        # decompression-bomb guard: an embedded image on page 1 bigger than
        # FALLBACK_MAX_PIXELS is never decoded (its size is read from the
        # image dictionary, without decoding the stream)
        if is_pdf and any(
            int(img[2]) * int(img[3]) > FALLBACK_MAX_PIXELS for img in doc[0].get_images(full=True)
        ):
            return None
        zoom = min(width_px / rect.width, width_px * 1.5 / rect.height)
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("jpeg", jpg_quality=FALLBACK_JPEG_QUALITY)
    except Exception:
        return None
    finally:
        doc.close()


def data_uri(image: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"


# ── Drive work (worker threads; no DB) ───────────────────────────────────


@dataclass(frozen=True)
class Outcome:
    image: bytes | None
    mime: str | None
    reason: str  # "ok" or a reason code

    @property
    def cacheable(self) -> bool:
        return self.reason == "ok" or self.reason in _CACHEABLE_MISSES


def _error_reason(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 429:
        return "rate_limited"
    if status is not None:
        return "http_error"
    return "error"


def _curated_run_active() -> bool:
    """The /health.curated_run_active signal; unknown counts as active."""
    try:
        from oncofiles.tools.curated import run_active

        return bool(run_active())
    except Exception:
        return True


async def _bounded(sems: tuple[asyncio.Semaphore, ...], fn, deadline: float):
    """Run ``fn`` in a worker thread holding ``sems`` (acquired in order).

    The slots are released when the THREAD ends, not when this call gives up
    at ``deadline`` (raises TimeoutError), so the concurrency limits hold
    even for a request that outlives its batch."""
    loop = asyncio.get_running_loop()
    held: list[asyncio.Semaphore] = []
    try:
        for sem in sems:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(sem.acquire(), remaining)
            held.append(sem)
    except BaseException:
        for sem in held:
            sem.release()
        raise
    task = asyncio.ensure_future(asyncio.to_thread(fn))

    def _done(t: asyncio.Future) -> None:
        for sem in held:
            sem.release()
        if not t.cancelled():
            t.exception()  # retrieved: a late failure is never "never retrieved"

    task.add_done_callback(_done)
    return await asyncio.wait_for(asyncio.shield(task), max(deadline - loop.time(), 0.001))


def _fallback_blocking(gdrive, drive_id: str, mime: str, size: str) -> Outcome:
    """Download (capped) and render page 1. Worker thread; Drive is only read."""
    data = gdrive.download_capped(
        drive_id, mime, timeout=DRIVE_TIMEOUT_S, max_bytes=FALLBACK_MAX_DOWNLOAD_BYTES
    )
    if data is None:
        return Outcome(None, None, "too_large")
    image = render_first_page(data, mime, SIZES_PX[size])
    if not image or len(image) > MAX_IMAGE_BYTES[size]:
        return Outcome(None, None, "render_failed")
    return Outcome(image, "image/jpeg", "ok")


async def _fetch_one(gdrive, drive_id: str, size: str, deadline: float) -> Outcome:
    """One document's preview from Drive (no DB). Never raises."""
    drive_sem, fallback_sem = _semaphores()
    thumb = functools.partial(
        gdrive.get_thumbnail,
        drive_id,
        SIZES_PX[size],
        timeout=DRIVE_TIMEOUT_S,
        max_bytes=MAX_IMAGE_BYTES[size],
    )
    try:
        info = await _bounded((drive_sem,), thumb, deadline)
    except Exception as exc:  # one attempt only: 429, HTTP error, timeout → null
        return Outcome(None, None, _error_reason(exc))
    if info.get("trashed"):
        return Outcome(None, None, "drive_trashed")
    image = info.get("image")
    if image:
        mime = sniff_image_mime(image)
        return Outcome(image, mime, "ok") if mime else Outcome(None, None, "thumbnail_not_image")
    if info.get("has_thumbnail"):
        # Drive has one but the fetch did not give it (non-200, empty, too big):
        # no retry, no heavy fallback.
        return Outcome(None, None, "thumbnail_unavailable")

    # Drive holds no thumbnail: local render of page 1 (heavy path).
    mime = str(info.get("mime") or "")
    if mime not in FALLBACK_MIMES and mime not in GOOGLE_EXPORTABLE:
        return Outcome(None, None, "no_preview")
    if mime not in GOOGLE_EXPORTABLE:
        file_size = info.get("size")
        if not file_size:
            return Outcome(None, None, "size_unknown")
        if file_size > FALLBACK_MAX_DOWNLOAD_BYTES:
            return Outcome(None, None, "too_large")
    if _curated_run_active():
        return Outcome(None, None, "run_active")
    work = functools.partial(_fallback_blocking, gdrive, drive_id, mime, size)
    try:
        return await _bounded((fallback_sem, drive_sem), work, deadline)
    except Exception as exc:
        return Outcome(None, None, _error_reason(exc))


# ── holding-store documents (#950 phase 2) ───────────────────────────────


def _holding_blocking(file_id: str, mime: str, size: str) -> Outcome:
    """Decrypt a held file and render page 1 locally. Worker thread, no DB."""
    from oncofiles import holding_store

    data = holding_store.read(file_id)
    if len(data) > FALLBACK_MAX_DOWNLOAD_BYTES:
        return Outcome(None, None, "too_large")
    image = render_first_page(data, mime, SIZES_PX[size])
    if not image or len(image) > MAX_IMAGE_BYTES[size]:
        return Outcome(None, None, "render_failed")
    return Outcome(image, "image/jpeg", "ok")


async def _holding_previews(db, patient_id, ids, size, result, reasons) -> list[int]:
    """Previews of the patient's holding-store documents, from the held bytes.

    One SQL read scoped to the patient (``get_documents_by_ids(...,
    patient_id=)``: unknown and foreign ids are simply not there, and are left
    for the Drive path, which answers them ``null`` as before); deleted and
    excluded rows are ``null``. Local PyMuPDF render only, one at a time (the
    fallback semaphore), never while a curated run is active, cached like the
    rest. Returns the ids that are not holding documents.
    """
    from oncofiles.holding_store import is_holding_file_id

    found = await db.get_documents_by_ids(set(ids), patient_id=patient_id)
    rest: list[int] = []
    for doc_id in ids:
        doc = found.get(doc_id)
        if doc is None or not is_holding_file_id(doc.file_id):
            rest.append(doc_id)
            continue
        if doc.deleted_at is not None:
            reasons["not_found"] += 1
            continue
        if excluded(doc):
            reasons["excluded"] += 1
            continue
        mime = (doc.mime_type or "").lower()
        if mime not in FALLBACK_MIMES:
            reasons["no_preview"] += 1
            continue
        key = (patient_id, doc_id, size, doc.file_id, "holding")
        hit = _cache.get(key)
        if hit is not None:
            reasons["cache_hit" if hit.image else "cache_hit_none"] += 1
            if hit.image and hit.mime:
                result[str(doc_id)] = {"src": data_uri(hit.image, hit.mime)}
            continue
        if _curated_run_active():
            reasons["run_active"] += 1
            continue
        _drive_sem, fallback_sem = _semaphores()
        deadline = asyncio.get_running_loop().time() + BATCH_DEADLINE_S
        work = functools.partial(_holding_blocking, doc.file_id, mime, size)
        try:
            out = await _bounded((fallback_sem,), work, deadline)
        except TimeoutError:
            out = Outcome(None, None, "timeout")
        except Exception:  # missing key or file, undecryptable → null
            out = Outcome(None, None, "holding_unreadable")
        reasons[out.reason] += 1
        if out.cacheable:
            _cache.put(key, out.image, out.mime)
        if out.image and out.mime:
            result[str(doc_id)] = {"src": data_uri(out.image, out.mime)}
    return rest


# ── the batch ────────────────────────────────────────────────────────────


async def thumbnails_for(
    db,
    patient_id: str,
    ids: list[int],
    size: str,
    *,
    google_enabled: bool,
    make_clients,
) -> dict[str, dict | None]:
    """``{"<id>": {"src": data URI} | None}`` for the patient's documents.

    ``make_clients(db, patient_id)`` is ``server._create_patient_clients``.
    DB reads run here on the event loop, one after the other (one aiosqlite
    connection); only Drive work and rendering go to worker threads.
    """
    started = time.monotonic()
    result: dict[str, dict | None] = {str(i): None for i in ids}
    reasons: Counter[str] = Counter()
    drive_calls = 0
    if patient_id and patient_id != NO_PATIENT_ACCESS_SENTINEL:
        # #950 phase 2: holding-store documents are rendered from the held
        # bytes, Google or not; the rest goes the Drive way below.
        ids = await _holding_previews(db, patient_id, ids, size, result, reasons)
    if not ids:
        pass  # every id was a holding document
    elif not google_enabled:
        reasons["google_disabled"] = len(ids)
    elif not patient_id or patient_id == NO_PATIENT_ACCESS_SENTINEL:
        reasons["no_patient"] = len(ids)
    else:
        todo: list[tuple[int, str, tuple]] = []
        for doc_id in ids:  # sequential: single DB connection
            doc = await db.get_document(doc_id, patient_id=patient_id)
            reason, drive_id = _source(doc)
            if reason:
                reasons[reason] += 1
                continue
            key = (patient_id, doc_id, size, drive_id, _version(doc))
            hit = _cache.get(key)
            if hit is not None:
                reasons["cache_hit" if hit.image else "cache_hit_none"] += 1
                if hit.image and hit.mime:
                    result[str(doc_id)] = {"src": data_uri(hit.image, hit.mime)}
                continue
            todo.append((doc_id, drive_id, key))
        if todo:
            gdrive = None
            try:
                clients = await make_clients(db, patient_id)
                gdrive = clients[0] if clients else None
            except Exception as exc:
                logger.warning("doc-thumbnails: Drive client unavailable (%s)", type(exc).__name__)
            if gdrive is None:
                reasons["no_drive_client"] += len(todo)
            else:
                drive_calls = len(todo)
                deadline = asyncio.get_running_loop().time() + BATCH_DEADLINE_S
                outcomes = await asyncio.gather(
                    *(_fetch_one(gdrive, drive_id, size, deadline) for _, drive_id, _ in todo)
                )
                for (doc_id, _drive_id, key), out in zip(todo, outcomes, strict=True):
                    reasons[out.reason] += 1
                    if out.cacheable:
                        _cache.put(key, out.image, out.mime)
                    if out.image and out.mime:
                        result[str(doc_id)] = {"src": data_uri(out.image, out.mime)}
                    logger.debug("doc-thumbnails: doc=%d reason=%s", doc_id, out.reason)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    shown = sum(1 for v in result.values() if v)
    logger.log(
        logging.INFO if drive_calls else logging.DEBUG,
        "doc-thumbnails: patient=%s size=%s ids=%d shown=%d drive=%d reasons=%s ms=%d "
        "cache_entries=%d cache_bytes=%d",
        patient_id[:4] if patient_id != NO_PATIENT_ACCESS_SENTINEL else "-",  # prefix only
        size,
        len(ids),
        shown,
        drive_calls,
        dict(sorted(reasons.items())),
        elapsed_ms,
        len(_cache),
        _cache.bytes,
    )
    return result
