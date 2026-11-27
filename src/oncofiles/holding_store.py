"""Encrypted holding store for welcome@ trial documents (#950 phase 2).

A document sent to ``welcome@`` (Flow B) belongs to a trial patient that has
no Google Drive yet. Its bytes wait here, on the server volume, for the 30-day
trial (maintainer decision 2026-11-21), until the person connects Drive and
the files move there (phase 3, not built: see the seam below).

Storage
-------
* One file per document under ``<DATA_DIR>/holding/`` (``holding_dir()``),
  named ``<uuid>.bin`` after a random uuid4. The directory is owner-only
  (0700), every file owner-only (0600), set with chmod whatever the umask. A
  file is written to ``<uuid>.tmp``, fsynced, renamed into place (atomic on one
  volume) and the directory is fsynced after the rename (infra review).
* Content: ``v1`` magic + 96-bit random nonce + AES-GCM ciphertext
  (``cryptography``) with ``secrets_keys.holding_store_key()`` (purpose
  ``oncofiles.holding_store.v1``) and ``holding|<patient_id>|<uuid>`` as
  associated data, so a file renamed onto another uuid or claimed for another
  patient never decrypts. No plaintext ever touches the disk; the original
  file name is not stored in the file.
* The key comes from the MANDATORY runtime env ``HOLDING_STORE_SECRET``
  (fail closed like ``INBOUND_JOBS_SECRET``): unset = ``configured()`` is
  False, the welcome@ admission refuses the mail before a trial is created,
  and every read raises.

Limits (infra review), all checked BEFORE anything is written, so a refusal
never leaves a file behind (:func:`check_limits`, ``HoldingRefusedError.code``):

* ``trial_ended``: the trial's ``trial_ends_at`` has passed (no new writes);
* ``document_cap``: ``MAX_DOCUMENTS_PER_PATIENT``;
* ``trial_bytes_cap``: more than ``TRIAL_MAX_BYTES`` held for the patient;
* ``holding_full``: more than ``HOLDING_MAX_BYTES`` held in all;
* ``disk_low``: less than ``MIN_FREE_BYTES`` free on the ``DATA_DIR``
  filesystem after the write (``shutil.disk_usage``).

The refused file is counted by its code and reported in the reply counts.

Rows
----
Each held file is ONE ordinary ``documents`` row with ``file_id =
"holding:<uuid>"`` (no extra table). Why: the dashboard, the #948 previews,
search, ``view_document`` and the MCP read tools already work on
``documents`` + ``document_pages`` with the #867 / #499 patient scoping, so a
held document is visible and isolated exactly like any other once the person
signs in; the row is the single source of truth (no second table to keep in
step), and phase 3 finds every held file with ``file_id LIKE 'holding:%'`` and
matches it by ``structured_metadata.holding.sha256``. State lives in
``structured_metadata``: ``{"source": "holding", "holding": {sha256, md5,
extract, claimed_at, attempts, pages, model_pages, method, extracted_at,
error}}`` (``error`` is always a code). Each row is registered in the curated
cloud refusal at once (``curated.indexer.register_curated_ids``) and loaded
into it at startup (``db.curated_document_ids`` includes ``holding:`` rows),
the "without AI" queries of the nightly cloud pipeline exclude ``holding:``
rows, and the Files API is never called for them: their content goes to the
local model only.

Retention (#734): NOTHING here deletes or overwrites a held file or a row.
The end of an unclaimed 30-day trial (``trial_accounts.trial_ends_at``,
maintainer decision 2026-11-26) deletes nothing either: the account becomes
read-only (:func:`trial_ended`: no new writes, no model reads; sign-in, view,
download and export keep working), one end-of-trial e-mail offers connect
Drive / export / deletion by reply (``oncofiles.trial_funnel``), and erasure
happens only on request (``patient_erasure.erase_patient``, #856). TODO(#950):
the removal of a held copy after a verified move into Drive (phase 3,
``holding.moved_to_drive`` set) needs a RUNBOOK lifecycle entry and a reviewed
``tests/test_no_automatic_deletion.py`` allowlist entry (maintainer decision).
Until then a moved file's encrypted copy stays here, unused: reads go to the
Drive copy (:func:`moved_drive_id`).

:func:`holding_mode` is the one predicate "this patient's new mail goes to
the holding store" (a trial patient without a connected Drive folder that is
not converted yet). Once Drive is connected new mail goes through the #935
upload path again, and the held files are copied into the curated inbox by
``oncofiles.holding_move`` (phase 3).

Logs: document ids, counts and ``patient_id[:4]`` only, never a file name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

HOLDING_PREFIX = "holding:"
_MAGIC = b"v1"
_UUID_LEN = 36

TRIAL_MAX_BYTES = 500 * 1024 * 1024  # per trial patient
HOLDING_MAX_BYTES = 20 * 1024 * 1024 * 1024  # all trials together
MIN_FREE_BYTES = 20 * 1024 * 1024 * 1024  # free space left on DATA_DIR's filesystem

REFUSAL_CODES = ("trial_ended", "document_cap", "trial_bytes_cap", "holding_full", "disk_low")


class HoldingStoreError(RuntimeError):
    """A held file is missing, unreadable or does not decrypt."""


class HoldingRefusedError(RuntimeError):
    """A file is not stored: ``code`` is one of ``REFUSAL_CODES``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def holding_dir() -> Path:
    from oncofiles import config

    return Path(config.DATA_DIR) / "holding"


def configured() -> bool:
    """True when the mandatory ``HOLDING_STORE_SECRET`` is set."""
    from oncofiles.secrets_keys import holding_store_secret_set

    return holding_store_secret_set()


def is_holding_file_id(file_id: str | None) -> bool:
    return bool(file_id) and str(file_id).startswith(HOLDING_PREFIX)


def _uuid_of(file_id: str) -> str:
    if not is_holding_file_id(file_id):
        raise HoldingStoreError("not a holding file id")
    raw = file_id[len(HOLDING_PREFIX) :]
    try:
        parsed = uuid.UUID(raw)
    except ValueError as exc:
        raise HoldingStoreError("bad holding id") from exc
    if str(parsed) != raw or len(raw) != _UUID_LEN:
        raise HoldingStoreError("bad holding id")
    return raw


def _aad(patient_id: str, uid: str) -> bytes:
    if not patient_id:
        raise HoldingStoreError("patient id required")
    return f"holding|{patient_id}|{uid}".encode()


def _ensure_dir() -> Path:
    path = holding_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_encrypted(data: bytes, patient_id: str) -> str:
    """Encrypt ``data`` into a new held file; returns its ``holding:<uuid>`` id.

    Blocking (call through ``asyncio.to_thread``). Raises
    ``HoldingStoreSecretMissingError`` without the key, before anything is
    written. Limits are the caller's (:func:`check_limits`, before this).
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from oncofiles.secrets_keys import holding_store_key

    key = holding_store_key()  # fail closed before any write
    uid = str(uuid.uuid4())
    nonce = os.urandom(12)
    blob = _MAGIC + nonce + AESGCM(key).encrypt(nonce, data, _aad(patient_id, uid))
    folder = _ensure_dir()
    tmp = folder / f"{uid}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        os.chmod(tmp, 0o600)
    final = folder / f"{uid}.bin"
    os.replace(tmp, final)
    os.chmod(final, 0o600)
    _fsync_dir(folder)  # the rename itself is durable
    return HOLDING_PREFIX + uid


def read(file_id: str, patient_id: str) -> bytes:
    """The plaintext of a held file of ``patient_id``. Blocking; raises
    ``HoldingStoreError`` (also for another patient's id)."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from oncofiles.secrets_keys import holding_store_key

    uid = _uuid_of(file_id)
    key = holding_store_key()
    path = holding_dir() / f"{uid}.bin"
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise HoldingStoreError("missing") from exc
    if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + 12 + 16:
        raise HoldingStoreError("malformed")
    nonce = blob[len(_MAGIC) : len(_MAGIC) + 12]
    try:
        return AESGCM(key).decrypt(nonce, blob[len(_MAGIC) + 12 :], _aad(patient_id, uid))
    except InvalidTag as exc:
        raise HoldingStoreError("undecryptable") from exc


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def trial_active(trial: dict | None) -> bool:
    """True while a trial row's ``trial_ends_at`` lies in the future."""
    return bool(trial) and str(trial.get("trial_ends_at") or "") > _now_iso()


def trial_ended(trial: dict | None) -> bool:
    """True for a trial whose ``trial_ends_at`` has passed and that was not
    converted (#950, maintainer decision 2026-11-26): read-only, no model
    reads, the end-of-trial e-mail. A converted trial (Drive connected, every
    held file copied) is an ordinary patient and never "ended"; no trial row
    at all is not a trial."""
    return bool(trial) and not trial.get("converted_at") and not trial_active(trial)


async def holding_mode(db, patient_id: str) -> bool:
    """True when this patient's new mail goes to the holding store: a trial
    patient (``trial_accounts``) without a connected Drive folder. The phase 3
    seam: once Drive is connected, mail goes to the #935 upload path again."""
    trial = await db.get_trial(patient_id=patient_id)
    if trial is None or trial.get("converted_at"):
        return False  # phase 3: a converted trial is an ordinary patient
    try:
        token = await db.get_oauth_token(patient_id)
    except Exception:
        token = None
    return not (token is not None and getattr(token, "gdrive_folder_id", None))


def holding_meta(structured_metadata) -> dict:
    """``structured_metadata.holding`` of a row (a JSON string or a dict), or {}."""
    meta = structured_metadata
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except ValueError:
            return {}
    if not isinstance(meta, dict):
        return {}
    held = meta.get("holding")
    return dict(held) if isinstance(held, dict) else {}


def moved_drive_id(structured_metadata) -> str | None:
    """The Drive id of a held file's verified copy (#950 phase 3), else None.

    Once set, the row is served from that Drive copy, never from the store."""
    moved = holding_meta(structured_metadata).get("moved_to_drive")
    if isinstance(moved, dict) and isinstance(moved.get("drive_id"), str) and moved["drive_id"]:
        return moved["drive_id"]
    return None


def _free_bytes() -> int:
    folder = holding_dir()
    probe = folder if folder.exists() else folder.parent
    return shutil.disk_usage(probe).free


async def check_limits(db, patient_id: str, size: int) -> None:
    """Raise ``HoldingRefusedError`` when a file of ``size`` bytes must not be
    stored for ``patient_id``. Reads only; runs before any write."""
    from oncofiles.config import MAX_DOCUMENTS_PER_PATIENT

    if not trial_active(await db.get_trial(patient_id=patient_id)):
        raise HoldingRefusedError("trial_ended")
    if (
        MAX_DOCUMENTS_PER_PATIENT > 0
        and await db.count_documents(patient_id=patient_id) >= MAX_DOCUMENTS_PER_PATIENT
    ):
        raise HoldingRefusedError("document_cap")
    if await db.holding_bytes(patient_id=patient_id) + size > TRIAL_MAX_BYTES:
        raise HoldingRefusedError("trial_bytes_cap")
    if await db.holding_bytes() + size > HOLDING_MAX_BYTES:
        raise HoldingRefusedError("holding_full")
    if _free_bytes() - size < MIN_FREE_BYTES:
        raise HoldingRefusedError("disk_low")


async def store_document(db, patient_id: str, name: str, data: bytes, mime: str) -> int:
    """Check the limits, encrypt ``data`` into the store, register its row.

    Returns the document id. ``HoldingRefusedError`` (nothing written) when a
    limit refuses the file. The row is registered in the curated cloud refusal
    before anything else can see it; the caller starts its reading
    (``holding_extract.kick``). Raises on a missing key or an I/O error. A row
    whose insert still fails leaves its encrypted file behind (never deleted,
    #734; it holds no plaintext and no row points to it).
    """
    import asyncio

    from oncofiles.curated.indexer import register_curated_ids
    from oncofiles.models import Document, DocumentCategory

    await check_limits(db, patient_id, len(data))  # before the write: never an orphan
    sha = hashlib.sha256(data).hexdigest()
    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
    file_id = await asyncio.to_thread(write_encrypted, data, patient_id)
    meta = {
        "source": "holding",
        "holding": {"sha256": sha, "md5": md5, "extract": "pending", "attempts": 0},
    }
    doc = Document(
        file_id=file_id,
        filename=name,
        original_filename=name,
        category=DocumentCategory.OTHER,
        mime_type=mime or "application/octet-stream",
        size_bytes=len(data),
    )
    doc = await db.insert_document(doc, patient_id=patient_id)
    register_curated_ids([doc.id])  # local model only, before any other job sees it
    await db.update_structured_metadata(doc.id, json.dumps(meta, sort_keys=True))
    logger.info("holding: stored doc=%d patient %s bytes=%d", doc.id, patient_id[:4], len(data))
    return int(doc.id)
