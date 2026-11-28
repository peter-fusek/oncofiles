"""Encrypted per-patient chat history: the storage API (#1022, epic #1018).

The chat core (#1020) and the dashboard Chat section (#1023) call this module
and nothing else to keep and read conversations; ``database._chat`` only moves
sealed rows. The front end has already checked the ACL (caregiver only, #867
helpers); every call here still names ONE patient and every query filters on
it, so a foreign patient or conversation is simply "not found".

* **Encryption.** The text is AES-GCM with ``secrets_keys.chat_history_key()``
  (own purpose string; ``CHAT_HISTORY_SECRET`` when set, else derived from the
  mandatory ``HOLDING_STORE_SECRET``), associated data
  ``chat|<patient_id>|<conversation_id>|<row_uuid>``: a body copied onto any
  other row, patient or conversation never decrypts.
* **Fail closed.** Without a key nothing is stored: :func:`store_message`
  returns None and counts ``key_missing``; the chat still answers, it just
  keeps no history.
* **What is stored.** The text, citations as document ids + pages only (never
  text), the local model id, grounding counts, a keyed hash of the caller.
* **Never logged.** Logs and counters carry codes and counts only; the text
  never reaches a log, an e-mail, a cloud model or the index.
* **Retention.** 90 days, then the trash, then purged 30 days later
  (``oncofiles.chat_retention``, off until the infra check). The user's own
  delete moves a conversation to the same trash; :func:`restore_conversation`
  brings it back until the purge.
* **Data rights.** Exported decrypted (:func:`export_row`, called by
  ``data_rights.export_stream``) and erased with the patient (#856 registry).
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from oncofiles.chat_retention_sql import ts as _ts
from oncofiles.database._chat import CHAT_CHANNELS, CHAT_ROLES

logger = logging.getLogger(__name__)

_MAGIC = b"OCH1"
MAX_TEXT_CHARS = 20_000
MAX_CITATIONS = 50
MAX_GROUNDING_KEYS = 30
MAX_MODEL_CHARS = 200
DEFAULT_FOLLOWUP_MESSAGES = 20
TITLE_CHARS = 80

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@-]{1,200}$")

# In-memory counters since process start (codes -> counts), for operator
# /readiness via the retention job's status. Never a patient id or text.
_counters: Counter = Counter()


class ChatHistoryUnavailableError(RuntimeError):
    """No key: nothing can be stored or read (``key_missing``)."""


def counters() -> dict[str, int]:
    return dict(sorted(_counters.items()))


def _reset_for_tests() -> None:
    _counters.clear()


def _now() -> str:
    return _ts(datetime.now(UTC))


def _canonical_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("conversation_id must be a UUID") from exc
    if str(parsed) != str(value):
        raise ValueError("conversation_id must be a canonical lower-case UUID")
    return str(parsed)


def new_conversation_id() -> str:
    return str(uuid.uuid4())


# ── crypto ────────────────────────────────────────────────────────────────


def _aad(patient_id: str, conversation_id: str, row_uuid: str) -> bytes:
    if not patient_id or not conversation_id or not row_uuid:
        raise ValueError("patient, conversation and row required")
    return f"chat|{patient_id}|{conversation_id}|{row_uuid}".encode()


def _key() -> bytes:
    from oncofiles.secrets_keys import ChatHistorySecretMissingError, chat_history_key

    try:
        return chat_history_key()
    except ChatHistorySecretMissingError as exc:
        raise ChatHistoryUnavailableError("key_missing") from exc


def seal(text: str, patient_id: str, conversation_id: str, row_uuid: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(
        nonce, text.encode("utf-8"), _aad(patient_id, conversation_id, row_uuid)
    )
    return _MAGIC + nonce + ct


def unseal(blob: bytes, patient_id: str, conversation_id: str, row_uuid: str) -> str:
    """Decrypt one body; raises ``ValueError`` (malformed) or
    ``cryptography.exceptions.InvalidTag`` (wrong key / row / patient)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = bytes(blob)
    if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + 12 + 16:
        raise ValueError("malformed")
    nonce = blob[len(_MAGIC) : len(_MAGIC) + 12]
    raw = AESGCM(_key()).decrypt(
        nonce, blob[len(_MAGIC) + 12 :], _aad(patient_id, conversation_id, row_uuid)
    )
    return raw.decode("utf-8")


def _open_text(row: dict, patient_id: str) -> str | None:
    """The row's text, or None when purged / unreadable (counted)."""
    blob = row.get("body_enc")
    if blob is None:
        return None
    try:
        return unseal(blob, patient_id, row["conversation_id"], row["row_uuid"])
    except ChatHistoryUnavailableError:
        _counters["read_key_missing"] += 1
        return None
    except Exception as exc:  # noqa: BLE001 - one bad row never breaks a read
        _counters["unreadable"] += 1
        logger.warning("chat_history: unreadable row (%s)", type(exc).__name__)
        return None


# ── sanitising the side data ──────────────────────────────────────────────


def clean_citations(citations: Any) -> list[dict]:
    """Document ids and pages only: ``[{"document_id": int, "page": int|None}]``.
    Every other key (text, evidence, file names) is dropped."""
    out: list[dict] = []
    for item in citations or []:
        if not isinstance(item, dict):
            continue
        doc = item.get("document_id")
        if isinstance(doc, bool) or not isinstance(doc, int) or doc <= 0:
            continue
        page = item.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
            page = None
        entry = {"document_id": doc, "page": page}
        if entry not in out:
            out.append(entry)
        if len(out) >= MAX_CITATIONS:
            break
    return out


def clean_grounding(grounding: Any) -> dict[str, int]:
    """Code-like keys with non-negative integer counts only."""
    out: dict[str, int] = {}
    for key, value in dict(grounding or {}).items():
        if (
            isinstance(key, str)
            and _CODE_RE.match(key)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ):
            out[key] = value
        if len(out) >= MAX_GROUNDING_KEYS:
            break
    return dict(sorted(out.items()))


def _clean_model(model: Any) -> str | None:
    if model is None:
        return None
    model = str(model)
    return model if _MODEL_RE.match(model) else None


def _caller_hash(caller_email: str | None) -> str | None:
    if not caller_email:
        return None
    try:
        from oncofiles.secrets_keys import caller_email_hash

        return caller_email_hash(caller_email)
    except Exception:  # noqa: BLE001 - no key configured: no hash
        return None


# ── the storage API ───────────────────────────────────────────────────────


async def store_message(
    db,
    *,
    patient_id: str,
    conversation_id: str,
    channel: str,
    role: str,
    text: str,
    citations: list[dict] | None = None,
    model: str | None = None,
    grounding: dict | None = None,
    caller_email: str | None = None,
) -> int | None:
    """Keep one message. Returns its row id, or None when nothing was stored:
    no key (``key_missing``), empty / too long text (``empty_text`` /
    ``too_long``). ``ValueError`` for a programming error (bad channel, role,
    conversation id, missing patient, a conversation of another patient)."""
    conversation_id = _canonical_uuid(conversation_id)
    if channel not in CHAT_CHANNELS:
        raise ValueError(f"unknown chat channel {channel!r}")
    if role not in CHAT_ROLES:
        raise ValueError(f"unknown chat role {role!r}")
    if not isinstance(text, str) or not text.strip():
        _counters["empty_text"] += 1
        return None
    if len(text) > MAX_TEXT_CHARS:
        _counters["too_long"] += 1
        return None
    row_uuid = str(uuid.uuid4())
    try:
        body = seal(text, patient_id, conversation_id, row_uuid)
    except ChatHistoryUnavailableError:
        _counters["key_missing"] += 1
        logger.warning("chat_history: no key configured, message not stored (key_missing)")
        return None
    cites = clean_citations(citations)
    ground = clean_grounding(grounding)
    row_id = await db.add_chat_message(
        patient_id=patient_id,
        conversation_id=conversation_id,
        channel=channel,
        role=role,
        row_uuid=row_uuid,
        body_enc=body,
        citations_json=json.dumps(cites) if cites else None,
        model=_clean_model(model) if role == "assistant" else None,
        grounding_json=json.dumps(ground) if ground else None,
        caller_email_hash=_caller_hash(caller_email),
    )
    _counters["stored"] += 1
    return row_id


def _message_out(row: dict, patient_id: str) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "channel": row["channel"],
        "text": _open_text(row, patient_id),
        "citations": json.loads(row["citations_json"]) if row.get("citations_json") else [],
        "model": row.get("model"),
        "grounding": json.loads(row["grounding_json"]) if row.get("grounding_json") else {},
        "created_at": row["created_at"],
    }


async def conversation_messages(
    db, *, patient_id: str, conversation_id: str, limit: int = DEFAULT_FOLLOWUP_MESSAGES
) -> list[dict]:
    """The last ``limit`` messages of one conversation, oldest first, decrypted
    (for follow-up questions and the dashboard). Foreign / unknown / trashed →
    []. A row that cannot be decrypted has ``text`` None."""
    try:
        conversation_id = _canonical_uuid(conversation_id)
    except ValueError:
        return []
    rows = await db.get_chat_conversation_messages(
        patient_id=patient_id, conversation_id=conversation_id, limit=limit
    )
    return [_message_out(r, patient_id) for r in rows]


async def list_conversations(
    db, *, patient_id: str, trashed: bool = False, limit: int = 50
) -> list[dict]:
    """The patient's conversations, newest first, each with a short ``title``
    (the start of its first question, decrypted). ``trashed`` = the trash."""
    out = []
    for conv in await db.list_chat_conversations(
        patient_id=patient_id, trashed=trashed, limit=limit
    ):
        first = await db.get_chat_first_user_message(
            patient_id=patient_id, conversation_id=conv["conversation_id"], trashed=trashed
        )
        text = _open_text(first, patient_id) if first else None
        out.append({**conv, "title": text[:TITLE_CHARS] if text else None})
    return out


async def delete_conversation(db, *, patient_id: str, conversation_id: str) -> dict:
    """The user's delete: the conversation moves to the trash (restorable until
    the purge). ``{"status": "trashed" | "not_found", "messages": n}``."""
    try:
        conversation_id = _canonical_uuid(conversation_id)
    except ValueError:
        return {"status": "not_found", "messages": 0}
    n = await db.soft_delete_chat_conversation(
        patient_id=patient_id, conversation_id=conversation_id, at=_now()
    )
    return {"status": "trashed" if n else "not_found", "messages": n}


async def restore_conversation(db, *, patient_id: str, conversation_id: str) -> dict:
    """Back from the trash. ``{"status": "restored" | "not_found", "messages": n}``
    (purged, foreign and unknown conversations are all ``not_found``)."""
    try:
        conversation_id = _canonical_uuid(conversation_id)
    except ValueError:
        return {"status": "not_found", "messages": 0}
    n = await db.restore_chat_conversation(
        patient_id=patient_id, conversation_id=conversation_id, at=_now()
    )
    return {"status": "restored" if n else "not_found", "messages": n}


# ── data export (#856) ────────────────────────────────────────────────────

# Columns the export reads for the decryption (never written out as such).
EXPORT_RAW_COLUMNS = ("conversation_id", "row_uuid", "body_enc")


def export_row(row: dict, patient_id: str) -> dict:
    """One exported row: the readable columns plus ``body`` (the decrypted
    text; None when purged) and ``body_unreadable`` when it exists but cannot
    be decrypted (no key / rotated key). ``body_enc`` and ``row_uuid`` are
    dropped."""
    out = {k: v for k, v in row.items() if k not in ("body_enc", "row_uuid")}
    blob = row.get("body_enc")
    text = _open_text(row, patient_id) if blob is not None else None
    out["body"] = text
    if blob is not None and text is None:
        out["body_unreadable"] = True
    return out
