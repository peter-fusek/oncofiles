"""Signal bindings: which sender may ask about which patient (#1032).

The dashboard's "Connect Signal" button (a later PR) calls
:func:`create_link_code` for the signed-in caregiver; the caregiver sends the
6-character code to the bot number and the adapter calls
:func:`bind_from_message`. From then on :func:`binding_for_phone` resolves the
sender to ONE patient; a sender without a valid binding resolves to nothing
(no default patient, fail closed, #623).

* **Sender address.** The E.164 number, or ``uuid:<account uuid>`` when the
  sender hides the number. Stored only as a keyed HMAC (``phone_hash``, the
  lookup key) and AES-GCM (``phone_enc``, to reply; associated data
  ``signal|<patient_id>|<row_uuid>``). Never logged; logs carry a short prefix
  of the hash at most.
* **Codes.** Crockford base32, :data:`CODE_LEN` characters, :data:`CODE_TTL_S`
  seconds, single use (a conditional UPDATE), only a keyed HMAC stored; the
  lookup is by that hash and confirmed with ``hmac.compare_digest``. At most
  :data:`MAX_OPEN_CODES_PER_PATIENT` unexpired unused codes per patient.
* **One caregiver per sender.** A sender bound by caregiver A can bind further
  codes of A (A's other patients) but never a code of caregiver B: possession
  of another family's code never widens what this sender can read.
* **A binding follows the caregiver.** It counts only while the patient is
  active and the patient's CURRENT caregiver still hashes to the binding's
  ``caregiver_email_hash``; a caregiver change silently ends it.
* **Several patients.** One row per (sender, patient); the newest
  ``selected_at`` is the active patient (:func:`select_patient`).
* **Throttle.** :func:`note_message` keeps a persisted per-sender hour window
  (:data:`MAX_MESSAGES_PER_HOUR`), failed code attempts
  (:data:`MAX_FAILED_CODES_PER_HOUR`) and the once-a-day neutral reply.

Every DB call runs on the event loop through the one connection; nothing is
ever deleted (revoke = ``active`` 0 + ``revoked_at``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from oncofiles import secrets_keys
from oncofiles.database._tenant import require_patient_id

logger = logging.getLogger(__name__)

CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
CODE_LEN = 6
CODE_TTL_S = 10 * 60
MAX_OPEN_CODES_PER_PATIENT = 5
MAX_MESSAGES_PER_HOUR = 20
MAX_FAILED_CODES_PER_HOUR = 5
NEUTRAL_REPLY_INTERVAL_S = 24 * 3600
WINDOW_S = 3600
CONVERSATION_IDLE_S = 6 * 3600

_E164 = re.compile(r"^\+[1-9][0-9]{6,14}$")
_CROCKFORD_FOLD = str.maketrans({"O": "0", "I": "1", "L": "1"})
_CODE_SEP = re.compile(r"[\s\-]")


class LinkCodeRefusedError(Exception):
    """:func:`create_link_code` refused; ``code`` is a reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BindResult:
    status: str  # bound | already | refused
    patient_id: str | None = None
    display_name: str | None = None
    reason: str | None = None  # why refused (logged as a code, never shown)


# ── time ──────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_fine(dt: datetime) -> str:
    """Microsecond ISO time for ``selected_at``: two switches within one
    second still order correctly."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


# ── address, hashes, encryption ───────────────────────────────────────────


def is_e164(value: object) -> bool:
    return isinstance(value, str) and bool(_E164.match(value))


def normalize_address(raw: object) -> str | None:
    """``+<digits>`` (E.164) or ``uuid:<canonical uuid>``; None for anything else."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if _E164.match(value):
        return value
    candidate = value.removeprefix("uuid:")
    try:
        return "uuid:" + str(uuid.UUID(candidate))
    except ValueError:
        return None


def phone_hash(address: str) -> str:
    """Keyed HMAC hex of a normalised sender address (raises without a key)."""
    return hmac.new(
        secrets_keys.signal_phone_hash_key(), address.encode(), hashlib.sha256
    ).hexdigest()


def caregiver_hash(email: str | None) -> str | None:
    value = (email or "").strip().lower()
    if not value:
        return None
    return hmac.new(
        secrets_keys.signal_caregiver_hash_key(), value.encode(), hashlib.sha256
    ).hexdigest()


def _code_hash(code: str) -> str:
    return hmac.new(secrets_keys.signal_link_code_key(), code.encode(), hashlib.sha256).hexdigest()


def _aad(patient_id: str, row_uuid: str) -> bytes:
    return f"signal|{patient_id}|{row_uuid}".encode()


def seal_address(address: str, patient_id: str, row_uuid: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return nonce + AESGCM(secrets_keys.signal_phone_key()).encrypt(
        nonce, address.encode(), _aad(patient_id, row_uuid)
    )


def open_address(blob: bytes, patient_id: str, row_uuid: str) -> str | None:
    """The stored sender address, or None when it does not decrypt for this row."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = bytes(blob)
        plain = AESGCM(secrets_keys.signal_phone_key()).decrypt(
            raw[:12], raw[12:], _aad(patient_id, row_uuid)
        )
    except (InvalidTag, ValueError, TypeError):
        return None
    return plain.decode()


# ── codes ─────────────────────────────────────────────────────────────────


def normalize_code(text: object) -> str | None:
    """The code a message carries (``ab-c1 23`` → ``ABC123``), or None."""
    if not isinstance(text, str) or len(text) > 16:
        return None
    value = _CODE_SEP.sub("", text).upper().translate(_CROCKFORD_FOLD)
    if len(value) != CODE_LEN or any(ch not in CODE_ALPHABET for ch in value):
        return None
    return value


def looks_like_code(text: object) -> bool:
    """A message that is nothing but a code (digits required, so a plain
    six-letter word such as a greeting is not taken for one)."""
    return normalize_code(text) is not None and any(ch.isdigit() for ch in str(text))


def _new_code() -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if any(ch.isdigit() for ch in code):
            return code


async def _patient_row(db, patient_id: str) -> dict | None:
    async with db.db.execute(
        "SELECT patient_id, display_name, caregiver_email, is_active FROM patients "
        "WHERE patient_id = ?",
        (patient_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def create_link_code(db, *, patient_id: str, caregiver_email: str) -> dict:
    """A one-time code for the patient's OWN caregiver: ``{"code", "expires_at"}``.

    Refusals (``LinkCodeRefusedError.code``): ``not_configured`` (no key),
    ``not_found`` (unknown / inactive patient), ``not_caregiver`` (the caller
    is not the patient's caregiver; an admin who is not gets no chat),
    ``too_many_codes``.
    """
    pid = require_patient_id(patient_id)
    try:
        caller = caregiver_hash(caregiver_email)
    except secrets_keys.SignalSecretMissingError:
        raise LinkCodeRefusedError("not_configured") from None
    patient = await _patient_row(db, pid)
    if not patient or not patient["is_active"]:
        raise LinkCodeRefusedError("not_found")
    own = caregiver_hash(patient["caregiver_email"])
    if caller is None or own is None or not hmac.compare_digest(caller, own):
        raise LinkCodeRefusedError("not_caregiver")
    now = _now()
    async with db.db.execute(
        "SELECT COUNT(*) FROM signal_link_codes WHERE patient_id = ? AND used_at IS NULL "
        "AND expires_at > ?",
        (pid, _iso(now)),
    ) as cur:
        open_codes = (await cur.fetchone())[0]
    if open_codes >= MAX_OPEN_CODES_PER_PATIENT:
        raise LinkCodeRefusedError("too_many_codes")
    expires = _iso(now + timedelta(seconds=CODE_TTL_S))
    for _ in range(5):
        code = _new_code()
        cur = await db.db.execute(
            "INSERT INTO signal_link_codes (patient_id, code_hash, caregiver_email_hash, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(code_hash) DO NOTHING",
            (pid, _code_hash(code), own, _iso(now), expires),
        )
        if cur.rowcount == 1:
            await db.db.commit()
            logger.info("signal: link code created (patient %s)", pid[:4])
            return {"code": code, "expires_at": expires}
    raise LinkCodeRefusedError("too_many_codes")


async def bind_from_message(db, phone: str, code: str) -> BindResult:
    """Consume a code sent by ``phone`` and bind it to the code's patient.

    Refused (one neutral reply upstream, nothing else): unknown, wrong,
    expired or used code; a code of another caregiver than the one this
    sender is already bound to; a patient that is gone or whose caregiver
    changed since. A refusal never consumes a valid code.
    """
    address = normalize_address(phone)
    normal = normalize_code(code)
    if address is None or normal is None:
        return BindResult("refused", reason="malformed")
    wanted = _code_hash(normal)
    async with db.db.execute(
        "SELECT id, patient_id, code_hash, caregiver_email_hash, expires_at, used_at "
        "FROM signal_link_codes WHERE code_hash = ?",
        (wanted,),
    ) as cur:
        row = await cur.fetchone()
    row = dict(row) if row else None
    if row is None or not hmac.compare_digest(row["code_hash"], wanted):
        return BindResult("refused", reason="unknown_code")
    now = _now()
    if row["used_at"]:
        return BindResult("refused", reason="used_code")
    expires = _parse(row["expires_at"])
    if expires is None or expires <= now:
        return BindResult("refused", reason="expired_code")
    pid = row["patient_id"]
    patient = await _patient_row(db, pid)
    current = caregiver_hash(patient["caregiver_email"]) if patient else None
    if (
        not patient
        or not patient["is_active"]
        or current is None
        or not hmac.compare_digest(current, row["caregiver_email_hash"])
    ):
        return BindResult("refused", reason="caregiver_changed")
    ph = phone_hash(address)
    for other in await _valid_bindings(db, ph):
        if not hmac.compare_digest(other["caregiver_email_hash"], row["caregiver_email_hash"]):
            return BindResult("refused", reason="other_caregiver")
    cur = await db.db.execute(
        "UPDATE signal_link_codes SET used_at = ? WHERE id = ? AND used_at IS NULL "
        "AND expires_at > ?",
        (_iso(now), row["id"], _iso(now)),
    )
    if cur.rowcount != 1:
        await db.db.commit()
        return BindResult("refused", reason="used_code")
    async with db.db.execute(
        "SELECT id FROM signal_bindings WHERE phone_hash = ? AND patient_id = ? AND active = 1",
        (ph, pid),
    ) as c:
        existing = await c.fetchone()
    if existing:
        await db.db.execute(
            "UPDATE signal_bindings SET selected_at = ? WHERE id = ?", (_iso_fine(now), existing[0])
        )
        await db.db.commit()
        return BindResult("already", patient_id=pid, display_name=patient["display_name"])
    row_uuid = str(uuid.uuid4())
    await db.db.execute(
        "INSERT INTO signal_bindings (patient_id, caregiver_email_hash, phone_hash, row_uuid, "
        "phone_enc, selected_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            require_patient_id(pid),
            row["caregiver_email_hash"],
            ph,
            row_uuid,
            seal_address(address, pid, row_uuid),
            _iso_fine(now),
            _iso(now),
        ),
    )
    await db.db.commit()
    logger.info("signal: bound sender %s to patient %s", ph[:8], pid[:4])
    return BindResult("bound", patient_id=pid, display_name=patient["display_name"])


# ── bindings ──────────────────────────────────────────────────────────────


async def _valid_bindings(db, ph: str) -> list[dict]:
    """The sender's active bindings that still count (see the module docstring),
    oldest first."""
    async with db.db.execute(
        "SELECT b.id, b.patient_id, b.caregiver_email_hash, b.row_uuid, b.phone_enc, "
        "b.conversation_uuid, b.conversation_at, b.selected_at, b.created_at, "
        "p.display_name, p.caregiver_email, p.is_active "
        "FROM signal_bindings b JOIN patients p ON p.patient_id = b.patient_id "
        "WHERE b.phone_hash = ? AND b.active = 1 ORDER BY b.id",
        (ph,),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    out = []
    for r in rows:
        current = caregiver_hash(r.pop("caregiver_email"))
        if (
            r.pop("is_active")
            and current is not None
            and hmac.compare_digest(current, r["caregiver_email_hash"])
        ):
            out.append(r)
    return out


async def bound_patients(db, phone: str) -> list[dict]:
    """The sender's valid bindings, oldest first: ``binding_id``, ``patient_id``,
    ``display_name``, ``active`` (the selected one)."""
    address = normalize_address(phone)
    if address is None:
        return []
    rows = await _valid_bindings(db, phone_hash(address))
    chosen = _selected(rows)
    return [
        {
            "binding_id": r["id"],
            "patient_id": r["patient_id"],
            "display_name": r["display_name"],
            "active": chosen is not None and r["id"] == chosen["id"],
        }
        for r in rows
    ]


def _selected(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    return max(rows, key=lambda r: (r["selected_at"] or r["created_at"] or "", r["id"]))


async def active_binding(db, phone: str) -> dict | None:
    """The sender's active binding row (with ``display_name``), or None."""
    address = normalize_address(phone)
    if address is None:
        return None
    return _selected(await _valid_bindings(db, phone_hash(address)))


async def binding_for_phone(db, phone: str) -> str | None:
    """The patient this sender may ask about right now, or None (no default)."""
    row = await active_binding(db, phone)
    return row["patient_id"] if row else None


async def select_patient(db, phone: str, number: int) -> dict | None:
    """Make the sender's ``number``-th (1-based, :func:`bound_patients` order)
    binding active. None when there is no such binding."""
    address = normalize_address(phone)
    if address is None:
        return None
    rows = await _valid_bindings(db, phone_hash(address))
    if not 1 <= number <= len(rows):
        return None
    row = rows[number - 1]
    await db.db.execute(
        "UPDATE signal_bindings SET selected_at = ? WHERE id = ? AND active = 1",
        (_iso_fine(_now()), row["id"]),
    )
    await db.db.commit()
    return {"patient_id": row["patient_id"], "display_name": row["display_name"]}


async def revoke_binding(db, *, patient_id: str, binding_id: int | None = None) -> int:
    """End the patient's bindings (all, or one ``binding_id`` of that patient).
    Returns how many were active. Nothing is deleted."""
    pid = require_patient_id(patient_id)
    sql = (
        "UPDATE signal_bindings SET active = 0, revoked_at = ? WHERE patient_id = ? AND active = 1"
    )
    params: list = [_iso(_now()), pid]
    if binding_id is not None:
        sql += " AND id = ?"
        params.append(int(binding_id))
    cur = await db.db.execute(sql, params)
    await db.db.commit()
    if cur.rowcount:
        logger.info("signal: %d binding(s) revoked (patient %s)", cur.rowcount, pid[:4])
    return cur.rowcount


async def list_bindings(db, *, patient_id: str) -> list[dict]:
    """The patient's bindings for the dashboard: ids and times only, never the number."""
    pid = require_patient_id(patient_id)
    async with db.db.execute(
        "SELECT id, active, created_at, selected_at, revoked_at FROM signal_bindings "
        "WHERE patient_id = ? ORDER BY id",
        (pid,),
    ) as cur:
        return [
            {
                "binding_id": r["id"],
                "active": bool(r["active"]),
                "created_at": r["created_at"],
                "selected_at": r["selected_at"],
                "revoked_at": r["revoked_at"],
            }
            for r in await cur.fetchall()
        ]


def reply_address(binding: dict) -> str | None:
    """The address to answer a binding on (decrypted, never logged)."""
    return open_address(binding["phone_enc"], binding["patient_id"], binding["row_uuid"])


async def conversation_for(db, binding: dict) -> str:
    """The binding's current chat_history conversation (a new one after an idle gap)."""
    now = _now()
    last = _parse(binding.get("conversation_at"))
    conv = binding.get("conversation_uuid")
    if not conv or last is None or (now - last).total_seconds() > CONVERSATION_IDLE_S:
        conv = str(uuid.uuid4())
    await db.db.execute(
        "UPDATE signal_bindings SET conversation_uuid = ?, conversation_at = ? "
        "WHERE id = ? AND patient_id = ?",
        (conv, _iso(now), binding["id"], binding["patient_id"]),
    )
    await db.db.commit()
    return conv


# ── throttle ──────────────────────────────────────────────────────────────


async def _limits(db, ph: str) -> dict:
    await db.db.execute(
        "INSERT INTO signal_sender_limits (phone_hash) VALUES (?) "
        "ON CONFLICT(phone_hash) DO NOTHING",
        (ph,),
    )
    async with db.db.execute(
        "SELECT window_started_at, window_messages, window_failed_codes, neutral_reply_at "
        "FROM signal_sender_limits WHERE phone_hash = ?",
        (ph,),
    ) as cur:
        return dict(await cur.fetchone())


def _window_open(row: dict, now: datetime) -> bool:
    start = _parse(row["window_started_at"])
    return start is not None and (now - start).total_seconds() < WINDOW_S


async def note_message(db, ph: str) -> str:
    """Count one incoming message of a sender: ``ok``, ``limited_notice`` (the
    first one over :data:`MAX_MESSAGES_PER_HOUR` in this window: tell them once)
    or ``limited`` (drop silently)."""
    now = _now()
    row = await _limits(db, ph)
    if _window_open(row, now):
        count = row["window_messages"] + 1
        await db.db.execute(
            "UPDATE signal_sender_limits SET window_messages = ?, updated_at = ? "
            "WHERE phone_hash = ?",
            (count, _iso(now), ph),
        )
    else:
        count = 1
        await db.db.execute(
            "UPDATE signal_sender_limits SET window_started_at = ?, window_messages = 1, "
            "window_failed_codes = 0, updated_at = ? WHERE phone_hash = ?",
            (_iso(now), _iso(now), ph),
        )
    await db.db.commit()
    if count <= MAX_MESSAGES_PER_HOUR:
        return "ok"
    return "limited_notice" if count == MAX_MESSAGES_PER_HOUR + 1 else "limited"


async def code_attempt_allowed(db, ph: str) -> bool:
    row = await _limits(db, ph)
    if not _window_open(row, _now()):
        return True
    return row["window_failed_codes"] < MAX_FAILED_CODES_PER_HOUR


async def note_failed_code(db, ph: str) -> None:
    await db.db.execute(
        "UPDATE signal_sender_limits SET window_failed_codes = window_failed_codes + 1, "
        "updated_at = ? WHERE phone_hash = ?",
        (_iso(_now()), ph),
    )
    await db.db.commit()


async def claim_neutral_reply(db, ph: str) -> bool:
    """True (and remembered) when this sender may get the neutral reply now:
    at most once per :data:`NEUTRAL_REPLY_INTERVAL_S`."""
    now = _now()
    await _limits(db, ph)
    cutoff = _iso(now - timedelta(seconds=NEUTRAL_REPLY_INTERVAL_S))
    cur = await db.db.execute(
        "UPDATE signal_sender_limits SET neutral_reply_at = ?, updated_at = ? "
        "WHERE phone_hash = ? AND (neutral_reply_at IS NULL OR neutral_reply_at <= ?)",
        (_iso(now), _iso(now), ph, cutoff),
    )
    await db.db.commit()
    return cur.rowcount == 1
