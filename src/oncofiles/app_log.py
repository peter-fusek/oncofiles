"""Persistent, rotating application log (#706).

Container stdout is lost when Coolify recreates the container (every deploy),
so incidents older than the last deploy could not be reconstructed. With
``APP_LOG_DIR`` set (production: ``/app/data/logs``, on the persistent volume)
the root logger also writes to ``<dir>/oncofiles.log``, rotated at UTC midnight
and kept ``APP_LOG_RETENTION_DAYS`` days (default 7). Each day's file is capped
at ``APP_LOG_MAX_MB`` (default 50): past the cap one marker line is written and
records are dropped until the next rotation, so the directory never exceeds
about (days + 1) x cap. stderr keeps receiving everything.

The directory is 0700 and the files 0600 (owner only, the process uid —
10001 in production). Every root handler, stderr and this file alike, carries
the same ``RedactFilter``: e-mail addresses and token-shaped strings are
masked before any line is written.

Off by default (unset ``APP_LOG_DIR``): local dev and tests write no files.
The file handler only changes WHERE lines go, and the code base logs ids,
counts and reasons, never document values (PHI).
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import TimedRotatingFileHandler

LOG_FILENAME = "oncofiles.log"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_MB = 50
DIR_MODE = 0o700
FILE_MODE = 0o600

# ── redaction: one filter on EVERY root handler (stderr and the file) ──

# Log lines can carry attacker-controlled text (filenames, request values), so
# every quantifier is bounded (linear matching, no catastrophic backtracking).
# Order: (1) a hard raw cap (MAX_RAW_CHARS) drops the middle of a huge text
# BEFORE redaction, so no unredacted text ever survives and the cost stays
# bounded; (2) the WHOLE capped string is redacted; (3) the redacted result is
# cut to MAX_REDACT_CHARS (head + tail). At every cut, a token-character run
# the cut splits is replaced by "[cut]", so no fragment of a secret remains.
MAX_REDACT_CHARS = 16 * 1024
_TAIL_CHARS = 4 * 1024  # a traceback's last lines name the actual error
MAX_RAW_CHARS = 256 * 1024
_RAW_TAIL_CHARS = 64 * 1024  # the raw cap also keeps the end of a traceback

_SECRET_KEYS = (
    "access_token|refresh_token|id_token|token|client_secret|api_key|apikey|secret|"
    "password|authorization"
)

_REDACTIONS: list[tuple[re.Pattern, str]] = [
    # e-mail addresses (caregiver logins, OAuth identities); RFC length limits.
    # No lookbehind: a bounded match may start inside a longer run (_x_@a.b).
    (
        re.compile(
            r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}"
            r"(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}"
        ),
        "[email]",
    ),
    # Authorization headers and bearer values
    (re.compile(r"(?i)\b(bearer|basic)[ \t]{1,8}[A-Za-z0-9._~+/=-]{8,4096}"), r"\1 [redacted]"),
    # KEY=value / "key": "value" for secret-named keys, also prefixed ones
    # (RESEND_API_KEY, auth_token, oauth_token). max_tokens=… stays: the key
    # must be followed by the separator.
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9_]{0,64}_)?(?:" + _SECRET_KEYS + r")"
            r"[\"']?[ \t]{0,8}[:=][ \t]{0,8}[\"']?)[^\s\"'&,;}]{1,4096}"
        ),
        r"\1[redacted]",
    ),
    # well-known token shapes: patient bearer tokens, Google access/refresh
    # tokens, Anthropic/OpenAI-style keys, GitHub tokens
    (re.compile(r"\bonco_[A-Za-z0-9_-]{8,512}"), "[token]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]{10,4096}"), "[token]"),
    (re.compile(r"(?<![A-Za-z0-9])1//[A-Za-z0-9_-]{20,1024}"), "[token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,512}"), "[token]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}"), "[token]"),
]

# At a cut, a value must never appear without its line context (a cut between
# "Bearer " and its value leaves a bare value no pattern recognises). So a
# head-side cut drops back to the last newline, a tail-side cut drops forward to
# the next newline, each at most _CUT_LINE_CHARS; without a newline in that
# span, the span is dropped and a non-whitespace run the new cut splits too.
_CUT = "[cut]"
_CUT_LINE_CHARS = 1024


def _redact_all(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def _head(text: str, n: int) -> str:
    """First ``n`` chars, cut back to a line start (see _CUT_LINE_CHARS)."""
    if n >= len(text):
        return text
    nl = text.rfind("\n", max(0, n - _CUT_LINE_CHARS), n)
    if nl >= 0:
        return text[: nl + 1] + _CUT
    k = max(0, n - _CUT_LINE_CHARS)
    if k > 0 and not text[k - 1].isspace() and not text[k].isspace():
        while k > 0 and not text[k - 1].isspace():
            k -= 1
    return text[:k] + _CUT


def _tail(text: str, n: int) -> str:
    """Last ``n`` chars, cut forward to a line end (see _CUT_LINE_CHARS)."""
    start = len(text) - n
    if start <= 0:
        return text
    nl = text.find("\n", start, min(len(text), start + _CUT_LINE_CHARS))
    if nl >= 0:
        return _CUT + text[nl:]
    k = min(len(text), start + _CUT_LINE_CHARS)
    if k < len(text) and not text[k - 1].isspace() and not text[k].isspace():
        while k < len(text) and not text[k].isspace():
            k += 1
    return _CUT + text[k:]


def redact(text: str, limit: int = MAX_REDACT_CHARS) -> str:
    """Cap, mask secrets and e-mail addresses, then cut to about ``limit``
    chars (head + tail with a "…[truncated N chars]" marker)."""
    original = len(text)
    if original > MAX_RAW_CHARS:
        text = _head(text, MAX_RAW_CHARS - _RAW_TAIL_CHARS) + " " + _tail(text, _RAW_TAIL_CHARS)
    red = _redact_all(text)
    tail_n = min(_TAIL_CHARS, limit // 4)
    head_n = limit - tail_n
    if len(red) <= limit:
        if original <= MAX_RAW_CHARS:
            return red
        return f"{red} …[truncated {original - MAX_RAW_CHARS} chars]"
    head, tail = _head(red, head_n), _tail(red, tail_n)
    dropped = max(0, original - head_n - tail_n)
    return f"{head}…[truncated {dropped} chars]{tail}"


class RedactFilter(logging.Filter):
    """Rewrites a record's message (and traceback) with secrets and e-mail
    addresses masked. Never drops a record. Patient UUIDs are not secrets and
    stay (the code logs ids, counts and reasons, never document values)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True  # a broken format string: let the handler report it
        record.msg, record.args = redact(msg), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def attach_redaction(handler: logging.Handler) -> None:
    if not any(isinstance(f, RedactFilter) for f in handler.filters):
        handler.addFilter(RedactFilter())


class CappedDailyFileHandler(TimedRotatingFileHandler):
    """Daily rotation plus a per-file size cap (records past it are dropped).
    Files are created 0600 (owner only), whatever the process umask."""

    def __init__(self, filename: str, *, backup_days: int, max_bytes: int) -> None:
        super().__init__(
            filename, when="midnight", backupCount=backup_days, utc=True, encoding="utf-8"
        )
        self.max_bytes = max_bytes
        self._capped = False

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
        os.fchmod(fd, FILE_MODE)  # also tightens a file left by an older build
        return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)

    def doRollover(self) -> None:  # noqa: N802 (stdlib name)
        super().doRollover()
        self._capped = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.shouldRollover(record):
                self.doRollover()
            if self.max_bytes > 0:
                if self.stream is None:
                    self.stream = self._open()
                if self.stream.tell() >= self.max_bytes:
                    if not self._capped:
                        self._capped = True
                        self.stream.write(
                            "log size cap reached; further lines dropped until rotation\n"
                        )
                        self.flush()
                    return
            logging.FileHandler.emit(self, record)
        except Exception:
            self.handleError(record)


def file_handler_from_env(fmt: str, level: int) -> logging.Handler | None:
    """The persistent handler configured from env, or None when off/unusable."""
    log_dir = os.environ.get("APP_LOG_DIR", "").strip()
    if not log_dir:
        return None
    try:
        days = max(1, int(os.environ.get("APP_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))
        max_mb = max(0, int(os.environ.get("APP_LOG_MAX_MB", DEFAULT_MAX_MB)))
        os.makedirs(log_dir, mode=DIR_MODE, exist_ok=True)
        os.chmod(log_dir, DIR_MODE)  # makedirs honours the umask; an existing dir too
        handler = CappedDailyFileHandler(
            os.path.join(log_dir, LOG_FILENAME), backup_days=days, max_bytes=max_mb * 1024 * 1024
        )
    except (OSError, ValueError) as exc:
        # Never stop the server over a log directory; stderr still works.
        logging.getLogger(__name__).warning(
            "persistent log disabled: %s (%s)", type(exc).__name__, log_dir
        )
        return None
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))
    attach_redaction(handler)  # the same filter as stderr (_setup_logging)
    return handler
