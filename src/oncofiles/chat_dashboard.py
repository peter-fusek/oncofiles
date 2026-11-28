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

Storage seam (#1022): v1 is stateless; the client sends the bounded recent
turns (``chat.core.validate``). See ``HISTORY_SEAM`` below.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import Counter, OrderedDict, deque
from collections.abc import AsyncIterator
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

# TODO(#1022) HISTORY_SEAM: once the encrypted chat history lands, the route
# stores the user's turn before streaming and the assistant's text after
# ``done`` here (and reads the recent turns from it instead of trusting the
# client's copy). Until then nothing about a turn is stored by this adapter.
HISTORY_SEAM = None

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


async def stream(
    db, *, patient_id: str, principal: str, messages: list[dict], lang: str
) -> AsyncIterator[bytes]:
    """NDJSON lines of one turn; one count-only log line at the end."""
    from oncofiles.chat import core

    counts: Counter[str] = Counter()
    error_code = None
    t0 = time.monotonic()
    outcome = "cancelled"
    try:
        async for ev in core.answer(
            db, patient_id=patient_id, principal=principal, messages=messages, lang=lang
        ):
            counts[ev.type] += 1
            if ev.type == "error":
                error_code = str(ev.data.get("code") or "internal_error")
            yield frame(ev.type, ev.data)
        outcome = "error" if error_code else "done"
    finally:
        logger.info(
            "chat http: outcome=%s events=%s error=%s seconds=%.1f patient=%s",
            outcome,
            json.dumps(dict(sorted(counts.items()))),
            error_code,
            time.monotonic() - t0,
            patient_id[:4],
        )


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
