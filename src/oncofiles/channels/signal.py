"""Chat over Signal: a thin adapter over the chat core (#1032, epic #1018).

OFF by default. The lifespan starts :func:`signal_loop` only when
``SIGNAL_ENABLED=true``, ``SIGNAL_API_URL`` (a private host, :func:`api_url_refusal`),
``SIGNAL_NUMBER`` and a key (``SIGNAL_SECRET`` or ``HOLDING_STORE_SECRET``) are
all set; otherwise it logs one warning naming the missing variables
(:func:`missing_settings`) and nothing runs.

signal-cli runs as its own container (``bbernhard/signal-cli-rest-api`` in
``json-rpc`` mode) on its own Docker network, no published port, egress only
to the Signal servers (infra preconditions on #1032). Signal is end-to-end
encrypted up to that container, so the text is readable only on our server.

**Receive / send.** :class:`SignalClient` reads incoming envelopes from the
websocket ``/v1/receive/<number>`` and answers with ``POST /v2/send``. The
loop waits ``STARTUP_GRACE_S`` first (a rolling deploy runs two containers
for a few seconds) and reconnects with a doubling back-off.

**Per message** (:func:`handle_envelope`, one at a time, DB on the event loop):

* only direct text messages (group, sync, receipt and typing messages are ignored);
* the persisted per-sender hour limit (``signal_bindings.note_message``):
  over it one notice, then silence;
* a message that is a link code → :func:`signal_bindings.bind_from_message`
  (a wrong / expired / used code → one neutral line, at most
  ``MAX_FAILED_CODES_PER_HOUR`` per hour);
* a sender with no valid binding → the neutral "connect in the Oncofiles
  dashboard first" at most once a day, nothing else (no default patient);
* ``pacient`` / ``patient`` [n] → the sender's own bound patients (display
  names only) or switch the active one;
* attachments → "attachments are not supported yet" (the text, if any, is
  still answered);
* anything else → ``chat.core.answer_text`` for the ACTIVE binding's patient
  only, with the conversation's recent turns from ``chat_history`` (#1022);
  the answer goes back in at most :data:`MAX_PARTS` parts of about
  :data:`CHUNK_CHARS` characters split on paragraphs, sources as short lines
  (document, date, page), the disclaimer always kept; both turns are stored
  with channel ``signal``. Busy or model busy → a short "try again later";
  never a cloud fallback (the core runs on the local model only).

**Logs** carry counts, codes and a short prefix of the keyed sender hash; never
message text, names or numbers.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import unicodedata
from collections import Counter
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from oncofiles import secrets_keys
from oncofiles.channels import signal_bindings as sb

logger = logging.getLogger(__name__)

JOB_ID = "signal_adapter"
CHANNEL = "signal"
ENABLED_ENV = "SIGNAL_ENABLED"
API_URL_ENV = "SIGNAL_API_URL"
NUMBER_ENV = "SIGNAL_NUMBER"

CHUNK_CHARS = 1500
MAX_PARTS = 3
MAX_SOURCES = 5
SOURCE_TITLE_CHARS = 60
HISTORY_MESSAGES = 8
SEND_TIMEOUT_S = 15.0
RECEIVE_MAX_BYTES = 256 * 1024
RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 60.0
BUSY_CODES = frozenset({"busy", "model_busy"})

# Docker / compose service names are single labels; any dotted public name,
# public IP or non-http scheme is refused.
_PRIVATE_NAMES = frozenset({"localhost", "host.lima.internal", "host.docker.internal"})
_PRIVATE_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa", ".localhost", ".test")
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_counters: Counter[str] = Counter()


def counters() -> dict[str, int]:
    return dict(_counters)


# ── texts ─────────────────────────────────────────────────────────────────

TEXTS: dict[str, dict[str, str]] = {
    "neutral": {
        "sk": "Tento kontakt nie je pripojený. Pripojte Signal najprv v nástenke Oncofiles "
        "(Pripojiť Signal) a pošlite sem kód, ktorý dostanete.",
        "en": "This contact is not connected. Connect Signal in the Oncofiles dashboard "
        "first (Connect Signal) and send the code you get there.",
    },
    "code_invalid": {
        "sk": "Kód nie je platný alebo už vypršal. Vytvorte nový v nástenke Oncofiles.",
        "en": "The code is not valid or has expired. Create a new one in the Oncofiles dashboard.",
    },
    "bound": {
        "sk": "Pripojené. Teraz sa môžete pýtať na záznamy pacienta: {name}.",
        "en": "Connected. You can now ask about the records of: {name}.",
    },
    "attachments": {
        "sk": "Prílohy zatiaľ nepodporujem.",
        "en": "Attachments are not supported yet.",
    },
    "busy": {
        "sk": "Model je práve zaneprázdnený. Skúste to prosím o chvíľu.",
        "en": "The model is busy right now. Please try again in a moment.",
    },
    "too_many": {
        "sk": "Príliš veľa správ za hodinu. Skúste to prosím neskôr.",
        "en": "Too many messages this hour. Please try again later.",
    },
    "too_long": {
        "sk": "Správa je príliš dlhá. Skráťte prosím otázku.",
        "en": "The message is too long. Please shorten the question.",
    },
    "patients": {
        "sk": "Pripojení pacienti (zmena: pacient <číslo>):",
        "en": "Connected patients (switch: patient <number>):",
    },
    "switched": {"sk": "Aktívny pacient: {name}.", "en": "Active patient: {name}."},
    "no_such_patient": {
        "sk": "Pacient s týmto číslom nie je pripojený.",
        "en": "No connected patient has that number.",
    },
    "active_mark": {"sk": "aktívny", "en": "active"},
    "sources": {"sk": "Zdroje:", "en": "Sources:"},
    "page": {"sk": "s.", "en": "p."},
    "document": {"sk": "dokument", "en": "document"},
}


def _t(key: str, lang: str, **kw: str) -> str:
    texts = TEXTS[key]
    return texts.get(lang, texts["sk"]).format(**kw)


# ── settings ──────────────────────────────────────────────────────────────


def enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def api_url_refusal(url: str | None) -> str | None:
    """Why ``SIGNAL_API_URL`` may not be used, or None when its host is private.

    Same idea as the #925 rule of ``LOCAL_LLM_BASE_URL``: loopback, RFC 1918 /
    ULA, ``host.lima.internal`` / ``host.docker.internal``, ``localhost``, a
    private suffix (``.local``, ``.internal``, ``.lan``, ``.home.arpa``,
    ``.localhost``, ``.test``) or a single-label Docker service name. The
    reason names the host only.
    """
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return f"{API_URL_ENV} is not a valid URL"
    if parts.scheme not in ("http", "https"):
        return f"{API_URL_ENV} must be an http(s) URL"
    if not host:
        return f"{API_URL_ENV} has no host"
    if parts.username or parts.password:
        return f"{API_URL_ENV} must not carry credentials"
    if host in _PRIVATE_NAMES or any(host.endswith(s) for s in _PRIVATE_SUFFIXES):
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback or any(ip in net for net in _PRIVATE_NETS):
            return None
    elif "." not in host and _LABEL.match(host):
        return None  # a Docker service name on the app's private network
    return f"{API_URL_ENV} host {host[:64]!r} is not loopback, private or a service name"


def missing_settings() -> list[str]:
    """Names of the settings that keep the adapter off (empty = may start).
    Names only, never a value."""
    missing = []
    if not enabled():
        missing.append(ENABLED_ENV)
    url = os.environ.get(API_URL_ENV, "").strip()
    if not url:
        missing.append(API_URL_ENV)
    elif api_url_refusal(url) is not None:
        missing.append(f"{API_URL_ENV} (not a private host)")
    if not sb.is_e164(os.environ.get(NUMBER_ENV, "").strip()):
        missing.append(NUMBER_ENV)
    if not secrets_keys.signal_secret_set():
        missing.append(f"{secrets_keys.SIGNAL_SECRET_ENV} or HOLDING_STORE_SECRET")
    return missing


# ── the signal-cli client ─────────────────────────────────────────────────


class SignalClient:
    """Minimal client of ``bbernhard/signal-cli-rest-api`` in json-rpc mode.

    ``receive()`` yields decoded envelopes from the websocket; ``send()``
    posts one text message. Both are small enough to replace with a fake in
    tests (``transport`` for httpx, ``connect`` for the websocket).
    """

    def __init__(
        self,
        base_url: str,
        number: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        reason = api_url_refusal(base_url)
        if reason is not None:
            raise ValueError(reason)
        self.base_url = base_url.rstrip("/")
        self.number = number
        self._transport = transport
        self._connect = connect

    def receive_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        rest = self.base_url.split("://", 1)[1]
        return f"{scheme}://{rest}/v1/receive/{quote(self.number, safe='')}"

    async def receive(self) -> AsyncIterator[dict]:
        if self._connect is None:
            from websockets.asyncio.client import connect
        else:
            connect = self._connect
        async with connect(self.receive_url(), max_size=RECEIVE_MAX_BYTES, open_timeout=10) as ws:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    _counters["receive_bad_json"] += 1
                    continue
                if isinstance(data, dict):
                    yield data

    async def send(self, recipient: str, text: str) -> bool:
        """One message; True when signal-cli accepted it. Never raises."""
        target = recipient.removeprefix("uuid:")
        body = {"message": text, "number": self.number, "recipients": [target]}
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=SEND_TIMEOUT_S, follow_redirects=False
            ) as client:
                resp = await client.post(f"{self.base_url}/v2/send", json=body)
        except httpx.HTTPError as exc:
            _counters["send_failed"] += 1
            logger.warning("signal: send failed (%s)", type(exc).__name__)
            return False
        if resp.status_code >= 300:
            _counters["send_failed"] += 1
            logger.warning("signal: send refused (HTTP %d)", resp.status_code)
            return False
        _counters["sent"] += 1
        return True


# ── message parsing ───────────────────────────────────────────────────────


def parse_envelope(data: dict) -> tuple[str | None, str, int] | None:
    """``(sender address, text, attachment count)`` of a direct data message,
    None for anything else (group, sync, receipt, typing, malformed)."""
    env = data.get("envelope") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return None
    msg = env.get("dataMessage")
    if not isinstance(msg, dict):
        return None
    if msg.get("groupInfo") or msg.get("groupV2") or msg.get("groupContext"):
        return None
    sender = sb.normalize_address(env.get("sourceNumber") or env.get("source") or "")
    if sender is None:
        sender = sb.normalize_address(env.get("sourceUuid") or "")
    text = msg.get("message")
    text = text.strip() if isinstance(text, str) else ""
    attachments = msg.get("attachments")
    n_att = len(attachments) if isinstance(attachments, list) else 0
    return sender, text, n_att


_EN_WORDS = frozenset(
    {
        "the",
        "what",
        "is",
        "are",
        "my",
        "when",
        "how",
        "which",
        "show",
        "last",
        "latest",
        "result",
        "results",
        "please",
        "did",
        "was",
        "were",
        "do",
        "does",
        "have",
        "has",
        "of",
        "value",
        "values",
        "from",
        "with",
        "about",
    }
)
_SK_LETTERS = set("áäčďéíĺľňóôŕšťúýž")


def detect_lang(text: str) -> str:
    """``en`` only when the message clearly reads English; Slovak otherwise."""
    lower = text.lower()
    if any(ch in _SK_LETTERS for ch in lower):
        return "sk"
    words = re.findall(r"[a-z']+", lower)
    hits = sum(1 for w in words if w in _EN_WORDS)
    return "en" if hits >= 2 or (hits == 1 and len(words) <= 3) else "sk"


_SWITCH = re.compile(r"^(pacient|pacientka|patient)(?:\s+#?(\d{1,2}))?[.!]?$")


def _fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def parse_switch(text: str) -> tuple[bool, int | None]:
    """``(is_switch_command, number or None)``."""
    m = _SWITCH.match(_fold(text))
    if not m:
        return False, None
    return True, int(m.group(2)) if m.group(2) else None


# ── answer formatting ─────────────────────────────────────────────────────


def chunk_text(text: str, lang: str, limit: int = CHUNK_CHARS, max_parts: int = MAX_PARTS):
    """Split on paragraphs into at most ``max_parts`` parts of at most ``limit``
    characters. When it does not fit, the last part is cut and ends with the
    disclaimer, so it is never lost."""
    from oncofiles.chat import prompts

    pieces: list[str] = []
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = para.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            pieces.append(para[:cut].rstrip())
            para = para[cut:].strip()
        if para:
            pieces.append(para)
    parts: list[str] = []
    for piece in pieces:
        if parts and len(parts[-1]) + 2 + len(piece) <= limit:
            parts[-1] += "\n\n" + piece
        else:
            parts.append(piece)
    if len(parts) <= max_parts:
        return parts
    disclaimer = prompts.DISCLAIMER.get(lang, prompts.DISCLAIMER["en"])
    tail = "…\n\n" + disclaimer
    kept = parts[:max_parts]
    kept[-1] = kept[-1][: max(0, limit - len(tail))].rstrip() + tail
    return kept


async def source_lines(db, patient_id: str, citations: list[dict], lang: str) -> list[str]:
    """Short source lines: document, date, page. Documents are read only for
    THIS patient (a foreign id simply gives the generic label)."""
    lines, seen = [], set()
    for c in citations:
        key = (c.get("document_id"), c.get("page"), c.get("date"))
        if key in seen:
            continue
        seen.add(key)
        title = None
        doc_id = c.get("document_id")
        if isinstance(doc_id, int):
            try:
                doc = await db.get_document(doc_id, patient_id=patient_id)
            except Exception:  # noqa: BLE001 - a source line never breaks the answer
                doc = None
            name = getattr(doc, "filename", None) if doc else None
            if name:
                title = name.rsplit(".", 1)[0][:SOURCE_TITLE_CHARS]
        if not title:
            title = f"{_t('document', lang)} #{doc_id}" if doc_id is not None else c.get("label")
        bits = [str(title or "")]
        if c.get("date"):
            bits.append(str(c["date"]))
        if c.get("page"):
            bits.append(f"{_t('page', lang)} {c['page']}")
        lines.append("• " + ", ".join(b for b in bits if b))
        if len(lines) >= MAX_SOURCES:
            break
    return lines


async def format_answer(db, patient_id: str, result: dict, lang: str) -> list[str]:
    from oncofiles.chat import prompts

    text = (result.get("text") or "").strip()
    disclaimer = prompts.DISCLAIMER.get(lang, prompts.DISCLAIMER["en"])
    if disclaimer not in text:
        text = f"{text}\n\n{disclaimer}" if text else disclaimer
    lines = await source_lines(db, patient_id, result.get("citations") or [], lang)
    if lines:
        # sources go before the disclaimer, which stays the last paragraph
        body = text[: text.rfind(disclaimer)].rstrip()
        text = f"{body}\n\n{_t('sources', lang)}\n" + "\n".join(lines) + f"\n\n{disclaimer}"
    return chunk_text(text, lang)


# ── chat history (#1022) ──────────────────────────────────────────────────


async def load_history(db, patient_id: str, conversation_id: str) -> list[dict]:
    """The conversation's recent turns as chat-core messages (oldest first).
    Empty when the history module is not there or fails: the turn still runs."""
    try:
        from oncofiles import chat_history
    except ImportError:
        return []
    try:
        rows = await chat_history.conversation_messages(
            db, patient_id=patient_id, conversation_id=conversation_id, limit=HISTORY_MESSAGES
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("signal: history read failed (%s)", type(exc).__name__)
        return []
    return [
        {"role": r["role"], "content": r["text"]}
        for r in rows
        if r.get("role") in ("user", "assistant") and isinstance(r.get("text"), str)
    ]


async def store_turn(db, patient_id: str, conversation_id: str, role: str, text: str, **kw):
    try:
        from oncofiles import chat_history
    except ImportError:
        return None
    try:
        return await chat_history.store_message(
            db,
            patient_id=patient_id,
            conversation_id=conversation_id,
            channel=CHANNEL,
            role=role,
            text=text,
            **kw,
        )
    except Exception as exc:  # noqa: BLE001 - history never breaks the answer
        logger.warning("signal: history write failed (%s)", type(exc).__name__)
        return None


def fit_messages(history: list[dict], question: str) -> list[dict]:
    """History (newest kept) + the question, within the core's bounds."""
    from oncofiles.chat import core

    out = [{"role": "user", "content": question}]
    total = len(question.encode())
    for msg in reversed(history):
        content = msg["content"].strip()
        size = len(content.encode())
        if (
            not content
            or size > core.MAX_MESSAGE_BYTES
            or total + size > core.MAX_TOTAL_BYTES
            or len(out) >= core.MAX_MESSAGES
        ):
            break
        out.insert(0, {"role": msg["role"], "content": content})
        total += size
    return out


# ── one message ───────────────────────────────────────────────────────────


async def _answer(db, client, binding: dict, ph: str, sender: str, text: str, lang: str) -> str:
    from oncofiles.chat import core as chat_core

    pid = binding["patient_id"]
    to = sb.reply_address(binding) or sender
    if len(text.encode()) > chat_core.MAX_MESSAGE_BYTES:
        await client.send(to, _t("too_long", lang))
        return "too_long"
    conv = await sb.conversation_for(db, binding)
    history = await load_history(db, pid, conv)
    result = await chat_core.answer_text(
        db,
        patient_id=pid,
        principal="signal:" + ph[:12],
        messages=fit_messages(history, text),
        lang=lang,
    )
    err = result.get("error")
    if err:
        code = err.get("code") if isinstance(err, dict) else "internal_error"
        if code in BUSY_CODES:
            await client.send(to, _t("busy", lang))
        else:
            await client.send(to, str(err.get("message") or _t("busy", lang)))
        return f"error:{code}"
    parts = await format_answer(db, pid, result, lang)
    for part in parts:
        if not await client.send(to, part):
            return "send_failed"
    await store_turn(db, pid, conv, "user", text)
    await store_turn(
        db,
        pid,
        conv,
        "assistant",
        result.get("text") or "",
        citations=[
            {"document_id": c.get("document_id"), "page": c.get("page")}
            for c in result.get("citations") or []
        ],
        model=(result.get("done") or {}).get("model"),
    )
    return f"answered:{len(parts)}"


async def handle_envelope(db, client, data: dict) -> str:
    """Process one received envelope; returns an outcome code (logged, tested)."""
    parsed = parse_envelope(data)
    if parsed is None:
        return "ignored"
    sender, text, n_att = parsed
    if sender is None:
        return "bad_sender"
    ph = sb.phone_hash(sender)
    lang = detect_lang(text) if text else "sk"
    rate = await sb.note_message(db, ph)
    if rate == "limited":
        return "rate_limited"
    if rate == "limited_notice":
        await client.send(sender, _t("too_many", lang))
        return "rate_limited_notice"

    if text and sb.looks_like_code(text):
        if not await sb.code_attempt_allowed(db, ph):
            return "code_attempts_exhausted"
        result = await sb.bind_from_message(db, sender, text)
        if result.status in ("bound", "already"):
            await client.send(sender, _t("bound", lang, name=result.display_name or ""))
            return result.status
        await sb.note_failed_code(db, ph)
        logger.info("signal: code refused for sender %s (%s)", ph[:8], result.reason)
        await client.send(sender, _t("code_invalid", lang))
        return "code_refused"

    binding = await sb.active_binding(db, sender)
    if binding is None:
        if await sb.claim_neutral_reply(db, ph):
            await client.send(sender, _t("neutral", lang))
            return "neutral_reply"
        return "unknown_sender"

    to = sb.reply_address(binding) or sender
    if n_att:
        await client.send(to, _t("attachments", lang))
        if not text:
            return "attachments"
    if not text:
        return "empty"

    is_switch, number = parse_switch(text)
    if is_switch:
        if number is not None:
            chosen = await sb.select_patient(db, sender, number)
            if chosen is not None:
                await client.send(to, _t("switched", lang, name=chosen["display_name"]))
                return "switched"
        rows = await sb.bound_patients(db, sender)
        lines = [] if number is None else [_t("no_such_patient", lang)]
        lines.append(_t("patients", lang))
        for i, r in enumerate(rows, 1):
            mark = f" ({_t('active_mark', lang)})" if r["active"] else ""
            lines.append(f"{i}. {r['display_name']}{mark}")
        await client.send(to, "\n".join(lines))
        return "patients_listed"

    return await _answer(db, client, binding, ph, sender, text, lang)


# ── the loop ──────────────────────────────────────────────────────────────


def build_client() -> SignalClient:
    return SignalClient(os.environ.get(API_URL_ENV, "").strip(), os.environ[NUMBER_ENV].strip())


async def signal_loop(db, client: SignalClient | None = None, *, grace_s: float | None = None):
    """Receive forever (until cancelled); one message at a time."""
    if grace_s is None:
        from oncofiles.tools.curated import STARTUP_GRACE_S

        grace_s = STARTUP_GRACE_S
    if grace_s:
        await asyncio.sleep(grace_s)
    client = client or build_client()
    backoff = RECONNECT_MIN_S
    while True:
        try:
            async for data in client.receive():
                backoff = RECONNECT_MIN_S
                try:
                    outcome = await handle_envelope(db, client, data)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one message never kills the loop
                    outcome = "error"
                    logger.warning("signal: message failed (%s)", type(exc).__name__)
                _counters[outcome.split(":", 1)[0]] += 1
            _counters["receive_closed"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect with back-off
            _counters["receive_failed"] += 1
            logger.warning(
                "signal: receive failed (%s), retry in %.0fs", type(exc).__name__, backoff
            )
        await asyncio.sleep(backoff)
        backoff = min(RECONNECT_MAX_S, backoff * 2)


def start_reason() -> str | None:
    """None when the adapter may start; else one warning line (names only)."""
    missing = missing_settings()
    if not missing:
        return None
    if missing == [ENABLED_ENV]:
        return "signal adapter off (SIGNAL_ENABLED is not true)"
    return "signal adapter not started; missing or invalid: " + ", ".join(missing)
