"""Raster previews of quarantined attachments for the caregiver's card (#976).

``GET /api/inbound-quarantine/preview`` (``server.api_inbound_quarantine_preview``)
asks :func:`render` for one file of one quarantined mail. This module is the
ONLY path from a quarantined file to anything but the approve path
(``inbound_quarantine`` itself imports no renderer; AST lock in
``tests/test_976_quarantine_preview.py``), and it never returns the file:

* **Who.** The route runs the card's ACL first (#867
  ``_dashboard_patient_or_refusal``, then the #921 write refusal: only the
  patient's own caregiver; an admin's read-only view and the operator bearer
  get 403 ``read_only``). The item is loaded with ``patient_id`` in SQL, so
  another patient's item id is the same 404 as an unknown one.
* **Which.** Only an item the card shows: ``pending``, or ``rejected`` /
  ``expired`` while its files are still on disk (the trash, restorable).
  Approved and purged items are 404.
* **What.** Only types with a safe local renderer: PDF (page 1), JPEG and PNG
  (``inbound_quarantine.PREVIEW_MIMES``; the #935 allowlist takes no GIF,
  and WebP / HEIC / TIFF have no stdlib-free renderer here), else 415 and the
  card keeps the file-type mark. The manifest type is never trusted: the
  decrypted bytes are sniffed again and must match it.
* **Integrity.** The decrypted bytes must have the manifest's sha256
  (constant-time compare); a mismatch is refused (409), nothing rendered.
* **Bounds.** Input at most ``MAX_INPUT_BYTES``; the #948 renderer
  (``doc_thumbnails.render_first_page``: PyMuPDF, decompression-bomb guard on
  the pixel count of a photo and of every image on page 1, encrypted PDFs
  refused) draws page 1 at most ``SIZES_PX`` wide (thumb 320, large 1600) and
  re-encodes it as JPEG; an answer over ``MAX_OUTPUT_BYTES`` is dropped. One
  render at a time per process (``RENDER_CONCURRENCY``) and, for fairness, at
  most one in flight per patient (``_PatientSlot``): a second
  request for a patient whose render still runs is refused at once
  (``BUSY`` → 429 ``preview_busy`` + ``Retry-After: BUSY_RETRY_AFTER_S``)
  instead of queueing for the one global slot, so one patient's burst cannot
  hold every other patient's card behind it. Both slots are released when
  the worker THREAD ends (``doc_thumbnails._bounded`` done-callback), also
  after a timeout or an exception, never earlier; ``RENDER_TIMEOUT_S`` per
  request. Decryption
  and rendering run in a worker thread; the only DB call (the item row) runs
  on the event loop before it.
* **No copies.** Nothing is cached, stored or indexed; no model, no OCR.
  The answer carries ``Cache-Control: no-store``.
* **Logs.** Item id, file index, size, a result code and milliseconds; never
  a file name, a sender, the text or bytes.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import logging
import time
from typing import Any

from oncofiles import doc_thumbnails, inbound_quarantine

logger = logging.getLogger(__name__)

SIZES_PX = {"thumb": 320, "large": 1600}
MAX_INPUT_BYTES = 25 * 1024 * 1024  # the #935 per-file cap of the filing path
MAX_OUTPUT_BYTES = {"thumb": 256 * 1024, "large": 4 * 1024 * 1024}
RENDER_TIMEOUT_S = 15.0
RENDER_CONCURRENCY = 1
BUSY_RETRY_AFTER_S = 2
SHOWN_STATES = ("pending", "rejected", "expired")

# result codes (the route maps them to statuses)
NOT_FOUND = "not_found"
UNSUPPORTED = "unsupported"
INTEGRITY = "integrity"
FAILED = "render_failed"
TIMEOUT = "timeout"
BUSY = "preview_busy"


class PreviewRefusedError(Exception):
    """No preview; ``code`` is one of the result codes above."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_sems: dict[str, tuple[Any, asyncio.Semaphore, set[str]]] = {}


def _loop_state() -> tuple[asyncio.Semaphore, set[str]]:
    """The global render slot and the patients in flight, per running loop."""
    loop = asyncio.get_running_loop()
    held = _sems.get("current")
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Semaphore(RENDER_CONCURRENCY), set())
        _sems["current"] = held
    return held[1], held[2]


class _PatientSlot:
    """At most one in-flight render per patient, shaped like a semaphore for
    ``doc_thumbnails._bounded`` (``acquire`` / ``release``), so it is released
    by the same done-callback as the global slot: when the worker thread
    ends, or at once when the request gives up before the thread starts.

    ``acquire`` never waits: the check and the mark happen in one step on the
    event loop (no ``await`` between them), and a held slot raises ``BUSY``.
    """

    def __init__(self, in_flight: set[str], patient_id: str) -> None:
        self._in_flight = in_flight
        self._patient_id = patient_id

    async def acquire(self) -> bool:
        if self._patient_id in self._in_flight:
            raise PreviewRefusedError(BUSY)
        self._in_flight.add(self._patient_id)
        return True

    def release(self) -> None:
        self._in_flight.discard(self._patient_id)


def patient_busy(patient_id: str) -> bool:
    """True while a render of this patient holds its slot (tests, telemetry)."""
    return patient_id in _loop_state()[1]


def parse_size(raw: str | None) -> str | None:
    value = (raw or "thumb").strip().lower()
    return value if value in SIZES_PX else None


def sniff(data: bytes) -> str | None:
    """The type by magic bytes: PDF, JPEG or PNG; anything else None."""
    if data[:5] == b"%PDF-":
        return "application/pdf"
    mime = doc_thumbnails.sniff_image_mime(data)
    return mime if mime in ("image/jpeg", "image/png") else None


def _render_blocking(
    patient_id: str, item_uuid: str, index: int, sha256: str, mime: str, size: str
) -> bytes:
    """Decrypt, verify, render. Worker thread; no DB."""
    try:
        data = inbound_quarantine.read_file(patient_id, item_uuid, index)
    except FileNotFoundError as exc:
        raise PreviewRefusedError(NOT_FOUND) from exc
    except Exception as exc:  # a blob that does not decrypt under this item
        raise PreviewRefusedError(INTEGRITY) from exc
    if len(data) > MAX_INPUT_BYTES:
        raise PreviewRefusedError(UNSUPPORTED)
    actual = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual, str(sha256 or "")):
        raise PreviewRefusedError(INTEGRITY)
    sniffed = sniff(data)
    if sniffed is None or sniffed != mime:
        raise PreviewRefusedError(UNSUPPORTED)
    image = doc_thumbnails.render_first_page(data, sniffed, SIZES_PX[size])
    del data
    if not image or image[:3] != b"\xff\xd8\xff" or len(image) > MAX_OUTPUT_BYTES[size]:
        raise PreviewRefusedError(FAILED)
    return image


async def render(db, patient_id: str, item_id: int, index: int, size: str) -> bytes:
    """A JPEG preview of file ``index`` of the patient's quarantined item.

    Raises :class:`PreviewRefusedError`. The caller has already checked that
    the caller is this patient's caregiver."""
    started = time.monotonic()
    code = "ok"
    try:
        if size not in SIZES_PX or index < 0:
            raise PreviewRefusedError(NOT_FOUND)
        item = await db.get_quarantine_item(item_id, patient_id=patient_id)
        if (
            item is None
            or item.get("state") not in SHOWN_STATES
            or item.get("files_purged_at")
            or index >= int(item.get("file_count") or 0)
        ):
            raise PreviewRefusedError(NOT_FOUND)
        try:
            _sender, files = inbound_quarantine._item_view(item)
        except Exception as exc:
            raise PreviewRefusedError(INTEGRITY) from exc
        if index >= len(files):
            raise PreviewRefusedError(NOT_FOUND)
        entry = files[index]
        mime = str(entry.get("mime") or "").lower()
        if not inbound_quarantine.previewable(item, entry):
            raise PreviewRefusedError(UNSUPPORTED)
        if int(entry.get("size") or 0) > MAX_INPUT_BYTES:
            raise PreviewRefusedError(UNSUPPORTED)
        job = functools.partial(
            _render_blocking,
            patient_id,
            item["item_uuid"],
            index,
            str(entry.get("sha256") or ""),
            mime,
            size,
        )
        loop = asyncio.get_running_loop()
        global_sem, in_flight = _loop_state()
        # the patient's slot first: a busy patient is refused before it would
        # wait for (and hold back others at) the global slot
        slots = (_PatientSlot(in_flight, patient_id), global_sem)
        try:
            return await doc_thumbnails._bounded(slots, job, loop.time() + RENDER_TIMEOUT_S)
        except TimeoutError as exc:
            raise PreviewRefusedError(TIMEOUT) from exc
    except PreviewRefusedError as exc:
        code = exc.code
        raise
    finally:
        logger.info(
            "quarantine preview: patient %s item=%d n=%d size=%s result=%s ms=%d",
            patient_id[:4],
            int(item_id),
            int(index),
            size,
            code,
            int((time.monotonic() - started) * 1000),
        )
