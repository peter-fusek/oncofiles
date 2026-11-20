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
* **checks per request** — ≤ ``MAX_FILES`` files; the per-patient document
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

Files are handled one after another (one DB connection, one Drive writer);
the first failed write stops the request. Logs carry counts, sizes, mime
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

MAX_FILES = 20
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
        f"Naraz najviac {MAX_FILES} súborov.",
        f"At most {MAX_FILES} files at a time.",
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


@dataclass(frozen=True)
class Accepted:
    index: int
    name: str
    data: bytes
    mime: str


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


async def _store_drive_only(db, patient_id: str, files: list[Accepted]) -> list[dict]:
    from oncofiles.tools.documents import store_drive_only_upload

    results: list[dict] = []
    stopped = False
    for f in files:
        if stopped:
            results.append({"status": "not_attempted"})
            continue
        try:
            name = await _free_document_name(db, patient_id, f.name)
            res = await store_drive_only_upload(db, f.data, name, f.mime, patient_id=patient_id)
        except Exception as exc:
            logger.warning("dashboard upload: Drive-only store failed (%s)", type(exc).__name__)
            res = {"status": "failed", "code": type(exc).__name__}
        if res.get("status") != "uploaded":
            stopped = True
        results.append(res)
    return results


async def handle_upload(db, patient_id: str, uploads: list) -> Outcome:
    """Check and store ``uploads`` (Starlette ``UploadFile``s) for one patient.

    The caller has already passed the ACL and read-only checks.
    """
    from oncofiles import config

    if not uploads:
        return refusal("no_files", 400)
    if len(uploads) > MAX_FILES:
        return refusal("too_many_files", 400, limit=MAX_FILES)

    entries: list[dict] = []
    accepted: list[Accepted] = []
    for i, up in enumerate(uploads):
        data = await _read(up)
        checked = check_file(getattr(up, "filename", "") or "", data)
        size = min(len(data), MAX_FILE_BYTES + 1)
        if isinstance(checked, str):
            entries.append({"index": i, "status": "refused", "code": checked, "size": size})
            continue
        name, mime = checked
        accepted.append(Accepted(i, name, data, mime))
        entries.append({"index": i, "status": "pending", "size": size})

    path = None
    if accepted:
        cap = max(0, config.MAX_DOCUMENTS_PER_PATIENT)
        if cap:
            count = await db.count_documents(patient_id=patient_id)
            if count + len(accepted) > cap:
                return refusal("document_cap", 409, limit=cap)

        from oncofiles.curated_folder import curated_reason
        from oncofiles.server import _create_patient_clients

        clients = await _create_patient_clients(db, patient_id)
        if not clients or clients[0] is None or not clients[3]:
            return refusal("no_drive", 409)
        gdrive, folder_id = clients[0], clients[3]
        reason = await asyncio.to_thread(curated_reason, gdrive, folder_id, use_cache=False)
        if reason == "is a curated folder":
            from oncofiles.curated import inbox_upload

            path = "curated_inbox"
            files = [inbox_upload.InboxFile(a.name, a.data, a.mime) for a in accepted]
            try:
                results = await asyncio.to_thread(
                    inbox_upload.upload_to_inbox, gdrive, folder_id, files
                )
            except inbox_upload.InboxUnavailableError as exc:
                logger.warning(
                    "dashboard upload: patient %s inbox unavailable (%s)", patient_id, exc.code
                )
                return refusal("inbox_unavailable", 409, reason=exc.code)
        elif reason is None:
            path = "drive_only"
            results = await _store_drive_only(db, patient_id, accepted)
        else:
            logger.warning(
                "dashboard upload: patient %s folder overlaps a curated tree", patient_id
            )
            return refusal("folder_not_writable", 409)

        for a, res in zip(accepted, results, strict=True):
            entry = entries[a.index]
            entry["status"] = res.get("status", "failed")
            if res.get("name"):
                entry["name"] = res["name"]
            if res.get("code"):
                entry["code"] = res["code"]

    statuses = Counter(e["status"] for e in entries)
    logger.info(
        "dashboard upload: patient %s path=%s files=%d statuses=%s bytes=%d mimes=%s names=%s",
        patient_id,
        path or "-",
        len(entries),
        dict(statuses),
        sum(len(a.data) for a in accepted),
        dict(Counter(a.mime for a in accepted)),
        ",".join(name_len(a.name) for a in accepted),
    )
    return Outcome(
        200,
        {"path": path, "uploaded": statuses.get("uploaded", 0), "files": entries},
    )
