"""Allowlist redaction of tool arguments for the audit trail (#758).

``activity_log.input_summary`` used to hold the first 100 characters of every
tool argument, which for most tools is health data (search text, clinical
values, SQL, filenames, notes). Only values that are safe by type or by name
are kept now; everything else is written as ``<redacted:len=N>``. The format
stays ``k=v, k=v`` so ``search_activity_log`` / ``get_activity_stats`` /
``qa_analysis`` keep working.

Safe values:
- ``None`` and booleans (any key);
- integers under an id key (``*_id``) or a paging/size key (``limit``, ...);
- lists of integers under ``*_ids`` keys;
- ``patient_slug`` / ``target_patient_slug`` when slug-shaped;
- known enum/option values (``category``, ``kinds``, ``filter``, ``sort``, ...);
- the LLM extractor override modes (``thinking``, ``sampling``, ``prefill``,
  ``reasoning_marker``) and ``timeout_s`` (#769);
- fact codes matching ``^[A-Z0-9_]{1,20}$`` (``code``, ``codes``, ``parameters``).

The same rules also scrub the values that are NOT safe out of the error
message stored with the row and out of every log record emitted while the
tool call runs (FastMCP logs argument-validation errors with their input).
"""

from __future__ import annotations

import ast
import contextvars
import logging
import re
from collections.abc import Iterable
from typing import Any

from oncofiles.models import DocumentCategory, EntryType, PromptCallType

MAX_SUMMARY_LEN = 500
MAX_VALUE_LEN = 100

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_CODE_RE = re.compile(r"[A-Z0-9_]{1,20}")
# Markers this module writes; kept verbatim so a rewrite is idempotent.
_MARKER_RE = re.compile(r"<redacted(?::len=\d+)?>|<(?:ids|more):count=\d+>")
# Splits a stored ``k=v, k=v`` summary at the start of each ``key=``.
_PART_SPLIT = re.compile(r", (?=[A-Za-z_][A-Za-z0-9_]{0,63}=)")
# pydantic: ``input_value='...', input_type=str`` (the repr may be truncated).
_PYDANTIC_INPUT_RE = re.compile(r"input_value=.*?(?=, input_type=|\]|$)", re.MULTILINE)
_PYDANTIC_INPUT_DICT_RE = re.compile(r"'input': .*?(?=, 'ctx': |\}(?:, \{|\]|$))", re.DOTALL)

SLUG_KEYS = frozenset({"patient_slug", "target_patient_slug"})
INT_KEYS = frozenset(
    {
        "limit",
        "offset",
        "batch_size",
        "days",
        "max_actions",
        "max_docs",
        "passes",
        "top_tags_limit",
        "duration_ms",
        # LLM extractor A/B override (#767/#770): seconds, validated in [30, 900]
        "timeout_s",
    }
)
CODE_KEYS = frozenset({"code", "codes", "parameters"})
ENUM_VALUES: dict[str, frozenset[str]] = {
    "category": frozenset(c.value for c in DocumentCategory),
    "category_in": frozenset(c.value for c in DocumentCategory),
    "kinds": frozenset(
        {
            "encounter",
            "document",
            "treatment",
            "condition",
            "procedure",
            "genetic_variant",
            "biomarker",
            "medication",
            "lab",
            "tumor_marker",
        }
    ),
    "filter": frozenset(
        {
            "all",
            "missing_ocr",
            "missing_ai",
            "missing_metadata",
            "missing_date",
            "missing_institution",
            "not_synced",
            "not_renamed",
            "incomplete",
        }
    ),
    "sort": frozenset({"asc", "desc"}),
    "entry_type": frozenset(e.value for e in EntryType),
    "participant": frozenset(
        {"claude.ai", "claude-code", "chatgpt", "gemini", "oncoteam", "other"}
    ),
    "call_type": frozenset(c.value for c in PromptCallType),
    "record_type": frozenset(
        {
            "lab",
            "finding",
            "medication",
            "treatment_event",
            "biomarker",
            "condition",
            "procedure",
            "imaging",
            "pathology",
            "genetic_variant",
            "allergy",
            "vital",
        }
    ),
    # Operational statuses only; clinical ones ('high', 'abnormal') stay redacted.
    "status": frozenset(
        {"ok", "error", "timeout", "RECRUITING", "ACTIVE_NOT_RECRUITING", "COMPLETED"}
    ),
    "phase": frozenset({"PHASE1", "PHASE2", "PHASE3", "PHASE4"}),
    "mime_type": frozenset({"application/pdf", "image/jpeg", "image/png", "text/plain"}),
    # LLM extractor A/B overrides (#767/#770), mirrored from
    # curated.llm_extract_call (*_MODES; a test keeps them equal). Without them
    # e.g. thinking="off" became a call secret and every log line of the job
    # showed "thinking=<redacted:len=3>" (#769).
    "thinking": frozenset({"on", "off"}),
    "sampling": frozenset({"greedy", "qwen"}),
    "prefill": frozenset({"on", "off"}),
    "reasoning_marker": frozenset({"on", "off"}),
}


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _as_token(v: Any) -> str | None:
    """A str (or an int that a stored summary turned back into one) as text."""
    if isinstance(v, str):
        return v
    if _is_int(v):
        return str(v)
    return None


def _token_ok(key: str, v: Any) -> bool:
    tok = _as_token(v)
    if tok is None:
        return False
    if key in SLUG_KEYS:
        return bool(_SLUG_RE.fullmatch(tok))
    if key in CODE_KEYS:
        return all(_CODE_RE.fullmatch(p.strip()) for p in tok.split(","))
    allowed = ENUM_VALUES.get(key)
    return allowed is not None and tok in allowed


def is_safe_value(key: str, v: Any) -> bool:
    """True when ``v`` under ``key`` may be stored in the audit trail verbatim."""
    if v is None or isinstance(v, bool):
        return True
    if _is_int(v):
        return key.endswith("_id") or key in INT_KEYS or _token_ok(key, v)
    if isinstance(v, list):
        if key.endswith("_ids"):
            return all(_is_int(i) for i in v)
        if key in CODE_KEYS or key in ENUM_VALUES:
            return all(_token_ok(key, i) for i in v)
        return False
    if isinstance(v, str):
        return _token_ok(key, v)
    return False


def _redacted(v: Any) -> str:
    return f"<redacted:len={len(str(v))}>"


def render_value(key: str, v: Any) -> str:
    if isinstance(v, str) and _MARKER_RE.fullmatch(v):
        return v
    if not is_safe_value(key, v):
        return _redacted(v)
    text = str(v)
    if len(text) > MAX_VALUE_LEN:
        # Only an id list can be this long; a count keeps it parseable.
        return f"<ids:count={len(v)}>" if isinstance(v, list) else _redacted(v)
    return text


def _join(parts: list[str]) -> str:
    out: list[str] = []
    size = 0
    for i, part in enumerate(parts):
        extra = len(part) + (2 if out else 0)
        if size + extra > MAX_SUMMARY_LEN - 20:
            out.append(f"<more:count={len(parts) - i}>")
            break
        out.append(part)
        size += extra
    return ", ".join(out)


def _render_pairs(pairs: Iterable[tuple[str | None, Any]]) -> str:
    parts: list[str] = []
    for key, v in pairs:
        if key is None:  # a fragment without a key (stored summaries only)
            parts.append(v if _MARKER_RE.fullmatch(v) else _redacted(v))
        elif not _KEY_RE.fullmatch(key):
            parts.append(_redacted(f"{key}={v}"))
        else:
            parts.append(f"{key}={render_value(key, v)}")
    return _join(parts)


def summarize_args(args: Any) -> str:
    """The ``input_summary`` for a tool call's argument dict (allowlist)."""
    if not isinstance(args, dict) or not args:
        return ""
    return _render_pairs((str(k), v) for k, v in args.items())


_LITERAL_RE = re.compile(r"-?\d{1,18}|True|False|None|\[[^\n]{0,4000}\]")


def _parse_stored_value(text: str) -> Any:
    """Turn a stored ``str(v)`` back into a value where it is a plain literal."""
    if _LITERAL_RE.fullmatch(text):
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return text
    return text


def redact_stored_summary(summary: str | None) -> str:
    """Rewrite a stored ``input_summary`` into the allowlist format.

    Used by ``redact_activity_log_inputs`` on rows written before #758.
    Idempotent: a summary already in this format comes back unchanged.
    """
    if not summary:
        return summary or ""
    pairs: list[tuple[str | None, Any]] = []
    for frag in _PART_SPLIT.split(summary):
        key, sep, raw = frag.partition("=")
        if sep and _KEY_RE.fullmatch(key):
            pairs.append((key, _parse_stored_value(raw)))
        else:
            pairs.append((None, frag))
    return _render_pairs(pairs)


# ── scrubbing free text (error messages, log records) ──────────────────────

_MIN_SCRUB_LEN = 3

# The sensitive argument strings of the tool call running in this context.
_call_secrets: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "audit_call_secrets", default=()
)


def _leaves(v: Any) -> Iterable[Any]:
    if isinstance(v, dict):
        for k, item in v.items():
            yield k
            yield from _leaves(item)
    elif isinstance(v, (list, tuple, set)):
        for item in v:
            yield from _leaves(item)
    else:
        yield v


def sensitive_strings(args: Any) -> tuple[str, ...]:
    """Every text form of the non-allowlisted argument values, longest first."""
    if not isinstance(args, dict):
        return ()
    found: set[str] = set()
    for key, v in args.items():
        if is_safe_value(str(key), v):
            continue
        for leaf in _leaves(v):
            if leaf is None or isinstance(leaf, bool):
                continue
            text = str(leaf)
            for form in (text, repr(text)[1:-1]):
                if len(form) >= _MIN_SCRUB_LEN:
                    found.add(form)
        whole = str(v)
        if len(whole) >= _MIN_SCRUB_LEN and not isinstance(v, (int, float)):
            found.add(whole)
    return tuple(sorted(found, key=len, reverse=True))


def scrub_text(text: str | None, secrets: Iterable[str] = ()) -> str | None:
    """Remove argument values (and pydantic ``input_value=``) from ``text``."""
    if not text:
        return text
    for s in secrets:
        if s in text:
            text = text.replace(s, f"<redacted:len={len(s)}>")
    text = _PYDANTIC_INPUT_RE.sub("input_value=<redacted>", text)
    return _PYDANTIC_INPUT_DICT_RE.sub("'input': <redacted>", text)


def set_call_secrets(secrets: tuple[str, ...]) -> contextvars.Token:
    return _call_secrets.set(secrets)


def reset_call_secrets(token: contextvars.Token) -> None:
    _call_secrets.reset(token)


_installed = False


def install_log_redaction() -> None:
    """Scrub tool-call argument values from every log record made during a call.

    Wraps the global LogRecord factory. Outside a tool call (no secrets in the
    context) records are untouched. Inside one, the message is formatted and
    scrubbed, and an attached traceback is rendered to scrubbed text.
    """
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        secrets = _call_secrets.get()
        if not secrets:
            return record
        try:
            record.msg = scrub_text(record.getMessage(), secrets)
            record.args = None
            if record.exc_info:
                tb = logging.Formatter().formatException(record.exc_info)
                record.exc_text = scrub_text(tb, secrets)
                record.exc_info = None
        except Exception:  # never break logging; fall back to a bare message
            record.msg = "<log record redacted>"
            record.args = None
            record.exc_info = None
            record.exc_text = None
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
