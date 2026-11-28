"""Grounding guard of the chat core (#1020): numbers and dates must come from data.

Every number and date in a final answer must appear in a tool result of this
turn or in the question (plus today's date, which the system prompt gives the
model). Tokens are normalised before they are compared:

* dates: ISO, ``d.m.yyyy``, ``d/m/yyyy``, ``10. júna 2027``, ``June 10, 2027``,
  ``10 June 2027`` → ``YYYY-MM-DD``; month + year → ``YYYY-MM``; a date in the
  pool also grounds its year, year-month, month and day numbers;
* numbers: decimal comma = point (``7,9`` = ``7.9``), trailing zeros dropped
  (``4.80`` = ``4.8``); single-digit integers are skipped (ordinals and counts:
  "6. cyklus", "3 dokumenty");
* unit and exponent notation is never a value: ``×10⁹``, ``x 10^9``, ``10^9/L``,
  ``x10E9``, ``*10e12`` and any superscript digit are removed before
  tokenising, so ``×10⁹/L`` is not the number 109;
* marker names with digits (``CA 19-9``, ``CA 125``, ``CA 15-3``) are names.

A number the model computed is allowed only when the two source values it
comes from (difference, sum or percent change) are both in the same answer
and both grounded (``computed_ok``).

Pure functions, no I/O, no logging.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field


def fold(text: str) -> str:
    """Lower case without diacritics (matching only)."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


_MONTHS = {
    "januar": 1, "januara": 1, "january": 1, "jan": 1,
    "februar": 2, "februara": 2, "february": 2, "feb": 2,
    "marec": 3, "marca": 3, "march": 3, "mar": 3,
    "april": 4, "aprila": 4, "apr": 4,
    "maj": 5, "maja": 5, "may": 5,
    "jun": 6, "juna": 6, "june": 6,
    "jul": 7, "jula": 7, "july": 7,
    "august": 8, "augusta": 8, "aug": 8,
    "september": 9, "septembra": 9, "sep": 9, "sept": 9,
    "oktober": 10, "oktobra": 10, "october": 10, "oct": 10,
    "november": 11, "novembra": 11, "nov": 11,
    "december": 12, "decembra": 12, "dec": 12,
}  # fmt: skip
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))


def _iso(y: str, m: int | str, d: int | str | None = None) -> str | None:
    m_i, y_i = int(m), int(y)
    if not 1 <= m_i <= 12 or not 1000 <= y_i <= 9999:
        return None
    if d is None:
        return f"{y_i:04d}-{m_i:02d}"
    d_i = int(d)
    return f"{y_i:04d}-{m_i:02d}-{d_i:02d}" if 1 <= d_i <= 31 else None


_DATE_PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], str | None]]] = [
    (re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)"), lambda m: _iso(m[1], m[2], m[3])),
    (re.compile(r"(?<!\d)(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})(?!\d)"),
     lambda m: _iso(m[3], m[2], m[1])),
    (re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)"), lambda m: _iso(m[3], m[2], m[1])),
    (re.compile(rf"(?<!\d)(\d{{1,2}})\.?\s+({_MONTH_RE})\.?,?\s+(\d{{4}})(?!\d)"),
     lambda m: _iso(m[3], _MONTHS[m[2]], m[1])),
    (re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})(?!\d)"),
     lambda m: _iso(m[3], _MONTHS[m[1]], m[2])),
    (re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{4}})(?!\d)"), lambda m: _iso(m[2], _MONTHS[m[1]])),
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})(?![\d-])"), lambda m: _iso(m[1], m[2])),
]  # fmt: skip

_SUPERSCRIPTS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻"
# "×10⁹", "x 10^9", "x10E9", "*10e12", "· 10^-3", and a bare "10^9" / "10E9" /
# "10⁹" (a unit like "10^9/L"): exponent notation, never a value.
_EXPONENT_RE = re.compile(
    rf"(?:[×xX*·]\s*)?(?<![\d.,])10\s*(?:\^\s*[-−+]?\d+|[eE]\s*[-−+]?\d+\b|[{_SUPERSCRIPTS}]+)",
)
_SUPERSCRIPT_RE = re.compile(f"[{_SUPERSCRIPTS}]+")
_MARKER_NAME_RE = re.compile(r"\bca[\s-]?\d{2,3}(?:\s?[-‑/]\s?\d{1,2})?\b")
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?")


def _norm_number(raw: str) -> str:
    value = float(raw.replace(",", "."))
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _strip_notation(text: str) -> str:
    """Exponent notation and superscripts out, BEFORE folding: NFKD turns a
    superscript nine into a plain 9, so "10⁹" would fold to "109"."""
    text = _EXPONENT_RE.sub(" ", (text or "").replace("−", "-"))
    return _SUPERSCRIPT_RE.sub(" ", text)


def tokens(text: str) -> list[str]:
    """Normalised numbers and dates of a text, in order (see the module docstring)."""
    folded = _MARKER_NAME_RE.sub(" ", fold(_strip_notation(text)))
    out: list[str] = []
    for pattern, conv in _DATE_PATTERNS:

        def repl(m: re.Match, conv=conv) -> str:
            iso = conv(m)
            if iso is None:
                return m[0]
            out.append(iso)
            return " " * len(m[0])

        folded = pattern.sub(repl, folded)
    for m in _NUMBER_RE.finditer(folded):
        num = _norm_number(m[0])
        if re.fullmatch(r"\d", num):
            continue
        out.append(num)
    return out


def pool(texts: Iterable[str]) -> set[str]:
    """Every token of the texts, plus the year, year-month, month and day of
    each date (an answer may say "15. júla" without the year)."""
    found: set[str] = set()
    for text in texts:
        for tok in tokens(text):
            found.add(tok)
            parts = tok.split("-")
            if len(parts) >= 2 and len(parts[0]) == 4:
                found.update({parts[0], f"{parts[0]}-{parts[1]}", str(int(parts[1]))})
                if len(parts) == 3:
                    found.add(str(int(parts[2])))
    return found


def _is_number(tok: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", tok))


def _decimals(tok: str) -> int:
    return len(tok.split(".", 1)[1]) if "." in tok else 0


def computed_ok(value: str, grounded_numbers: list[str]) -> bool:
    """``value`` is a difference, sum or percent change of two grounded numbers
    that both appear in the same answer (rounded to ``value``'s decimals)."""
    if not _is_number(value):
        return False
    x = float(value)
    tol = 0.5 * 10 ** (-_decimals(value)) + 1e-9
    nums = [float(n) for n in dict.fromkeys(grounded_numbers) if _is_number(n)]
    for i, a in enumerate(nums):
        for b in nums[i + 1 :]:
            candidates = [abs(a - b), a + b]
            for base, other in ((a, b), (b, a)):
                if base:
                    candidates.append(abs((other - base) / base * 100))
            if any(abs(c - x) <= tol for c in candidates):
                return True
    return False


@dataclass
class Check:
    """Result of one grounding check (counts are reported, tokens never logged)."""

    checked: int = 0
    ungrounded: list[str] = field(default_factory=list)
    computed_allowed: int = 0

    @property
    def ok(self) -> bool:
        return not self.ungrounded


def check(answer: str, sources: Iterable[str]) -> Check:
    """Which numbers and dates of ``answer`` appear in none of ``sources``."""
    found = pool(sources)
    toks = tokens(answer)
    grounded_numbers = [t for t in toks if t in found and _is_number(t)]
    result = Check(checked=len(toks))
    for tok in toks:
        if tok in found:
            continue
        if computed_ok(tok, grounded_numbers):
            result.computed_allowed += 1
            continue
        if tok not in result.ungrounded:
            result.ungrounded.append(tok)
    return result
