"""Quarantine of inbound attachments from other senders (#950, migration 101).

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
``MAX_PENDING_BYTES`` pending bytes and ``MAX_RECEIVED_BYTES_30D`` bytes
received in 30 days (every state: a reject-and-resend loop cannot grow the
store, since nothing is deleted); globally its OWN budget
``QUARANTINE_MAX_PENDING_BYTES`` over pending items only (never the trial
holding store's ``HOLDING_MAX_BYTES``, so a stranger's flood never blocks a
trial) and the holding store's ``MIN_FREE_BYTES`` free-space guard on the
``DATA_DIR`` filesystem. Retained bytes of rejected / expired items are shown
in the admin overview (``admin_overview``): what the purge frees after the
trash.

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

Retention (#734, maintainer decision 2026-11-22, RUNBOOK lifecycle): no row
is ever deleted. The only deletion is ``purge_due`` (from ``run_maintenance``,
the inbound queue job), allowlisted in ``tests/test_no_automatic_deletion.py``:
it removes ONLY the encrypted ``<n>.bin`` files of one item (named by the
row's ``file_count``) and then that item's empty directory (``os.rmdir``,
never a recursive delete, never a glob), after checking the directory is a
real directory directly inside the quarantine root named exactly the row's
``item_uuid``. Rejected / expired items ``TRASH_DAYS`` after ``decided_at``
(the restore window ends there) → state ``purged`` + ``files_purged_at``;
approved items whose every file was filed only with
``QUARANTINE_PURGE_APPROVED=true`` (default off)
``QUARANTINE_PURGE_APPROVED_AFTER_DAYS`` after approval → state stays
``approved`` + ``files_purged_at``. Pending items (a restored item is
pending) are never touched. The row and its encrypted sender / manifest
blobs stay.

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
import stat
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
# bytes received per patient in a rolling 30 days, whatever the state (a
# rejected or expired item keeps its encrypted file through its 30-day trash)
MAX_RECEIVED_BYTES_30D = 500 * 1024 * 1024
# all patients' PENDING bytes; separate from the trial holding store's budget
QUARANTINE_MAX_PENDING_BYTES = 2 * 1024 * 1024 * 1024
# all patients' bytes still ON DISK in the quarantine store (security review
# of 15e2ae0): pending, rejected, expired AND approved (approval copies the
# file into the patient's documents; the encrypted quarantine copy stays)
# until ``purge_due`` removes the files (``files_purged_at``, migration 102).
QUARANTINE_MAX_RETAINED_BYTES = 5 * 1024 * 1024 * 1024
# quarantine stops accepting mail at this free space, well above the
# holding store's MIN_FREE_BYTES (20 GB), so trials never hit disk_low first
QUARANTINE_MIN_FREE_BYTES = 30 * 1024 * 1024 * 1024
NOTIFY_SENDER_INTERVAL_S = 3600  # one notification per sender and patient per hour
MAX_NOTICES_PER_DAY = 3  # notification e-mails per patient per 24 h
MAX_ITEMS_PER_NOTICE = 5  # items with buttons in one e-mail; the rest: dashboard
NOTICE_RETRY_S = 900  # after a refused send
TOKEN_BYTES = 32
TOKEN_TTL_S = 7 * 86400
EXPIRE_AFTER_DAYS = 30
# rejected / expired items stay restorable this long (maintainer decision
# 2026-11-22); the later purge removes only their encrypted files
TRASH_DAYS = 30
CLAIM_STALE_MIN = 30
# purge (migration 102): at most this many items per run, committed per item
PURGE_MAX_ITEMS = 200
# approved copies: env QUARANTINE_PURGE_APPROVED (default false, pending the
# maintainer's approval) and QUARANTINE_PURGE_APPROVED_AFTER_DAYS (default 7)
PURGE_APPROVED_AFTER_DAYS = 7

LINK_BASE = "https://oncofiles.com/q/"
DASHBOARD_URL = "https://oncofiles.com/dashboard"
# mailer kind and Brevo `X-Mailin-Tag` of the notification (exclude it
# from click tracking in Brevo's settings; see notify_due)
QUARANTINE_MAIL_TAG = "inbound_quarantine"

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
# address characters only, and none that shapes a URL (/ ? = & %): the
# sender is shown as plain text and must never turn into a link target
_SENDER_SAFE = re.compile(r"[^A-Za-z0-9.!#$*+^_`{|}~@-]")

_last_failed: dict[str, float] = {}
# the last purge run that found something to do (admin overview, /readiness)
_purge_last: dict = {}


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


def recipient_hash(caregiver_email: str | None) -> str:
    """Keyed hash of the canonical first caregiver address (the link's
    recipient). '' when there is none: such a link never resolves."""
    from oncofiles.email_identity import canonical_email
    from oncofiles.secrets_keys import quarantine_token_key

    first = parseaddr(str(caregiver_email or "").split(",", 1)[0].strip())[1]
    canon = canonical_email(first) if first else ""
    if not canon:
        return ""
    return hmac.new(
        quarantine_token_key(), b"recipient|" + canon.encode(), hashlib.sha256
    ).hexdigest()


def _binding(action: str, patient_id: str, item_uuid: str, recipient: str, token: str) -> str:
    from oncofiles.secrets_keys import quarantine_token_key

    msg = f"bind|{action}|{patient_id}|{item_uuid}|{recipient}|{token}".encode()
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
    from oncofiles import inbound_email

    pp = patient_id[:4]
    if not configured():
        return Outcome("listed", "quarantine_off")
    if not msg.attachments:
        return Outcome("listed", "no_attachments")
    source = _source_hash(th, msg)
    if await db.quarantine_source_exists(source, patient_id=patient_id):
        return Outcome("duplicate")
    now = _now()
    usage = await db.quarantine_usage(
        patient_id=patient_id,
        since=_iso(now - timedelta(days=1)),
        since_30d=_iso(now - timedelta(days=30)),
    )
    if usage["items_today"] >= MAX_ITEMS_PER_DAY:
        return Outcome("listed", "quarantine_daily_cap")
    if usage["pending_items"] >= MAX_PENDING_ITEMS:
        return Outcome("listed", "quarantine_pending_cap")
    declared = sum((a.declared_size or len(a.data or b"")) for a in msg.attachments)
    declared = min(declared, inbound_email.MAX_BYTES_PER_MAIL)
    if usage["pending_bytes"] + declared > MAX_PENDING_BYTES:
        return Outcome("listed", "quarantine_bytes_cap")
    if usage["received_bytes_30d"] + declared > MAX_RECEIVED_BYTES_30D:
        return Outcome("listed", "quarantine_received_cap")

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
    if usage["received_bytes_30d"] + total > MAX_RECEIVED_BYTES_30D:
        return Outcome("listed", "quarantine_received_cap", skipped=skipped)
    if await db.quarantine_pending_bytes_total() + total > QUARANTINE_MAX_PENDING_BYTES:
        return Outcome("listed", "quarantine_full", skipped=skipped)
    if await db.quarantine_retained_bytes_total() + total > QUARANTINE_MAX_RETAINED_BYTES:
        return Outcome("listed", "quarantine_full", skipped=skipped)
    if _free_bytes() - total < QUARANTINE_MIN_FREE_BYTES:
        return Outcome("listed", "quarantine_disk_low", skipped=skipped)

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
            # infra review of #970: the From may be the caregiver's OWN address
            # (forged). No approve link: only reject here; approving needs the
            # signed-in dashboard, which shows the warning.
            sk += [
                "POZOR: správa sa tvári, že je od vás, ale váš poskytovateľ pošty ju nepodpísal.",
                "Ak ste ju neposlali vy, odmietnite ju. Schváliť sa dá len po prihlásení: "
                + DASHBOARD_URL,
                f"Odmietnuť: {e['reject']}",
            ]
            en += [
                "WARNING: this mail claims to be from you, but it is not signed by your "
                "mail provider.",
                "If you did not send it, reject it. Approving is possible only after "
                "signing in: " + DASHBOARD_URL,
                f"Reject: {e['reject']}",
            ]
            continue
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


def link_actions(refusal_reason: str) -> tuple[str, ...]:
    """The e-mail actions of an item: an unverified claim of the caregiver's
    own address (``auth_failed``) gets NO approve link (infra review of #970)."""
    return ("reject",) if refusal_reason == "auth_failed" else ("approve", "reject")


async def _issue_tokens(db, item: dict, recipient: str) -> dict[str, str]:
    """The links for ``item`` (``link_actions``), bound to the caregiver
    address they go to (``recipient`` = ``recipient_hash`` of it)."""
    expires = _iso(_now() + timedelta(seconds=TOKEN_TTL_S))
    links = {}
    for action in link_actions(item["refusal_reason"]):
        token = secrets.token_urlsafe(TOKEN_BYTES)
        await db.insert_quarantine_token(
            patient_id=item["patient_id"],
            quarantine_id=item["id"],
            action=action,
            token_hash=token_hash(token),
            binding_hash=_binding(action, item["patient_id"], item["item_uuid"], recipient, token),
            recipient_hash=recipient,
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
    recipient = recipient_hash(to)
    if not to or not recipient:
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
        links = await _issue_tokens(db, it, recipient)
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
    # Click tracking (infra review of #970): Brevo documents NO SMTP header that
    # turns click tracking off per message (searched 2026-11-22:
    # https://developers.brevo.com/changelog only has `removeTrackingPixel`,
    # API, Enterprise, open pixel only; https://help.brevo.com/hc/en-us/articles/11643306229906
    # only an account-wide anonymisation). The mailer puts every tag into
    # `X-Mailin-Tag`, so these mails carry the recognisable tag
    # `inbound_quarantine` for infra to exclude from tracking in Brevo's
    # settings. The tokens are single use and bound to the caregiver anyway.
    ok = await mailer.send_mail(
        [to],
        subject,
        body,
        tags=[QUARANTINE_MAIL_TAG],
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


# ── purge of encrypted files after the trash (migration 102) ─────────────


def purge_approved_enabled() -> bool:
    """``QUARANTINE_PURGE_APPROVED`` (default false): only then are the
    encrypted copies of approved, fully filed items ever removed."""
    raw = os.environ.get("QUARANTINE_PURGE_APPROVED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def purge_approved_after_days() -> int:
    """``QUARANTINE_PURGE_APPROVED_AFTER_DAYS`` (default 7, at least 1)."""
    raw = os.environ.get("QUARANTINE_PURGE_APPROVED_AFTER_DAYS", "").strip()
    try:
        days = int(raw) if raw else PURGE_APPROVED_AFTER_DAYS
    except ValueError:
        days = PURGE_APPROVED_AFTER_DAYS
    return max(1, days)


@dataclass
class _ItemPurge:
    refused: bool = False
    removed: int = 0
    missing: int = 0
    bytes: int = 0
    dir_kept: bool = False


def _purge_item_files(item_uuid: str, file_count: int) -> _ItemPurge:
    """Remove the encrypted files of ONE item, then its empty directory.

    Blocking (``asyncio.to_thread``). Only ``<root>/<item_uuid>/<n>.bin`` for
    ``n`` in ``range(file_count)`` and that directory itself: no glob, no
    recursive delete. Refused (nothing removed) when the uuid is not in its
    canonical form, the directory is a symlink or not a directory, it does
    not resolve to a direct child of the quarantine root with that very name,
    or one of the named files is not a regular file. Missing files are
    counted and tolerated (a crash between the file removal and the row
    update leaves exactly that). A leftover we did not name (e.g. the
    ``.tmp`` of a crashed write) keeps the directory (``dir_kept``).
    """
    out = _ItemPurge()
    try:
        canonical = str(uuid.UUID(str(item_uuid)))
    except (ValueError, TypeError, AttributeError):
        return _ItemPurge(refused=True)
    if canonical != item_uuid or int(file_count) < 0:
        return _ItemPurge(refused=True)
    root = quarantine_dir().resolve()
    folder = root / item_uuid
    if not os.path.lexists(folder):
        out.missing = int(file_count)
        return out
    if folder.is_symlink() or not folder.is_dir():
        return _ItemPurge(refused=True)
    real = folder.resolve(strict=True)
    if real.parent != root or real.name != item_uuid:
        return _ItemPurge(refused=True)
    targets: list[tuple[Path, int]] = []
    for index in range(int(file_count)):
        path = real / f"{index}.bin"
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            out.missing += 1
            continue
        if not stat.S_ISREG(info.st_mode):
            return _ItemPurge(refused=True)
        targets.append((path, info.st_size))
    for path, size in targets:
        try:
            os.remove(path)  # one named file of this item (checked above)
        except FileNotFoundError:
            out.missing += 1
            continue
        out.removed += 1
        out.bytes += size
    try:
        os.rmdir(real)  # only when empty; never a recursive delete
    except FileNotFoundError:
        pass
    except OSError:
        out.dir_kept = True
    return out


async def purge_due(db, *, limit: int = PURGE_MAX_ITEMS) -> dict:
    """Remove the encrypted files of items past their lifecycle (module
    docstring) and mark each row; at most ``limit`` items, committed per
    item. The caller holds ``inbound_email.PROCESS_LOCK`` (the scheduler
    sweep), so approve / reject / restore never interleave; each item also
    takes its patient lock. Files first, then the conditional row update: a
    crash in between leaves a row the next run finds again (its missing files
    are tolerated). Returns counts only."""
    from oncofiles import inbound_email

    counts: Counter = Counter()
    if not configured():
        return {}
    now = _now()
    at = _iso(now)
    # the restore window is decided_at >= now - TRASH_DAYS: disjoint from this
    trash_before = _iso(now - timedelta(days=TRASH_DAYS))
    approved_on = purge_approved_enabled()
    approved_before = (
        _iso(now - timedelta(days=purge_approved_after_days())) if approved_on else None
    )
    candidates = await db.quarantine_purge_candidates(
        trash_before=trash_before, approved_before=approved_before, limit=limit
    )
    for item in candidates:
        pid = item["patient_id"]
        item_id = int(item["id"])
        if item["state"] == "approved":
            if not approved_on or approved_before is None:  # belt and braces
                continue
            kind, before = "approved", approved_before
        elif item["state"] in ("rejected", "expired"):
            kind, before = "trash", trash_before
        else:  # never a pending item, whatever the query returned
            continue
        async with inbound_email._state.lock(pid):
            try:
                res = await asyncio.to_thread(
                    _purge_item_files, item["item_uuid"], int(item["file_count"])
                )
            except Exception as exc:  # noqa: BLE001 - one item never stops the sweep
                counts["purge_failed"] += 1
                logger.warning(
                    "quarantine: purge failed patient %s item=%d (%s)",
                    pid[:4],
                    item_id,
                    type(exc).__name__,
                )
                continue
            if res.refused:
                counts["purge_refused"] += 1
                logger.warning(
                    "quarantine: purge refused patient %s item=%d (path check)", pid[:4], item_id
                )
                continue
            marked = await db.mark_quarantine_files_purged(
                item_id, patient_id=pid, kind=kind, at=at, before=before
            )
        counts["files_removed"] += res.removed
        counts["files_missing"] += res.missing
        counts["purged_bytes"] += res.bytes
        if res.dir_kept:
            counts["dirs_kept"] += 1
        if not marked:
            counts["purge_not_marked"] += 1
        elif kind == "approved":
            counts["approved_purged_items"] += 1
        else:
            counts["purged_items"] += 1
    if counts:
        for key, value in counts.items():
            _counters()[f"quarantine_{key}"] += value
        _purge_last.clear()
        _purge_last.update(
            {
                "at": at,
                **{
                    key: int(counts[key])
                    for key in (
                        "purged_items",
                        "approved_purged_items",
                        "purged_bytes",
                        "files_removed",
                        "files_missing",
                        "purge_refused",
                        "purge_failed",
                        "purge_not_marked",
                        "dirs_kept",
                    )
                },
            }
        )
        logger.info("quarantine: purge %s", dict(sorted(counts.items())))
    return dict(counts)


def purge_overview() -> dict:
    """The switches and the last purge run that had work (in memory, counts
    only; admin overview and operator /readiness)."""
    return {
        "trash_days": TRASH_DAYS,
        "approved_switch": purge_approved_enabled(),
        "approved_after_days": purge_approved_after_days(),
        "last_run": dict(_purge_last) or None,
    }


async def run_maintenance(db) -> dict:
    """Scheduler sweep (from the inbound queue job, under ``PROCESS_LOCK``):
    expire old pending items (mark only), purge the encrypted files of items
    past their lifecycle (``purge_due``) and send the due digests. Returns
    counts."""
    if not configured():
        return {"expired": 0, "notices": 0, "purge": {}}
    now = _now()
    expired = await db.expire_quarantine_items(
        before=_iso(now - timedelta(days=EXPIRE_AFTER_DAYS)),
        stale_before=_iso(now - timedelta(minutes=CLAIM_STALE_MIN)),
        at=_iso(now),
    )
    if expired:
        _counters()["quarantine_expired"] += expired
        logger.info("quarantine: expired=%d", expired)
    try:
        purge = await purge_due(db)
    except Exception as exc:  # noqa: BLE001 - the purge never stops the sweep
        purge = {"purge_failed": 1}
        logger.warning("quarantine: purge sweep failed (%s)", type(exc).__name__)
    notices = 0
    for pid in await db.quarantine_patients_due(since=_iso(now - timedelta(days=1))):
        try:
            notices += await notify_due(db, pid)
        except Exception as exc:  # noqa: BLE001 - one patient never stops the sweep
            logger.warning("quarantine: notify failed patient %s (%s)", pid[:4], type(exc).__name__)
    return {"expired": expired, "notices": notices, "purge": purge}


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
        if filed:
            await db.add_quarantine_filed(item_id, patient_id=pid, filed=filed)
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
        filed_count=int(item.get("filed_count") or 0) + filed,
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
    # files an earlier, partial approve already filed stay filed (create-only
    # path, nothing is deleted); the answer says how many
    item = await db.get_quarantine_item(int(item_id), patient_id=patient_id)
    return {"result": "rejected", "already_filed": int((item or {}).get("filed_count") or 0)}


async def restore(db, patient_id: str, item_id: int, *, decided_by: str) -> dict:
    """Trash → pending (maintainer decision 2026-11-22): a rejected or expired
    item decided within ``TRASH_DAYS``. Dashboard only (never an e-mail
    link); idempotent (``already`` when it is pending); issues no new e-mail
    link and voids the item's unused ones."""
    from oncofiles import inbound_email

    async with inbound_email.PROCESS_LOCK, inbound_email._state.lock(patient_id):
        now = _now()
        ok = await db.restore_quarantine_item(
            int(item_id),
            patient_id=patient_id,
            since=_iso(now - timedelta(days=TRASH_DAYS)),
            at=_iso(now),
        )
        if not ok:
            item = await db.get_quarantine_item(int(item_id), patient_id=patient_id)
            if item is None:
                return {"result": "not_found"}
            if item["state"] == "pending":
                return {"result": "already"}
            return {"result": "not_restorable", "state": item["state"]}
        await _journal(
            db,
            patient_id,
            "restore",
            {"item_id": int(item_id), "via": "dashboard", "caller_email_hash": decided_by},
        )
    _counters()["quarantine_restored"] += 1
    logger.info("quarantine: restored patient %s item=%d", patient_id[:4], int(item_id))
    return {"result": "restored"}


# ── action links (/q/<token>) ────────────────────────────────────────────


async def resolve_link(db, token: str) -> dict | None:
    """The live token row for ``token`` (pending item, unused, unexpired,
    binding intact, issued to the patient's CURRENT caregiver address), else
    None. Reads only. A caregiver change also voids the patient's outstanding
    tokens when it is written (``db.update_patient``); this check is the
    second layer for any other path."""
    if not configured() or not isinstance(token, str) or not _TOKEN_RE.match(token):
        return None
    row = await db.find_quarantine_token(token_hash(token))
    if row is None or row["used_at"] or row["state"] != "pending":
        return None
    if str(row["expires_at"]) <= _iso(_now()):
        return None
    if row["action"] not in link_actions(str(row.get("refusal_reason") or "")):
        # an approve link of an unverified caregiver claim is never issued;
        # one that exists anyway is refused (infra review of #970)
        return None
    stored = str(row.get("recipient_hash") or "")
    expected = _binding(row["action"], row["patient_id"], row["item_uuid"], stored, token)
    if not stored or not hmac.compare_digest(expected, row["binding_hash"]):
        return None
    patient = await db.get_patient(row["patient_id"])
    current = recipient_hash(getattr(patient, "caregiver_email", None) if patient else None)
    if not current or not hmac.compare_digest(current, stored):
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
    code = "error"
    try:
        patient = await db.get_patient(pid)
        decided_by = caller_email_hash(_recipient(patient)) if patient is not None else None
        decided_by = decided_by or "email_link"
        decide = approve if row["action"] == "approve" else reject
        result = await decide(
            db, pid, row["quarantine_id"], decided_by=decided_by, via="email_link"
        )
        code = result["result"]
    finally:
        # the token is spent only by a decision; an error, a busy item or a
        # file that could not be filed now gives it back (infra review of #970)
        if code not in ("approved", "rejected", "already", "not_found", "not_pending"):
            await db.release_quarantine_token(row["token_id"], patient_id=pid, at=used_at)
    if code in ("approved", "rejected"):
        return code
    if code in ("retry_later", "busy"):
        return "retry_later"
    return "invalid"


# ── admin overview ───────────────────────────────────────────────────────


async def admin_overview(db) -> dict:
    """Counts and bytes per state over all patients (admin zone): pending
    bytes against ``QUARANTINE_MAX_PENDING_BYTES``, the retained bytes of
    rejected / expired items the purge frees after the trash, and the last
    purge run (``purged_items``, ``purged_bytes``). No ids, no addresses,
    no names. ``by_state['purged']`` bytes are what the row recorded; those
    files are gone."""
    if not configured():
        return {"enabled": False}
    by_state = await db.quarantine_state_totals()
    retained = sum(by_state.get(s, {}).get("bytes", 0) for s in ("rejected", "expired"))
    last = _purge_last
    return {
        "enabled": True,
        "by_state": by_state,
        "pending_bytes": by_state.get("pending", {}).get("bytes", 0),
        "pending_budget_bytes": QUARANTINE_MAX_PENDING_BYTES,
        "retained_bytes": retained,
        # bytes still on disk (every item whose files were not purged),
        # against QUARANTINE_MAX_RETAINED_BYTES
        "stored_bytes": await db.quarantine_retained_bytes_total(),
        "stored_budget_bytes": QUARANTINE_MAX_RETAINED_BYTES,
        # the last purge run that had work (in memory; 0 before the first)
        "purged_items": int(last.get("purged_items", 0))
        + int(last.get("approved_purged_items", 0)),
        "purged_bytes": int(last.get("purged_bytes", 0)),
        "purge": purge_overview(),
    }


# ── dashboard ────────────────────────────────────────────────────────────


def _card_item(item: dict) -> dict | None:
    try:
        sender, files = _item_view(item)
    except Exception:
        logger.warning("quarantine: item %d metadata unreadable", item["id"])
        return None
    return {
        "id": item["id"],
        "sender": sender,
        "received_at": item["received_at"],
        "reason": item["refusal_reason"],
        "files": [
            {"name": f["name"], "type": file_type(f["name"]), "size": int(f["size"])} for f in files
        ],
        "total_bytes": int(item["total_bytes"]),
        "filed_count": int(item.get("filed_count") or 0),
    }


async def dashboard_view(db, patient_id: str, *, caregiver: bool) -> dict:
    """The card's data. The caregiver sees each pending item (sender, time,
    file names, types, sizes; ``reason`` ``auth_failed`` = claims to be from
    the caregiver but is not signed) and the trash (rejected / expired items
    still restorable, with ``days_left``); anyone else (an admin's read-only
    view) only the count."""
    if not configured():
        return {"enabled": False, "pending": 0, "items": [], "trash": []}
    if not caregiver:
        return {
            "enabled": True,
            "read_only": True,
            "pending": await db.count_quarantine_pending(patient_id=patient_id),
            "items": [],
            "trash": [],
        }
    out = []
    for item in await db.list_quarantine_items(patient_id=patient_id, limit=MAX_PENDING_ITEMS):
        view = _card_item(item)
        if view is not None:
            out.append(view)
    now = _now()
    trash = []
    for item in await db.list_quarantine_trash(
        patient_id=patient_id, since=_iso(now - timedelta(days=TRASH_DAYS))
    ):
        view = _card_item(item)
        if view is None:
            continue
        try:
            decided = datetime.strptime(item["decided_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
            days_left = max(0, TRASH_DAYS - (now - decided).days)
        except (TypeError, ValueError):
            days_left = 0
        trash.append({**view, "state": item["state"], "days_left": days_left})
    return {
        "enabled": True,
        "read_only": False,
        "pending": len(out),
        "items": out,
        "trash": trash,
    }
