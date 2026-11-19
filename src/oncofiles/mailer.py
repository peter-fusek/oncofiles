"""Outgoing e-mail over SMTP (#834).

The single path for every e-mail Oncofiles sends: the waitlist welcome, the
admin ``[Lead]`` note, onboarding notes and pipeline alerts. Standard library
only (``smtplib`` + ``email.message``); the relay is Brevo in production, but
nothing here is provider-specific beyond the optional ``X-Mailin-Tag`` header,
which other relays ignore.

Configuration is read from the environment at call time:

- ``SMTP_HOST`` (required), ``SMTP_PORT`` (default 587)
- ``SMTP_USER`` / ``SMTP_PASSWORD`` (login is skipped when the user is empty)
- ``SMTP_SSL`` (``true`` = implicit TLS; also implied by port 465). Otherwise
  STARTTLS is mandatory: a relay without STARTTLS fails the send, there is no
  plaintext fallback.
- ``MAIL_FROM`` (required), e.g. ``Oncofiles <noreply@oncofiles.com>``
- ``MAIL_REPLY_TO`` (optional, default Reply-To)
- ``NOTIFY_TO_EMAIL`` (admin alerts; comma-separated list allowed)

Logging rule: never an address, a password, a subject or a body. A send is
logged with its kind, the recipient count and a short hash of the Message-ID;
a failure with the exception class and, for SMTP replies, the numeric code.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_S = 10.0
DEFAULT_SMTP_PORT = 587


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    user: str
    password: str
    mail_from: str
    reply_to: str
    use_ssl: bool


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def load_config() -> MailConfig | None:
    """Return the SMTP config, or None when sending is not configured."""
    host = _env("SMTP_HOST")
    mail_from = _env("MAIL_FROM")
    if not host or not mail_from:
        return None
    raw_port = _env("SMTP_PORT")
    try:
        port = int(raw_port) if raw_port else DEFAULT_SMTP_PORT
    except ValueError:
        logger.warning("Mail disabled: SMTP_PORT is not a number")
        return None
    if not 0 < port < 65536:
        logger.warning("Mail disabled: SMTP_PORT is out of range")
        return None
    use_ssl = _env("SMTP_SSL").lower() in {"1", "true", "yes"} or port == 465
    return MailConfig(
        host=host,
        port=port,
        user=_env("SMTP_USER"),
        password=os.environ.get("SMTP_PASSWORD") or "",
        mail_from=mail_from,
        reply_to=_env("MAIL_REPLY_TO"),
        use_ssl=use_ssl,
    )


def is_configured() -> bool:
    """True when SMTP_HOST and MAIL_FROM are set (a send can be attempted)."""
    return load_config() is not None


def admin_recipients() -> list[str]:
    """Addresses from ``NOTIFY_TO_EMAIL`` (comma-separated), possibly empty."""
    return [a.strip() for a in _env("NOTIFY_TO_EMAIL").split(",") if a.strip()]


def _valid_address(value: str) -> bool:
    if not value or "\r" in value or "\n" in value:
        return False
    _name, addr = parseaddr(value)
    return "@" in addr and " " not in addr


def _one_line(value: str, limit: int = 200) -> str:
    """Collapse control characters so a caller value can never add a header."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(cleaned.split())[:limit]


def _ref(message_id: str) -> str:
    return hashlib.sha256(message_id.encode()).hexdigest()[:12]


def _error_code(exc: BaseException) -> str:
    """Exception class plus SMTP reply code; never the message (it may hold addresses)."""
    code = getattr(exc, "smtp_code", None)
    return f"{type(exc).__name__}:{code}" if code else type(exc).__name__


def _deliver(cfg: MailConfig, msg: EmailMessage, recipients: list[str]) -> None:
    """Blocking SMTP exchange; runs in a worker thread."""
    context = ssl.create_default_context()
    if cfg.use_ssl:
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(
            cfg.host, cfg.port, timeout=SMTP_TIMEOUT_S, context=context
        )
    else:
        smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT_S)
    with smtp:
        if not cfg.use_ssl:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if cfg.user:
            smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg, to_addrs=recipients)


async def send_mail(
    to: list[str],
    subject: str,
    text: str,
    *,
    html: str | None = None,
    reply_to: str | None = None,
    tags: list[str] | None = None,
) -> bool:
    """Send one message; True when the relay accepted it.

    Never raises: a missing config, a bad address or an SMTP failure returns
    False and logs one WARNING without addresses or content. ``tags[0]`` is
    the kind used in the log line; all tags go into ``X-Mailin-Tag``.
    """
    kind = _one_line(tags[0], 40) if tags else "mail"
    cfg = load_config()
    if cfg is None:
        missing = [n for n in ("SMTP_HOST", "MAIL_FROM") if not _env(n)]
        logger.warning(
            "Mail not sent (kind=%s): outgoing mail not configured (missing %s)",
            kind,
            ", ".join(missing) or "valid SMTP_PORT",
        )
        return False
    recipients = [a.strip() for a in to if a and a.strip()]
    reply = reply_to if reply_to is not None else cfg.reply_to
    if (
        not recipients
        or not all(_valid_address(a) for a in recipients)
        or not _valid_address(cfg.mail_from)
        or (reply and not _valid_address(reply))
    ):
        logger.warning(
            "Mail not sent (kind=%s): invalid address in from/to/reply-to (%d recipient(s))",
            kind,
            len(recipients),
        )
        return False

    msg = EmailMessage()
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = _one_line(subject, 250)
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    domain = parseaddr(cfg.mail_from)[1].rpartition("@")[2] or None
    msg["Message-ID"] = make_msgid(domain=domain)
    if reply:
        msg["Reply-To"] = reply
    if tags:
        msg["X-Mailin-Tag"] = ", ".join(_one_line(t, 40) for t in tags if t)
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")

    ref = _ref(msg["Message-ID"])
    try:
        await asyncio.to_thread(_deliver, cfg, msg, recipients)
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never raised
        logger.warning(
            "Mail not sent (kind=%s ref=%s recipients=%d): %s",
            kind,
            ref,
            len(recipients),
            _error_code(exc),
        )
        return False
    logger.info("Mail sent (kind=%s ref=%s recipients=%d)", kind, ref, len(recipients))
    return True
