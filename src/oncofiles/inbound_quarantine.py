"""Quarantine of inbound attachments from other senders (#950, migration 100).

Maintainer decision 2026-11-22: a document sent to a patient's document
address by ANY sender other than the patient's caregiver (the core's one
sender rule ``inbound_email.sender_refusal``: canonical caregiver match AND a
passing DKIM verdict) is not processed. Its screened attachments wait here,
encrypted, until the caregiver decides:

* the caregiver gets ONE notification e-mail (at most one per sender and
  patient per hour, at most ``MAX_NOTICES_PER_DAY`` per patient per day; a
  later digest announces what arrived meanwhile; further items only join the
  dashboard card) with an Approve and a Reject button;
* the patient's dashboard shows a "Waiting for your approval" card (caregiver
  only) with the same decisions.

Who lands here: an unknown sender (``unknown_sender``) and a mail that claims
the caregiver's From without a passing verdict (``auth_failed``: unsigned,
forged, or a DKIM refusal). Loop / bounce mail is still ignored before
(``inbound_email.loop_reason``), and the unmatched cap of the core still
applies first. The welcome@ handover of a single-patient caregiver is a
trusted caregiver mail and never comes here; welcome@ mail without a patient
token never reaches a patient address.

Screens before anything is written (the same as phase one): the archive /
signature-image / magic-byte / #935 type allowlist and size checks
(``inbound_email.screen``, which includes ``llm_input.extraction_exclusion``:
raw genotype never enters quarantine), duplicates within the mail and against
the patient's stored documents, and the caps below. Over a cap the mail is
only listed in the admin panel, as before this decision.

Caps (per patient): ``MAX_ITEMS_PER_DAY`` quarantined mails and
``MAX_FILES_PER_DAY`` files per 24 h, ``MAX_PENDING_ITEMS`` pending items,
``MAX_PENDING_BYTES`` pending bytes; globally the holding store's
``HOLDING_MAX_BYTES`` (held + quarantined bytes) and ``MIN_FREE_BYTES`` free
on the ``DATA_DIR`` filesystem.

Storage: one AES-GCM file per attachment under
``<DATA_DIR>/quarantine/<item_uuid>/<n>.bin`` (dirs 0700, files 0600, tmp +
fsync + atomic rename + directory fsync, like ``holding_store``), key
``secrets_keys.quarantine_key()`` (from the MANDATORY ``HOLDING_STORE_SECRET``;
unset = no quarantine, the mail is listed only), associated data
``quarantine|<patient_id>|<item_uuid>|file|<n>``. The sender address and the
file manifest (names, types, sizes, hashes) are AES-GCM blobs in the row
(associated data ``quarantine|<patient_id>|<item_uuid>|<field>``). A
quarantined file is NOT a document: no ``documents`` row, never read by a
model, never indexed, never in search or MCP, never previewed, not counted.

Action links (#834 exception, this purpose only): each announced item gets
two random tokens (``TOKEN_BYTES`` = 32 bytes, 256 bits, URL-safe), one per
action, valid ``TOKEN_TTL_S`` (7 days), single use. Only a keyed HMAC is
stored (``inbound_quarantine_tokens.token_hash``) together with a binding
HMAC over action + patient + item + token that is re-checked on use. A GET of
``/q/<token>`` changes nothing: it shows the sender (escaped), counts, types
and sizes (never a file name) and one confirm button; the POST to ``/q``
carries the token and a CSRF value (HMAC of the token hash) in the BODY.
A bad, used or expired token gets one identical neutral answer.

Approve files every file through the phase-one path
(``inbound_email._file_one``: ``dashboard_upload.handle_upload`` into the
curated inbox, create-only, or the holding store of a trial patient) with
the phase-one caps and duplicate checks; idempotent (a second approve does
nothing); journaled in ``activity_log``. A file that cannot be filed now
(Drive, quota) leaves the item pending: the next approve skips what is
already stored. Reject and expiry (``EXPIRE_AFTER_DAYS``) only mark the row.

Retention (#734): NOTHING here deletes a row or a file. TODO(#950): removing
the encrypted files of rejected / expired / approved items needs a RUNBOOK
lifecycle entry and a reviewed ``tests/test_no_automatic_deletion.py``
allowlist entry (maintainer decision).

No reply ever goes to the sender (no backscatter). Logs: counts, codes and
``patient_id[:4]`` only; never an address, a file name or a token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path

logger = logging.getLogger(__name__)

# ── caps and timings ─────────────────────────────────────────────────────

MAX_ITEMS_PER_DAY = 10  # quarantined mails per patient per 24 h
MAX_FILES_PER_DAY = 30  # quarantined files per patient per 24 h
MAX_PENDING_ITEMS = 20  # pending items per patient
MAX_PENDING_BYTES = 200 * 1024 * 1024  # pending bytes per patient
NOTIFY_SENDER_INTERVAL_S = 3600  # one notification per sender and patient per hour
MAX_NOTICES_PER_DAY = 3  # notification e-mails per patient per 24 h
MAX_ITEMS_PER_NOTICE = 5  # items with buttons in one e-mail; the rest: dashboard
NOTICE_RETRY_S = 900  # after a refused send
TOKEN_BYTES = 32
TOKEN_TTL_S = 7 * 86400
EXPIRE_AFTER_DAYS = 30
CLAIM_STALE_MIN = 30

LINK_BASE = "https://oncofiles.com/q/"
DASHBOARD_URL = "https://oncofiles.com/dashboard"

REASONS = ("unknown_sender", "auth_failed")

# Screen / filing codes that are final for a file: approval counts the file
# as handled (not filed) and never retries it.
PERMANENT_SKIPS = frozenset(
    {
        "duplicate",
        "type_not_allowed",
        "content_mismatch",
        "archive_refused",
        "signature_image",
        "file_too_large",
        "empty_file",
        "raw_genotype_excluded",
        "undecryptable",
    }
)

_MAGIC = b"q1"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
_SENDER_SAFE = re.compile(r"[^A-Za-z0-9.!#$%&*+/=?^_`{|}~@-]")

_last_failed: dict[str, float] = {}


# ── helpers ──────────────────────────────────────────────────────────────


def configured() -> bool:
    """True when the mandatory ``HOLDING_STORE_SECRET`` is set."""
    from oncofiles.secrets_keys import holding_store_secret_set

    return holding_store_secret_set()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def quarantine_dir() -> Path:
    from oncofiles import config

    return Path(config.DATA_DIR) / "quarantine"


def _aad(patient_id: str, item_uuid: str, part: str) -> bytes:
    if not patient_id or not item_uuid:
        raise ValueError("patient and item required")
    return f"quarantine|{patient_id}|{item_uuid}|{part}".encode()


def _encrypt(data: bytes, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from oncofiles.secrets_keys import quarantine_key

    nonce = os.urandom(12)
    return _MAGIC + nonce + AESGCM(quarantine_key()).encrypt(nonce, data, aad)


def _decrypt(blob: bytes, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from oncofiles.secrets_keys import quarantine_key

    if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + 12 + 16:
        raise ValueError("malformed")
    nonce = blob[len(_MAGIC) : len(_MAGIC) + 12]
    return AESGCM(quarantine_key()).decrypt(nonce, blob[len(_MAGIC) + 12 :], aad)


def seal(obj, patient_id: str, item_uuid: str, part: str) -> str:
    raw = json.dumps(obj, sort_keys=True).encode()
    return base64.b64encode(_encrypt(raw, _aad(patient_id, item_uuid, part))).decode()


def unseal(blob: str, patient_id: str, item_uuid: str, part: str):
    raw = _decrypt(base64.b64decode(blob), _aad(patient_id, item_uuid, part))
    return json.loads(raw)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_files(patient_id: str, item_uuid: str, datas: list[bytes]) -> None:
    """Encrypt every file of one item to disk. Blocking (``asyncio.to_thread``)."""
    root = quarantine_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    folder = root / item_uuid
    folder.mkdir(mode=0o700, exist_ok=False)
    os.chmod(folder, 0o700)
    for index, data in enumerate(datas):
        blob = _encrypt(data, _aad(patient_id, item_uuid, f"file|{index}"))
        tmp = folder / f"{index}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            os.chmod(tmp, 0o600)
        final = folder / f"{index}.bin"
        os.replace(tmp, final)
        os.chmod(final, 0o600)
    _fsync_dir(folder)
    _fsync_dir(root)


def read_file(patient_id: str, item_uuid: str, index: int) -> bytes:
    """Plaintext of one quarantined file. Blocking; raises on any mismatch."""
    try:
        uuid.UUID(item_uuid)
    except ValueError as exc:
        raise ValueError("bad item") from exc
    blob = (quarantine_dir() / item_uuid / f"{int(index)}.bin").read_bytes()
    return _decrypt(blob, _aad(patient_id, item_uuid, f"file|{int(index)}"))


def _free_bytes() -> int:
    import shutil

    folder = quarantine_dir()
    probe = folder if folder.exists() else folder.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def safe_sender(sender: str) -> str:
    """The From address as plain text for the caregiver: the bare address,
    characters outside an address dropped, at most 254 characters."""
    addr = parseaddr(sender or "")[1] or ""
    addr = _SENDER_SAFE.sub("", addr)[:254]
    return addr or "?"


def sender_hash(sender: str) -> str:
    from oncofiles.email_identity import canonical_email
    from oncofiles.secrets_keys import quarantine_sender_key

    canon = canonical_email(parseaddr(sender or "")[1] or sender or "") or ""
    return hmac.new(quarantine_sender_key(), canon.encode(), hashlib.sha256).hexdigest()


def _source_hash(th: str, msg) -> str:
    from oncofiles.secrets_keys import quarantine_sender_key

    parts = [th, msg.message_id or "", (msg.sender or "").strip().lower(), str(bool(msg.auth_pass))]
    for att in msg.attachments:
        parts.append(att.ref or hashlib.sha256(att.data or b"").hexdigest())
    return hmac.new(
        quarantine_sender_key(), ("source\n" + "\n".join(parts)).encode(), hashlib.sha256
    ).hexdigest()


def token_hash(token: str) -> str:
    from oncofiles.secrets_keys import quarantine_token_key

    return hmac.new(quarantine_token_key(), b"lookup|" + token.encode(), hashlib.sha256).hexdigest()


def _binding(action: str, patient_id: str, item_uuid: str, token: str) -> str:
    from oncofiles.secrets_keys import quarantine_token_key

    msg = f"bind|{action}|{patient_id}|{item_uuid}|{token}".encode()
    return hmac.new(quarantine_token_key(), msg, hashlib.sha256).hexdigest()


def csrf_for(token: str) -> str:
    """The confirm form's CSRF value: an HMAC of the token's hash."""
    from oncofiles.secrets_keys import quarantine_csrf_key

    return hmac.new(quarantine_csrf_key(), token_hash(token).encode(), hashlib.sha256).hexdigest()


def human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{max(1, round(n / 1024))} kB"
    return f"{n} B"


def file_type(name: str) -> str:
    from oncofiles.dashboard_upload import _ext

    return (_ext(name) or "").lstrip(".").upper() or "?"


async def _journal(db, patient_id: str, action: str, detail: dict, status: str = "ok") -> None:
    from oncofiles.models import ActivityLogEntry

    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=patient_id,
                session_id="inbound-quarantine",
                agent_id="inbound_quarantine",
                tool_name=f"inbound_quarantine_{action}",
                input_summary=json.dumps(detail, sort_keys=True),
                output_summary=status,
                status="ok" if status == "ok" else "error",
            )
        )
    except Exception:
        logger.warning("quarantine: activity_log write failed (%s)", action)


def _counters() -> Counter:
    from oncofiles import inbound_email

    return inbound_email._state.counters


# ── quarantining a mail (called by the processing core) ──────────────────


@dataclass
class Outcome:
    outcome: str  # quarantined | duplicate | listed | retry
    code: str = ""  # listed: why it was not quarantined
    temporary: str | None = None
    files: int = 0
    skipped: Counter = field(default_factory=Counter)


async def quarantine_mail(db, patient_id: str, th: str, msg, reason: str, progress=None) -> Outcome:
    """Quarantine the attachments of a mail the sender rule refused.

    Runs inside the core (``PROCESS_LOCK`` and the patient lock held). A
    temporary download failure of a queued mail returns ``retry`` with nothing
    written; the source hash makes a later attempt a no-op once stored.
    """
    from oncofiles import holding_store, inbound_email

    pp = patient_id[:4]
    if not configured():
        return Outcome("listed", "quarantine_off")
    if not msg.attachments:
        return Outcome("listed", "no_attachments")
    source = _source_hash(th, msg)
    if await db.quarantine_source_exists(source, patient_id=patient_id):
        return Outcome("duplicate")
    now = _now()
    usage = await db.quarantine_usage(patient_id=patient_id, since=_iso(now - timedelta(days=1)))
    if usage["items_today"] >= MAX_ITEMS_PER_DAY:
        return Outcome("listed", "quarantine_daily_cap")
    if usage["pending_items"] >= MAX_PENDING_ITEMS:
        return Outcome("listed", "quarantine_pending_cap")
    declared = sum((a.declared_size or len(a.data or b"")) for a in msg.attachments)
    if usage["pending_bytes"] + min(declared, inbound_email.MAX_BYTES_PER_MAIL) > MAX_PENDING_BYTES:
        return Outcome("listed", "quarantine_bytes_cap")

    skipped: Counter = Counter(msg.parse_skips)
    temporary: set[str] | None = set() if progress is not None else None
    await inbound_email._load_referenced(msg, skipped, progress, temporary)
    if temporary and not progress.final:
        return Outcome("retry", temporary=sorted(temporary)[0])

    seen: set[str] = set()
    files: list[dict] = []
    datas: list[bytes] = []
    total = 0
    for i, att in enumerate(msg.attachments):
        checked = inbound_email.screen(att, i)
        if isinstance(checked, str):
            skipped[checked] += 1
            continue
        name, mime = checked
        sha = hashlib.sha256(att.data).hexdigest()
        md5 = hashlib.md5(att.data, usedforsecurity=False).hexdigest()
        if sha in seen or await db.document_md5_exists(md5, patient_id=patient_id):
            skipped["duplicate"] += 1
            continue
        seen.add(sha)
        if len(files) >= inbound_email.MAX_FILES_PER_MAIL:
            skipped["too_many_files"] += 1
            continue
        if total + len(att.data) > inbound_email.MAX_BYTES_PER_MAIL:
            skipped["mail_too_large"] += 1
            continue
        total += len(att.data)
        files.append({"name": name, "mime": mime, "size": len(att.data), "sha256": sha, "md5": md5})
        datas.append(att.data)
    if not files:
        return Outcome("listed", "nothing_to_hold", skipped=skipped)
    if usage["files_today"] + len(files) > MAX_FILES_PER_DAY:
        return Outcome("listed", "quarantine_file_cap", skipped=skipped)
    if usage["pending_bytes"] + total > MAX_PENDING_BYTES:
        return Outcome("listed", "quarantine_bytes_cap", skipped=skipped)
    held = await db.holding_bytes() + await db.quarantine_bytes_total()
    if held + total > holding_store.HOLDING_MAX_BYTES:
        return Outcome("listed", "holding_full", skipped=skipped)
    if _free_bytes() - total < holding_store.MIN_FREE_BYTES:
        return Outcome("listed", "disk_low", skipped=skipped)

    item_uuid = str(uuid.uuid4())
    await asyncio.to_thread(_write_files, patient_id, item_uuid, datas)
    row_id = await db.insert_quarantine_item(
        patient_id=patient_id,
        item_uuid=item_uuid,
        source_hash=source,
        sender_hash=sender_hash(msg.sender),
        sender_enc=seal({"sender": safe_sender(msg.sender)}, patient_id, item_uuid, "sender"),
        files_enc=seal(files, patient_id, item_uuid, "files"),
        refusal_reason=reason if reason in REASONS else "unknown_sender",
        file_count=len(files),
        total_bytes=total,
    )
    if row_id is None:
        # Same delivery stored meanwhile: the files just written are unused
        # (encrypted, no row points to them; never deleted, #734).
        return Outcome("duplicate")
    _counters()["quarantined_mails"] += 1
    _counters()["quarantined_files"] += len(files)
    logger.info(
        "quarantine: patient %s item=%d files=%d skipped=%d reason=%s",
        pp,
        row_id,
        len(files),
        sum(skipped.values()),
        reason,
    )
    try:
        await notify_due(db, patient_id)
    except Exception as exc:  # noqa: BLE001 - the item is stored; the sweep retries
        logger.warning("quarantine: notify failed patient %s (%s)", pp, type(exc).__name__)
    return Outcome("quarantined", files=len(files), skipped=skipped)


# ── notification e-mail ──────────────────────────────────────────────────


def _recipient(patient) -> str:
    raw = str(getattr(patient, "caregiver_email", "") or "")
    return parseaddr(raw.split(",", 1)[0].strip())[1]


def notice_text(entries: list[dict], more: int) -> tuple[str, str]:
    """(subject, body) of the notification: sender address as plain text,
    counts, types and sizes, the two links per item. Never a patient name, a
    file name, the mail's subject or any text of the mail."""
    subject = "Oncofiles: príloha čaká na vaše schválenie / attachment waiting for your approval"
    sk = [
        "Dobrý deň,",
        "",
        "niekto poslal prílohy na adresu pre dokumenty vo vašom Oncofiles.",
        "Kým ich neschválite, nič sa s nimi nedeje.",
    ]
    en = [
        "Hello,",
        "",
        "someone sent attachments to the document address of your Oncofiles.",
        "Nothing happens to them until you approve them.",
    ]
    for e in entries:
        types = ", ".join(f"{t} {human_size(s)}" for t, s in e["files"])
        sk += ["", f"Odosielateľ: {e['sender']}", f"Príloh: {e['count']} ({types})"]
        en += ["", f"Sender: {e['sender']}", f"Attachments: {e['count']} ({types})"]
        if e["reason"] == "auth_failed":
            sk.append("Pozor: odosielateľa sa nepodarilo overiť.")
            en.append("Warning: the sender could not be verified.")
        sk += [f"Schváliť: {e['approve']}", f"Odmietnuť: {e['reject']}"]
        en += [f"Approve: {e['approve']}", f"Reject: {e['reject']}"]
    if more:
        sk += ["", f"Ďalšie čakajúce prílohy na schválenie: {more} (v Oncofiles)."]
        en += ["", f"More attachments waiting for approval: {more} (in Oncofiles)."]
    sk += [
        "",
        "Ak odosielateľa nepoznáte, prílohy odmietnite. Odkazy platia 7 dní;",
        "rozhodnúť môžete aj po prihlásení: " + DASHBOARD_URL,
    ]
    en += [
        "",
        "If you do not know the sender, reject them. The links are valid for 7 days;",
        "you can also decide after signing in: " + DASHBOARD_URL,
    ]
    body = "\n".join(sk) + "\n\n---\n\n" + "\n".join(en) + "\n\nOncofiles\n"
    return subject, body


async def _issue_tokens(db, item: dict) -> dict[str, str]:
    expires = _iso(_now() + timedelta(seconds=TOKEN_TTL_S))
    links = {}
    for action in ("approve", "reject"):
        token = secrets.token_urlsafe(TOKEN_BYTES)
        await db.insert_quarantine_token(
            patient_id=item["patient_id"],
            quarantine_id=item["id"],
            action=action,
            token_hash=token_hash(token),
            binding_hash=_binding(action, item["patient_id"], item["item_uuid"], token),
            expires_at=expires,
        )
        links[action] = LINK_BASE + token
    return links


def _item_view(item: dict) -> tuple[str, list[dict]]:
    pid, uid = item["patient_id"], item["item_uuid"]
    sender = unseal(item["sender_enc"], pid, uid, "sender").get("sender") or "?"
    files = unseal(item["files_enc"], pid, uid, "files")
    return sender, files


async def notify_due(db, patient_id: str) -> int:
    """Send the caregiver the notification for unannounced pending items.

    At most one e-mail per sender and patient per ``NOTIFY_SENDER_INTERVAL_S``
    (an item whose sender was announced within it waits for the digest of
    the next sweep) and ``MAX_NOTICES_PER_DAY`` per patient; items older than
    a day that no e-mail announced stay dashboard-only. Returns 1 when sent.
    """
    from oncofiles import mailer

    if not configured():
        return 0
    pp = patient_id[:4]
    now = _now()
    mono = time.monotonic()
    failed = _last_failed.get(patient_id)
    if failed is not None and mono - failed < NOTICE_RETRY_S:
        return 0
    day_ago = _iso(now - timedelta(days=1))
    items = await db.quarantine_unnotified(patient_id=patient_id, since=day_ago)
    if not items:
        return 0
    if await db.quarantine_notices_since(patient_id=patient_id, since=day_ago) >= (
        MAX_NOTICES_PER_DAY
    ):
        _counters()["quarantine_notice_daily_cap"] += 1
        return 0
    hour_ago = _iso(now - timedelta(seconds=NOTIFY_SENDER_INTERVAL_S))
    last_by_sender: dict[str, str | None] = {}
    due = []
    for it in items:
        sh = it["sender_hash"]
        if sh not in last_by_sender:
            last_by_sender[sh] = await db.quarantine_last_notice_for_sender(
                patient_id=patient_id, sender_hash=sh
            )
        last = last_by_sender[sh]
        if last and last >= hour_ago:
            continue
        due.append(it)
    if not due:
        _counters()["quarantine_notice_throttled"] += 1
        return 0
    patient = await db.get_patient(patient_id)
    to = _recipient(patient) if patient is not None else ""
    if not to:
        return 0
    notice_key = secrets.token_hex(8)
    at = _iso(now)
    if not await db.mark_quarantine_notified(
        [it["id"] for it in due], patient_id=patient_id, at=at, notice_key=notice_key
    ):
        return 0
    entries = []
    for it in due[:MAX_ITEMS_PER_NOTICE]:
        try:
            sender, files = _item_view(it)
        except Exception:
            logger.warning("quarantine: item %d metadata unreadable", it["id"])
            continue
        links = await _issue_tokens(db, it)
        entries.append(
            {
                "sender": sender,
                "count": len(files),
                "files": [(file_type(f["name"]), int(f["size"])) for f in files],
                "reason": it["refusal_reason"],
                **links,
            }
        )
    subject, body = notice_text(entries, len(due) - len(entries))
    ok = await mailer.send_mail(
        [to],
        subject,
        body,
        tags=["inbound_quarantine"],
        headers={"Auto-Submitted": "auto-generated"},
    )
    if not ok:
        await db.clear_quarantine_notice(patient_id=patient_id, notice_key=notice_key)
        _last_failed[patient_id] = mono
        _counters()["quarantine_notice_failed"] += 1
        logger.info("quarantine: notice for patient %s not sent, retried later", pp)
        return 0
    _last_failed.pop(patient_id, None)
    _counters()["quarantine_notice_sent"] += 1
    logger.info("quarantine: notice sent patient %s items=%d", pp, len(due))
    return 1


async def run_maintenance(db) -> dict:
    """Scheduler sweep (from the inbound queue job): expire old pending items
    (mark only) and send the due digests. Returns counts."""
    if not configured():
        return {"expired": 0, "notices": 0}
    now = _now()
    expired = await db.expire_quarantine_items(
        before=_iso(now - timedelta(days=EXPIRE_AFTER_DAYS)),
        stale_before=_iso(now - timedelta(minutes=CLAIM_STALE_MIN)),
    )
    if expired:
        _counters()["quarantine_expired"] += expired
        logger.info("quarantine: expired=%d", expired)
    notices = 0
    for pid in await db.quarantine_patients_due(since=_iso(now - timedelta(days=1))):
        try:
            notices += await notify_due(db, pid)
        except Exception as exc:  # noqa: BLE001 - one patient never stops the sweep
            logger.warning("quarantine: notify failed patient %s (%s)", pid[:4], type(exc).__name__)
    return {"expired": expired, "notices": notices}


# ── decisions ────────────────────────────────────────────────────────────


async def _address_hash(db, patient_id: str) -> str:
    from oncofiles import inbound_email

    row = await db.get_active_inbound_address(patient_id=patient_id)
    if row is None:
        return "quarantine:" + patient_id
    return inbound_email.token_hash(inbound_email.derive_token(patient_id, int(row["version"])))


async def approve(db, patient_id: str, item_id: int, *, decided_by: str, via: str) -> dict:
    """File the item's attachments as if the caregiver had sent them.

    Serialized with mail processing (``PROCESS_LOCK``, the patient lock).
    Result codes: ``approved``, ``already`` (approved before: nothing done),
    ``not_found``, ``not_pending`` (rejected / expired), ``busy`` (another
    approval holds it), ``retry_later`` (``code``: something could not be
    filed now; the item stays pending, what was filed stays filed).
    """
    from oncofiles import inbound_email

    async with inbound_email.PROCESS_LOCK, inbound_email._state.lock(patient_id):
        return await _approve_locked(db, patient_id, int(item_id), decided_by, via)


async def _approve_locked(db, pid: str, item_id: int, decided_by: str, via: str) -> dict:
    from oncofiles import holding_store, inbound_email, inbound_welcome

    pp = pid[:4]
    now = _now()
    lease = _iso(now)
    stale = _iso(now - timedelta(minutes=CLAIM_STALE_MIN))
    if not await db.claim_quarantine_item(item_id, patient_id=pid, now=lease, stale_before=stale):
        item = await db.get_quarantine_item(item_id, patient_id=pid)
        if item is None:
            return {"result": "not_found"}
        if item["state"] == "approved":
            return {"result": "already"}
        if item["state"] == "pending":
            return {"result": "busy"}
        return {"result": "not_pending", "state": item["state"]}

    filed = 0
    skipped: Counter = Counter()
    leftover: str | None = None
    try:
        item = await db.get_quarantine_item(item_id, patient_id=pid)
        files = unseal(item["files_enc"], pid, item["item_uuid"], "files")
        th = await _address_hash(db, pid)
        ts = time.time()
        holding = await holding_store.holding_mode(db, pid)
        # the phase-one caps, as for a caregiver mail
        if holding and (
            await inbound_welcome.trial_mails_over_cap(db, pid, including_this=False)
            or inbound_email._over_cap(
                inbound_email._state.trial_mails,
                th,
                ts,
                inbound_welcome.MAX_MAILS_PER_SENDER_DAY,
                inbound_welcome.MAX_MAILS_PER_SENDER_DAY,
            )
        ):
            leftover = "trial_capped"
        elif inbound_email._rate_limited(th, ts):
            leftover = "rate_limited"
        today = inbound_email._files_today(th, ts)
        file_cap = inbound_email.MAX_FILES_PER_DAY
        if holding:
            today = max(today, await inbound_welcome.trial_files_today(db, pid), 0)
            file_cap = inbound_welcome.MAX_FILES_PER_SENDER_DAY
        stored: set[str] | None = None
        recent = inbound_email._state.recent.setdefault(pid, OrderedDict())
        for index, meta in enumerate(files):
            if leftover:
                break
            try:
                data = await asyncio.to_thread(read_file, pid, item["item_uuid"], index)
            except Exception:
                skipped["undecryptable"] += 1
                continue
            if hashlib.sha256(data).hexdigest() != meta.get("sha256"):
                skipped["undecryptable"] += 1
                continue
            md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
            if not holding and stored is None:
                stored = await inbound_email._stored_md5s(db, pid)
            if (stored and md5 in stored) or await db.document_md5_exists(md5, patient_id=pid):
                skipped["duplicate"] += 1
                continue
            if today + filed >= file_cap:
                leftover = "daily_file_cap"
                break
            code = await inbound_email._file_one(db, pid, meta["name"], data)
            if code == "uploaded":
                filed += 1
                inbound_email._remember(recent, meta["sha256"], inbound_email.RECENT_MAX)
                continue
            if code in PERMANENT_SKIPS:
                skipped[code] += 1
                continue
            leftover = code
    except Exception as exc:  # noqa: BLE001 - the item stays pending
        leftover = leftover or "store_failed"
        logger.warning("quarantine: approve patient %s failed (%s)", pp, type(exc).__name__)
    if filed:
        inbound_email._state.files_day.setdefault(th, []).append((time.time(), filed))
    detail = {
        "item_id": item_id,
        "via": via,
        "filed": filed,
        "skipped": dict(skipped),
        "caller_email_hash": decided_by,
    }
    if leftover:
        await db.release_quarantine_claim(item_id, patient_id=pid, lease=lease)
        await _journal(db, pid, "approve", {**detail, "code": leftover}, status="retry_later")
        logger.info(
            "quarantine: approve patient %s item=%d filed=%d retry_later=%s",
            pp,
            item_id,
            filed,
            leftover,
        )
        return {"result": "retry_later", "code": leftover, "filed": filed}
    ok = await db.decide_quarantine_item(
        item_id,
        patient_id=pid,
        state="approved",
        at=_iso(_now()),
        decided_by=decided_by,
        decided_via=via,
        filed_count=filed,
        lease=lease,
    )
    if not ok:
        return {"result": "busy"}
    await _journal(db, pid, "approve", detail)
    _counters()["quarantine_approved"] += 1
    logger.info("quarantine: approved patient %s item=%d filed=%d", pp, item_id, filed)
    return {"result": "approved", "filed": filed, "skipped": dict(skipped)}


async def reject(db, patient_id: str, item_id: int, *, decided_by: str, via: str) -> dict:
    """Mark the item rejected (nothing filed, nothing deleted)."""
    from oncofiles import inbound_email

    async with inbound_email.PROCESS_LOCK, inbound_email._state.lock(patient_id):
        now = _now()
        ok = await db.decide_quarantine_item(
            int(item_id),
            patient_id=patient_id,
            state="rejected",
            at=_iso(now),
            decided_by=decided_by,
            decided_via=via,
            stale_before=_iso(now - timedelta(minutes=CLAIM_STALE_MIN)),
        )
        if not ok:
            item = await db.get_quarantine_item(int(item_id), patient_id=patient_id)
            if item is None:
                return {"result": "not_found"}
            if item["state"] == "rejected":
                return {"result": "already"}
            if item["state"] == "pending":
                return {"result": "busy"}
            return {"result": "not_pending", "state": item["state"]}
        await _journal(
            db,
            patient_id,
            "reject",
            {"item_id": int(item_id), "via": via, "caller_email_hash": decided_by},
        )
    _counters()["quarantine_rejected"] += 1
    logger.info("quarantine: rejected patient %s item=%d", patient_id[:4], int(item_id))
    return {"result": "rejected"}


# ── action links (/q/<token>) ────────────────────────────────────────────


async def resolve_link(db, token: str) -> dict | None:
    """The live token row for ``token`` (pending item, unused, unexpired,
    binding intact), else None. Reads only."""
    if not configured() or not isinstance(token, str) or not _TOKEN_RE.match(token):
        return None
    row = await db.find_quarantine_token(token_hash(token))
    if row is None or row["used_at"] or row["state"] != "pending":
        return None
    if str(row["expires_at"]) <= _iso(_now()):
        return None
    expected = _binding(row["action"], row["patient_id"], row["item_uuid"], token)
    if not hmac.compare_digest(expected, row["binding_hash"]):
        return None
    return row


async def link_view(db, token: str) -> dict | None:
    """What the confirm page may show: action, sender (escaped by the page),
    count, types and sizes. Never a file name. None = the neutral answer."""
    row = await resolve_link(db, token)
    if row is None:
        return None
    item = await db.get_quarantine_item(row["quarantine_id"], patient_id=row["patient_id"])
    if item is None:
        return None
    try:
        sender, files = _item_view(item)
    except Exception:
        return None
    return {
        "action": row["action"],
        "sender": sender,
        "count": len(files),
        "files": [(file_type(f["name"]), human_size(int(f["size"]))) for f in files],
        "reason": item["refusal_reason"],
        "csrf": csrf_for(token),
    }


async def act_on_link(db, token: str, csrf: str) -> str:
    """POST /q: decide through a link. Returns ``approved`` / ``rejected`` /
    ``retry_later`` / ``invalid`` (bad, used, expired or a wrong CSRF value:
    one neutral answer)."""
    from oncofiles.secrets_keys import caller_email_hash

    if not configured() or not isinstance(token, str) or not _TOKEN_RE.match(token):
        return "invalid"
    if not isinstance(csrf, str) or not hmac.compare_digest(csrf_for(token), csrf):
        return "invalid"
    row = await resolve_link(db, token)
    if row is None:
        return "invalid"
    pid = row["patient_id"]
    used_at = _iso(_now())
    if not await db.use_quarantine_token(row["token_id"], patient_id=pid, at=used_at):
        return "invalid"
    patient = await db.get_patient(pid)
    decided_by = caller_email_hash(_recipient(patient)) if patient is not None else None
    decided_by = decided_by or "email_link"
    if row["action"] == "approve":
        result = await approve(
            db, pid, row["quarantine_id"], decided_by=decided_by, via="email_link"
        )
    else:
        result = await reject(
            db, pid, row["quarantine_id"], decided_by=decided_by, via="email_link"
        )
    code = result["result"]
    if code in ("approved", "rejected"):
        return code
    if code in ("retry_later", "busy"):
        await db.release_quarantine_token(row["token_id"], patient_id=pid, at=used_at)
        return "retry_later"
    return "invalid"


# ── dashboard ────────────────────────────────────────────────────────────


async def dashboard_view(db, patient_id: str, *, caregiver: bool) -> dict:
    """The card's data. The caregiver sees each pending item (sender, time,
    file names, types, sizes); anyone else (an admin's read-only view) only
    the count."""
    if not configured():
        return {"enabled": False, "pending": 0, "items": []}
    if not caregiver:
        return {
            "enabled": True,
            "read_only": True,
            "pending": await db.count_quarantine_pending(patient_id=patient_id),
            "items": [],
        }
    out = []
    for item in await db.list_quarantine_items(patient_id=patient_id, limit=MAX_PENDING_ITEMS):
        try:
            sender, files = _item_view(item)
        except Exception:
            logger.warning("quarantine: item %d metadata unreadable", item["id"])
            continue
        out.append(
            {
                "id": item["id"],
                "sender": sender,
                "received_at": item["received_at"],
                "reason": item["refusal_reason"],
                "files": [
                    {"name": f["name"], "type": file_type(f["name"]), "size": int(f["size"])}
                    for f in files
                ],
                "total_bytes": int(item["total_bytes"]),
            }
        )
    return {"enabled": True, "read_only": False, "pending": len(out), "items": out}
