"""Review reasons of a curated sidecar as short codes (#919).

A sidecar's ``review_reasons`` are sentences written by the runner
(``classify.validate``, the vision notes of #609, the runner's own notes), and
some of them quote document content (a doctor's or another person's name, a
date). The read model and the dashboard must never carry that text, so the
indexer keeps only the codes this module derives from it: a fixed vocabulary,
matched by the sentence's start, ``other`` for anything unknown.

Pure functions, no I/O. Output is codes only, never input text.
"""

from __future__ import annotations

import re

# Safe shape of a code: lower case, digits and underscores, short.
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
MAX_CODES = 10
OTHER = "other"

# (code, pattern on the reason sentence). First match wins; order matters only
# where two patterns could both match.
_RULES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (code, re.compile(rx))
    for code, rx in (
        ("name_not_standard", r"^name_not_standard\b"),
        ("no_date", r"^no valid clinical date"),
        ("birth_date", r"^date \S+ is the patient's birth date"),
        ("future_date", r"^date \S+ is in the future"),
        ("implausible_date", r"^date \S+ is implausible"),
        ("date_not_printed", r"^date \S+ does not appear in the document text"),
        ("date_too_old", r"^date \S+ is more than \d+ years old"),
        ("unknown_type", r"^doc_type .* not in contract"),
        ("institution_unknown", r"^institution unknown"),
        ("near_vocabulary", r"^new (institution|doctor) .* is close to README token"),
        ("new_vocabulary", r"^new (institution|doctor) .* not in README vocabulary"),
        ("split_needed", r"^looks like several documents"),
        ("low_confidence", r"^model confidence "),
        ("other_person", r"^document names another person"),
        ("chart_page", r"^chart on page "),
        ("rotated_region", r"^rotated region on page "),
        ("orientation_unsure", r"^orientation unsure on page "),
        ("extraction_failed", r"^extraction failed:"),
        ("classification_failed", r"^classification failed:"),
        ("no_text", r"^no text to classify"),
        ("raw_genotype_excluded", r"^raw_genotype_excluded\b"),
        ("target_folder_missing", r"^target folder .* does not exist"),
    )
)
KNOWN_CODES = frozenset(code for code, _ in _RULES) | {OTHER}


def code_of(reason) -> str:
    """One reason (sentence or code) → a code from ``KNOWN_CODES``."""
    if not isinstance(reason, str):
        return OTHER
    text = reason.strip()
    if text in KNOWN_CODES:
        return text
    for code, rx in _RULES:
        if rx.search(text):
            return code
    return OTHER


def codes_of(reasons) -> list[str]:
    """The distinct codes of a ``review_reasons`` value, in order, at most ``MAX_CODES``.

    Anything that is not a list (a malformed front-matter value) gives ``[]``.
    Every code returned matches ``CODE_RE``.
    """
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list):
        return []
    out: list[str] = []
    for r in reasons:
        code = code_of(r)
        if code not in out and CODE_RE.match(code):
            out.append(code)
        if len(out) >= MAX_CODES:
            break
    return out


def safe_codes(value) -> list[str]:
    """Codes read back from stored metadata: only known codes of the safe shape."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        if isinstance(v, str) and v in KNOWN_CODES and CODE_RE.match(v) and v not in out:
            out.append(v)
        if len(out) >= MAX_CODES:
            break
    return out
