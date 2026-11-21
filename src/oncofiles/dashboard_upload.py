"""The dashboard's "Add documents" (#921): checks and where each file goes.

``POST /api/upload?patient_id=…`` (multipart, field ``files``) is routed in
``server.api_upload``, which runs the ACL first (``_dashboard_body_patient``,
then ``_dashboard_write_refusal``: an admin's read-only view of someone
else's patient cannot upload) and only then reads the body. This module does
the rest:

* **checks per file** — type by extension AND by content (PDF, JPEG, PNG,
  HEIC/HEIF, TIFF, WebP, ``.md``/``.txt`` notes in UTF-8); raw genotype
  exports are refused (``.vcf``/``.csv``/``.tsv`` by name, and a note that the
  #815 rule ``curated.llm_input.extraction_exclusion`` would exclude);
  ≤ ``MAX_FILE_BYTES`` (the rate limiter already refused a larger body with
  413 before reading it); name via ``files_api.sanitize_filename``;
* **checks per request** — ``MAX_FILES`` (one) file; the per-patient document
  cap (``MAX_DOCUMENTS_PER_PATIENT``) counted before anything is written;
  Google Drive connected (else ``no_drive``);
* **curated patient** (the connected folder is a curated root): every file
  goes unchanged into the README §2 inbox through
  ``curated.inbox_upload.upload_to_inbox`` (``drive_ops.upload_original``
  inside ``contract_writes()``); the pipeline picks it up after
  ``CURATED_SETTLE_S``;
* **non-curated patient**: the #777 Drive-only upload
  (``tools.documents.store_drive_only_upload``), whatever the cloud setting;
* a folder that only overlaps a curated tree, or whose check failed, is
  refused (``folder_not_writable``): fail closed.

Files are handled one after another and released before the next is read
(one DB connection, one Drive writer, one file's bytes in memory); the first
failed write stops the request. An upload never registers as a curated run or
job: it does not touch ``run_active()`` / the deploy gate. Logs carry counts, sizes, mime
types and name lengths only, never a file name. The response names a file
only by the name it was stored under, which the document table shows anyway.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field

from oncofiles.curated import drive_ops
from oncofiles.files_api import name_len, sanitize_filename

logger = logging.getLogger(__name__)

# One file per request (infra review of #921): the dashboard already sends one
# file per request, one after another (progress per file, and Traefik's 60 s
# read timeout applies per file); a server limit of one keeps a request's
# memory at one file and turns a client that batches into a visible refusal.
MAX_FILES = 1
MAX_FILE_BYTES = drive_ops.UPLOAD_MAX_BYTES  # 25 MiB

# extension -> canonical mime type (the browser's own type is not trusted)
ALLOWED_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".md": "text/markdown",
    ".txt": "text/plain",
}
TEXT_EXTS = (".md", ".txt")
RAW_GENOTYPE_EXTS = (".vcf", ".vcf.gz", ".csv", ".tsv")
GENOTYPE_REFUSED = "raw_genotype_excluded"

MESSAGES: dict[str, tuple[str, str]] = {
    "no_files": ("Nevybrali ste žiadny súbor.", "No file was selected."),
    "too_many_files": (
        "Jedna požiadavka nesie jeden súbor.",
        "One request carries one file.",
    ),
    "document_cap": (
        "Pacient dosiahol limit počtu dokumentov.",
        "The patient has reached the document limit.",
    ),
    "no_drive": (
        "Pre tohto pacienta nie je pripojený Google Drive, nie je kam nahrávať.",
        "Google Drive is not connected for this patient, so there is nowhere to upload.",
    ),
    "folder_not_writable": (
        "Do priečinka pacienta sa teraz nedá nahrávať. Kontaktujte správcu.",
        "The patient folder cannot take uploads right now. Contact the maintainer.",
    ),
    "inbox_unavailable": (
        "Priečinok pacienta nemá použiteľný priečinok pre nové súbory. Kontaktujte správcu.",
        "The patient folder has no usable inbox. Contact the maintainer.",
    ),
}


@dataclass
class Outcome:
    status: int
    payload: dict = field(default_factory=dict)


def refusal(code: str, status: int, **extra) -> Outcome:
    sk, en = MESSAGES[code]
    return Outcome(status, {"error": code, "message_sk": sk, "message_en": en, **extra})


def _ext(name: str) -> str:
    low = name.lower()
    return "." + low.rsplit(".", 1)[1] if "." in low else ""


def _content_matches(ext: str, data: bytes) -> bool:
    if ext == ".pdf":
        return b"%PDF-" in data[:1024]
    if ext in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".tif", ".tiff"):
        return data[:4] in (b"II*\x00", b"MM\x00*")
    if ext == ".webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if ext in (".heic", ".heif"):
        return data[4:8] == b"ftyp"
    if ext in TEXT_EXTS:
        if b"\x00" in data:
            return False
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return False
        return True
    return False


def check_file(name: str, data: bytes) -> tuple[str, str] | str:
    """``(safe name, mime)`` for an acceptable file, else a refusal code."""
    safe = sanitize_filename(name or "")
    low = safe.lower()
    if low.endswith(RAW_GENOTYPE_EXTS):
        return GENOTYPE_REFUSED  # #815 P0: raw genotype exports never enter
    ext = _ext(safe)
    mime = ALLOWED_TYPES.get(ext)
    if mime is None:
        return "type_not_allowed"
    if not data:
        return "empty_file"
    if len(data) > MAX_FILE_BYTES:
        return "file_too_large"
    if not _content_matches(ext, data):
        return "content_mismatch"
    if ext in TEXT_EXTS:
        from oncofiles.curated.llm_input import extraction_exclusion

        text = data.decode("utf-8-sig")
        # A note is judged as a raw text export: genotype markers in its name
        # or rsIDs in its text exclude it (when unsure, exclude).
        if extraction_exclusion({"filename": safe, "mime_type": "text/plain"}, [(1, text)]):
            return GENOTYPE_REFUSED
    return safe, mime


async def _read(upload) -> bytes:
    # One byte more than allowed is enough to know a file is too large.
    return await upload.read(MAX_FILE_BYTES + 1)


async def _free_document_name(db, patient_id: str, name: str) -> str:
    """``name`` or ``stem-2.ext`` … free among the patient's active documents."""
    from oncofiles.curated.inbox_upload import MAX_SUFFIX, split_ext

    if not await db.get_active_document_by_filename(name, patient_id=patient_id):
        return name
    stem, ext = split_ext(name)
    for n in range(2, MAX_SUFFIX + 1):
        candidate = f"{stem}-{n}{ext}"
        if not await db.get_active_document_by_filename(candidate, patient_id=patient_id):
            return candidate
    raise ValueError("no free document name")


@dataclass(frozen=True)
class _Target:
    """Where this request's files go; resolved once, on the first acceptable file."""

    path: str  # curated_inbox | drive_only
    gdrive: object
    folder_id: str


async def _resolve_target(db, patient_id: str, log_pid: str | None = None) -> _Target | Outcome:
    from oncofiles.curated_folder import curated_reason
    from oncofiles.server import _create_patient_clients

    clients = await _create_patient_clients(db, patient_id)
    if not clients or clients[0] is None or not clients[3]:
        return refusal("no_drive", 409)
    gdrive, folder_id = clients[0], clients[3]
    reason = await asyncio.to_thread(curated_reason, gdrive, folder_id, use_cache=False)
    if reason == "is a curated folder":
        return _Target("curated_inbox", gdrive, folder_id)
    if reason is None:
        return _Target("drive_only", gdrive, folder_id)
    logger.warning(
        "dashboard upload: patient %s folder overlaps a curated tree", log_pid or patient_id
    )
    return refusal("folder_not_writable", 409)


async def _store_one(db, patient_id: str, target: _Target, name: str, data: bytes, mime: str):
    """Store ONE file. Returns a result dict, or raises ``InboxUnavailableError``."""
    if target.path == "curated_inbox":
        from oncofiles.curated import inbox_upload

        files = [inbox_upload.InboxFile(name, data, mime)]
        results = await asyncio.to_thread(
            inbox_upload.upload_to_inbox, target.gdrive, target.folder_id, files
        )
        return results[0]
    from oncofiles.tools.documents import store_drive_only_upload

    try:
        free = await _free_document_name(db, patient_id, name)
        return await store_drive_only_upload(db, data, free, mime, patient_id=patient_id)
    except Exception as exc:
        logger.warning("dashboard upload: Drive-only store failed (%s)", type(exc).__name__)
        return {"status": "failed", "code": type(exc).__name__}


async def handle_upload(
    db, patient_id: str, uploads: list, *, log_pid: str | None = None
) -> Outcome:
    """Check and store ``uploads`` (Starlette ``UploadFile``s) for one patient.

    The caller has already passed the ACL and read-only checks. Memory (infra
    review of #921): the files are handled strictly one at a time — read
    (Starlette has spooled a large part to disk), checked, stored, then
    released and closed before the next one is read, so a request never holds
    more than one file's bytes. ``MAX_FILES`` is 1 anyway.

    ``log_pid`` replaces the patient id in this function's log lines (the
    #950 e-mail path logs ``patient_id[:4]`` only).
    """
    from oncofiles import config
    from oncofiles.curated.inbox_upload import InboxUnavailableError

    if not uploads:
        return refusal("no_files", 400)
    if len(uploads) > MAX_FILES:
        return refusal("too_many_files", 400, limit=MAX_FILES)

    cap = max(0, config.MAX_DOCUMENTS_PER_PATIENT)
    entries: list[dict] = []
    target: _Target | None = None
    stopped = False
    stored = 0
    total_bytes = 0
    mimes: Counter = Counter()
    name_lens: list[str] = []
    for i, up in enumerate(uploads):
        data = b""
        try:
            if stopped:
                entries.append({"index": i, "status": "not_attempted"})
                continue
            data = await _read(up)
            entry = {"index": i, "status": "refused", "size": min(len(data), MAX_FILE_BYTES + 1)}
            entries.append(entry)
            checked = check_file(getattr(up, "filename", "") or "", data)
            if isinstance(checked, str):
                entry["code"] = checked
                continue
            name, mime = checked
            if cap and await db.count_documents(patient_id=patient_id) + stored + 1 > cap:
                if not stored:
                    return refusal("document_cap", 409, limit=cap)
                entry["code"], stopped = "document_cap", True
                continue
            if target is None:
                resolved = await _resolve_target(db, patient_id, log_pid)
                if isinstance(resolved, Outcome):
                    return resolved
                target = resolved
            try:
                res = await _store_one(db, patient_id, target, name, data, mime)
            except InboxUnavailableError as exc:
                logger.warning(
                    "dashboard upload: patient %s inbox unavailable (%s)",
                    log_pid or patient_id,
                    exc.code,
                )
                if not stored:
                    return refusal("inbox_unavailable", 409, reason=exc.code)
                res = {"status": "failed", "code": "inbox_unavailable"}
            entry["status"] = res.get("status", "failed")
            for key in ("name", "code"):
                if res.get(key):
                    entry[key] = res[key]
            total_bytes += len(data)
            mimes[mime] += 1
            name_lens.append(name_len(name))
            if entry["status"] == "uploaded":
                stored += 1
            else:
                stopped = True
        finally:
            data = b""  # release this file's bytes before the next one is read
            await up.close()

    statuses = Counter(e["status"] for e in entries)
    logger.info(
        "dashboard upload: patient %s path=%s files=%d statuses=%s bytes=%d mimes=%s names=%s",
        log_pid or patient_id,
        target.path if target else "-",
        len(entries),
        dict(statuses),
        total_bytes,
        dict(mimes),
        ",".join(name_lens),
    )
    return Outcome(
        200,
        {
            "path": target.path if target else None,
            "uploaded": statuses.get("uploaded", 0),
            "files": entries,
        },
    )
