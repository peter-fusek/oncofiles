"""The dashboard adapter of the chat core (#1023).

``POST /api/chat`` and ``GET /api/chat/suggestions`` live in ``server``; this
module holds what they need beyond the #867 ACL helpers, so the server module
stays thin:

* ``enabled()``: the runtime flag ``CHAT_ENABLED`` (default false). While it
  is false both routes raise the app's unknown-path 404 before anything else
  (``hidden_routes.unknown_path``) and ``/api/patients`` says
  ``chat_enabled: false``, so the dashboard hides the Chat section.
* ``TurnLimiter``: sliding-window turn limits per principal (the keyed hash of
  the session e-mail, never the address) and per client IP (``ratelimit``'s
  ``client_ip`` with ``TRUSTED_PROXIES``), per minute and per hour, each
  configurable by runtime env (``CHAT_TURNS_PER_MIN`` / ``_PER_HOUR``,
  ``CHAT_IP_TURNS_PER_MIN`` / ``_PER_HOUR``; ``0`` switches that window off).
  In memory, bounded, nothing is consumed by a refused turn.
* ``stream()``: the NDJSON framing of ``chat.core.answer`` (one JSON object
  per line, ``{"type": …, "data": …}``). The response carries only the core's
  events; the one log line per turn carries event counts and an error code,
  never a question, an answer, a tool result or a citation.
* ``suggestions()``: three to five suggested questions (SK + EN) built from the
  patient's own data without any model call: the most recently measured lab
  codes, a trend when a code has several values, the next planned date when
  one exists, the last report, an overview question.

* ``plan_turn()`` / ``stream()`` keep the conversation in the encrypted chat
  history (#1022, ``oncofiles.chat_history``): with a key configured the
  server reads the recent turns of ``conversation_id`` (this patient only; a
  foreign, unknown or trashed id is "not found", the same 404 as a foreign
  patient) and ignores the client's copy of the history; after ``done`` it
  stores the question and the answer (channel ``dashboard``, citations as
  document ids + pages, grounding counts, model, keyed caller hash) and the
  ``done`` event carries ``conversation_id``. Without a key nothing is stored,
  the client's bounded turns are used as before and ``done`` says
  ``conversation_id: null``. A storage failure never breaks the answer; it is
  counted (``history_counters()``) and logged as a code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import Counter, OrderedDict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from oncofiles import config

logger = logging.getLogger(__name__)

MEDIA_TYPE = "application/x-ndjson"
MAX_BODY_BYTES = 32 * 1024  # also the rate limiter's cap for /api/chat
LANGS = ("sk", "en")
MIN_SUGGESTIONS = 3
MAX_SUGGESTIONS = 5
MAX_KEYS = 10_000

DEFAULT_TURNS_PER_MIN = 6
DEFAULT_TURNS_PER_HOUR = 60
DEFAULT_IP_TURNS_PER_MIN = 12
DEFAULT_IP_TURNS_PER_HOUR = 120

# #1022: the server-side history of a conversation (``plan_turn`` reads it,
# ``stream`` writes the turn after ``done``). Messages read back per turn:
HISTORY_READ_MESSAGES = 40
CHANNEL = "dashboard"
# The conversation list and one conversation for rendering.
LIST_LIMIT = 50
RENDER_MESSAGES = 200

# In-memory counters of the history seam since process start (codes only).
_history_counters: Counter[str] = Counter()

STREAM_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Accel-Buffering": "no",
}


def enabled() -> bool:
    """The runtime flag, read at call time (tests patch ``config.CHAT_ENABLED``)."""
    return bool(config.CHAT_ENABLED)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("chat: ignoring non-integer %s", name)
        return default
    return value if value >= 0 else default


def principal_key(email: str) -> str:
    """A non-reversible in-memory key for the limiter (never the address)."""
    return "p:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:24]


class TurnLimiter:
    """Sliding windows per key: a minute and an hour."""

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def reset(self) -> None:
        self._hits.clear()

    @staticmethod
    def limits() -> dict[str, tuple[int, int]]:
        return {
            "principal": (
                _env_int("CHAT_TURNS_PER_MIN", DEFAULT_TURNS_PER_MIN),
                _env_int("CHAT_TURNS_PER_HOUR", DEFAULT_TURNS_PER_HOUR),
            ),
            "ip": (
                _env_int("CHAT_IP_TURNS_PER_MIN", DEFAULT_IP_TURNS_PER_MIN),
                _env_int("CHAT_IP_TURNS_PER_HOUR", DEFAULT_IP_TURNS_PER_HOUR),
            ),
        }

    def _wait(self, key: str, per_min: int, per_hour: int, now: float) -> float:
        hits = self._hits.get(key)
        if not hits:
            return 0.0
        while hits and now - hits[0] >= 3600:
            hits.popleft()
        wait = 0.0
        if per_hour and len(hits) >= per_hour:
            wait = max(wait, 3600 - (now - hits[-per_hour]))
        recent = [t for t in hits if now - t < 60]
        if per_min and len(recent) >= per_min:
            wait = max(wait, 60 - (now - recent[-per_min]))
        return wait

    def take(self, principal: str, ip: str) -> float:
        """0 when the turn is allowed (and counted), else seconds to wait."""
        now = self.clock()
        lim = self.limits()
        keys = [(principal, *lim["principal"]), ("ip:" + ip, *lim["ip"])]
        wait = max(self._wait(k, m, h, now) for k, m, h in keys)
        if wait > 0:
            return wait
        for key, _m, _h in keys:
            hits = self._hits.setdefault(key, deque())
            hits.append(now)
            self._hits.move_to_end(key)
            while len(self._hits) > MAX_KEYS:
                self._hits.popitem(last=False)
        return 0.0


limiter = TurnLimiter()


def client_ip(scope) -> str:
    """The client address the rate limiter uses (``TRUSTED_PROXIES`` only)."""
    from oncofiles import ratelimit

    nets = ratelimit.parse_networks(
        os.environ.get("TRUSTED_PROXIES", ratelimit.DEFAULT_TRUSTED_PROXIES)
    )
    return ratelimit.client_ip(scope, nets)


def frame(event_type: str, data: dict) -> bytes:
    return (json.dumps({"type": event_type, "data": data}, ensure_ascii=False) + "\n").encode()


# ── server-side history of a conversation (#1022) ──


def history_counters() -> dict[str, int]:
    return dict(sorted(_history_counters.items()))


def _reset_history_for_tests() -> None:
    _history_counters.clear()


def _count(code: str) -> None:
    _history_counters[code] += 1


@dataclass
class TurnPlan:
    """What one turn sends to the core and where it is kept afterwards."""

    messages: list[dict]
    # The conversation the turn is stored in; None = nothing is stored (no key).
    conversation_id: str | None
    question: str
    # True when ``conversation_id`` already existed before this turn.
    existing: bool = False


def _cut_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore").rstrip()


def strip_disclaimer(text: str) -> str:
    """The answer without the fixed disclaimer line the core appends."""
    from oncofiles.chat import prompts

    out = text.rstrip()
    for line in prompts.DISCLAIMER.values():
        if out.endswith(line):
            return out[: -len(line)].rstrip()
    return out


def build_messages(history: list[dict], question: dict) -> list[dict]:
    """Stored turns (oldest first) + the new question, inside the core's bounds:
    at most ``core.MAX_MESSAGES`` messages, each ≤ ``MAX_MESSAGE_BYTES``, all
    ≤ ``MAX_TOTAL_BYTES``; the newest turns win. Unreadable rows are skipped."""
    from oncofiles.chat import core

    total = len(question["content"].encode("utf-8"))
    kept: list[dict] = []
    for msg in reversed(history):
        role, text = msg.get("role"), msg.get("text")
        if role not in ("user", "assistant") or not isinstance(text, str):
            continue
        text = strip_disclaimer(text) if role == "assistant" else text.strip()
        text = _cut_bytes(text, core.MAX_MESSAGE_BYTES)
        if not text:
            continue
        size = len(text.encode("utf-8"))
        if len(kept) >= core.MAX_MESSAGES - 1 or total + size > core.MAX_TOTAL_BYTES:
            break
        kept.append({"role": role, "content": text})
        total += size
    kept.reverse()
    return [*kept, question]


async def plan_turn(db, *, patient_id: str, conversation_id, messages, lang) -> TurnPlan | str:
    """The messages of this turn, or an error code: ``invalid_request`` (400) or
    ``not_found`` (the 404 of a foreign patient: a conversation_id that is not
    a live conversation of THIS patient, malformed included)."""
    from oncofiles import chat_history
    from oncofiles.chat import core

    clean = core.validate(messages, lang)  # the client's turns keep their bounds
    if clean is None:
        return "invalid_request"
    question = clean[-1]
    cid = None
    if conversation_id is not None:
        cid = chat_history.canonical_conversation_id(conversation_id)
        if cid is None:
            return "not_found"
        # Existence needs no key: one live row of this patient's conversation.
        rows = await db.get_chat_conversation_messages(
            patient_id=patient_id, conversation_id=cid, limit=1
        )
        if not rows:
            return "not_found"
    if not chat_history.available():
        _count("history_unavailable")
        return TurnPlan(messages=clean, conversation_id=None, question=question["content"])
    if cid is None:
        return TurnPlan(
            messages=[question],
            conversation_id=chat_history.new_conversation_id(),
            question=question["content"],
        )
    history = await chat_history.conversation_messages(
        db, patient_id=patient_id, conversation_id=cid, limit=HISTORY_READ_MESSAGES
    )
    return TurnPlan(
        messages=build_messages(history, question),
        conversation_id=cid,
        question=question["content"],
        existing=True,
    )


async def _store_turn(
    db,
    *,
    patient_id: str,
    plan: TurnPlan,
    answer_text: str,
    citations: list[dict],
    done: dict,
    caller_email: str,
) -> bool:
    """Keep the question and the answer; never raises (counted, logged by code)."""
    from oncofiles import chat_history

    try:
        uid = await chat_history.store_message(
            db,
            patient_id=patient_id,
            conversation_id=plan.conversation_id,
            channel=CHANNEL,
            role="user",
            text=plan.question,
            caller_email=caller_email,
        )
        if uid is None:
            _count("not_stored")
            logger.warning("chat history: turn not stored (not_stored)")
            return False
        aid = await chat_history.store_message(
            db,
            patient_id=patient_id,
            conversation_id=plan.conversation_id,
            channel=CHANNEL,
            role="assistant",
            text=strip_disclaimer(answer_text),
            citations=citations,
            model=done.get("model"),
            grounding=done.get("grounding") if isinstance(done.get("grounding"), dict) else None,
            caller_email=caller_email,
        )
    except Exception as exc:  # noqa: BLE001 - storage never breaks the answer
        _count("store_failed")
        logger.warning("chat history: store failed (%s)", type(exc).__name__)
        return False
    _count("stored" if aid is not None else "answer_not_stored")
    return True


async def stream(
    db,
    *,
    patient_id: str,
    principal: str,
    messages: list[dict],
    lang: str,
    plan: TurnPlan | None = None,
) -> AsyncIterator[bytes]:
    """NDJSON lines of one turn; one count-only log line at the end. With a
    ``plan`` that names a conversation, the turn is stored before ``done`` is
    sent and ``done`` carries ``conversation_id``."""
    from oncofiles.chat import core

    counts: Counter[str] = Counter()
    error_code = None
    t0 = time.monotonic()
    outcome = "cancelled"
    parts: list[str] = []
    cites: list[dict] = []
    stored = None
    try:
        async for ev in core.answer(
            db, patient_id=patient_id, principal=principal, messages=messages, lang=lang
        ):
            counts[ev.type] += 1
            data = ev.data
            if ev.type == "error":
                error_code = str(data.get("code") or "internal_error")
            elif ev.type == "delta":
                parts.append(str(data.get("text") or ""))
            elif ev.type == "citation":
                cites.append(data)
            elif ev.type == "done" and plan is not None:
                cid = None
                if plan.conversation_id is not None:
                    stored = await _store_turn(
                        db,
                        patient_id=patient_id,
                        plan=plan,
                        answer_text="".join(parts),
                        citations=cites,
                        done=data,
                        caller_email=principal,
                    )
                    # an existing conversation stays addressable even when this
                    # turn could not be kept; a new one only once it is stored
                    cid = plan.conversation_id if (plan.existing or stored) else None
                data = {**data, "conversation_id": cid, "history_stored": bool(stored)}
            yield frame(ev.type, data)
        outcome = "error" if error_code else "done"
    finally:
        logger.info(
            "chat http: outcome=%s events=%s error=%s history=%s seconds=%.1f patient=%s",
            outcome,
            json.dumps(dict(sorted(counts.items()))),
            error_code,
            "off" if plan is None or plan.conversation_id is None else stored,
            time.monotonic() - t0,
            patient_id[:4],
        )


# ── the conversation list and one conversation (dashboard routes) ──


def _days_left(deleted_at: str | None, now: datetime | None = None) -> int | None:
    """Whole days until the purge of a trashed conversation (30 after the trash)."""
    from oncofiles.chat_retention_sql import PURGE_AFTER_TRASH_DAYS, TS_FORMAT

    if not deleted_at:
        return None
    try:
        at = datetime.strptime(str(deleted_at), TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    elapsed = ((now or datetime.now(UTC)) - at).total_seconds() / 86400
    return max(0, int(PURGE_AFTER_TRASH_DAYS - elapsed + 0.999))


async def conversations(db, patient_id: str, *, trashed: bool) -> dict:
    """The patient's dashboard conversations, newest first: id, a short title,
    last activity, turn count (and, in the trash, the days until the purge)."""
    from oncofiles import chat_history
    from oncofiles.chat_retention_sql import PURGE_AFTER_TRASH_DAYS, TRASH_AFTER_DAYS

    rows = await chat_history.list_conversations(
        db, patient_id=patient_id, trashed=trashed, limit=LIST_LIMIT
    )
    items = []
    for row in rows:
        item = {
            "conversation_id": row["conversation_id"],
            "title": row.get("title"),
            "last_at": row.get("last_at"),
            "turns": int(row.get("messages") or 0),
        }
        if trashed:
            item["deleted_at"] = row.get("deleted_at")
            item["purge_in_days"] = _days_left(row.get("deleted_at"))
        items.append(item)
    return {
        "conversations": items,
        "history_available": chat_history.available(),
        "trash_after_days": TRASH_AFTER_DAYS,
        "purge_after_trash_days": PURGE_AFTER_TRASH_DAYS,
    }


async def conversation_turns(db, patient_id: str, conversation_id) -> list[dict] | None:
    """The turns of one live conversation for rendering (role, text, citations
    as document ids + pages, time), or None when it is not this patient's."""
    from oncofiles import chat_history

    cid = chat_history.canonical_conversation_id(conversation_id)
    if cid is None:
        return None
    msgs = await chat_history.conversation_messages(
        db, patient_id=patient_id, conversation_id=cid, limit=RENDER_MESSAGES
    )
    if not msgs:
        return None
    return [
        {
            "role": m["role"],
            "text": m["text"],
            "citations": m["citations"],
            "created_at": m["created_at"],
        }
        for m in msgs
    ]


# ── suggested questions (no model call) ──


def _q(sk: str, en: str) -> dict:
    return {"sk": sk, "en": en}


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


async def suggestions(db, patient_id: str) -> list[dict]:
    """Three to five questions from this patient's data (sequential reads)."""
    out: list[dict] = []
    catalog = await db.fact_catalog_rows(patient_id=patient_id)
    labs = [
        r
        for r in catalog
        if r.get("code") and str(r.get("category") or "") == "lab" and r.get("last_at")
    ]
    labs.sort(key=lambda r: (str(r.get("last_at") or ""), str(r.get("code"))), reverse=True)
    for row in labs[:2]:
        code = str(row["code"])
        out.append(_q(f"Aká je posledná hodnota {code}?", f"What is the latest {code} value?"))
    trend = next((r for r in labs if int(r.get("count") or 0) >= 2), None)
    if trend is not None:
        code = str(trend["code"])
        out.append(_q(f"Ako sa menil {code} v čase?", f"How has {code} changed over time?"))
    planned = await db.timeline_rows(
        patient_id=patient_id, date_from=_today(), kinds=["planned"], limit=1
    )
    if planned:
        out.append(_q("Kedy je najbližší plánovaný termín?", "When is the next planned date?"))
    if await db.count_documents(patient_id=patient_id) > 0:
        out.append(_q("Čo bolo v poslednej správe?", "What did the last report say?"))
    for extra in (
        _q("Aké diagnózy sú v dokumentoch?", "Which diagnoses are in the records?"),
        _q("Aké dokumenty sú v záznamoch?", "Which documents are in the records?"),
        _q("Aké lieky sú v dokumentoch?", "Which medications are in the records?"),
    ):
        if len(out) >= MIN_SUGGESTIONS:
            break
        out.append(extra)
    return out[:MAX_SUGGESTIONS]
