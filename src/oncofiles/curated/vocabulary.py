"""Automatic README vocabulary for curated folders (#854).

Maintainer decision 2026-11-16: when a document's institution or doctor is not
in the README §3 vocabulary, the curated pipeline adds it there itself and
files the document, instead of leaving it in ``00-inbox`` for review. A new
user's README starts with an empty vocabulary, so without this almost every
document of a first import would wait for a human.

Guards (``classify.validate`` decides, this module only edits the README):

* the value is printed in the document's own text — exact match of the printed
  spelling, ignoring diacritics, case and whitespace runs, on word boundaries,
  outside a birth-date context; never from the filename;
* never the patient's own given name or surname (nor a word built on it);
* close to an existing token (≤ 2 edits, same entity per #754) → the existing
  token; close but a different entity → review; ``?`` in the reading → ``Unknown``;
* the README is edited only when this code knows its layout: a ``Standard
  version`` line, a ``## 3.`` section and at most one well-formed managed block
  of a known block version. Anything else → ``AutoVocabUnsupportedError`` and the
  document goes to review as before (fail closed);
* the edit is an insertion into §3 only; the new README is parsed again and must
  equal the old contract plus exactly the new tokens.

The runner writes the README inside ``contract_writes()`` through
``drive_ops.update_text`` (read-back verified, never retried), journaled in the
canary before the write. Server logs only ever see counts and token hashes.

Kill switch: ``CURATED_AUTO_VOCAB=false`` restores the #754 behaviour (a new
institution/doctor → review).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from oncofiles.curated.contract import Contract, ContractError, parse_contract

BLOCK_VERSION = "v1"
BLOCK_BEGIN = f"<!-- oncofiles:auto-vocabulary {BLOCK_VERSION} -->"
BLOCK_END = "<!-- /oncofiles:auto-vocabulary -->"
_BEGIN_RE = re.compile(r"<!-- oncofiles:auto-vocabulary (?P<v>\S+) -->")
_HEADING_RE = re.compile(r"^## ", re.M)
_TOKEN_SHAPE_RE = re.compile(r"[A-Z][A-Za-z0-9]+")
LABELS = ("Institution", "Doctor")
PRINTED_MAX = 80


class AutoVocabUnsupportedError(ValueError):
    """This README cannot be edited safely — the document goes to review instead."""


@dataclass(frozen=True)
class VocabAddition:
    label: str  # "Institution" | "Doctor"
    token: str  # CamelCase ASCII, the README token
    printed: str  # the spelling printed in the document (Drive only, never logs)


def enabled() -> bool:
    """``CURATED_AUTO_VOCAB`` (default true). Read at call time: an env change
    needs only a container recreate."""
    value = os.environ.get("CURATED_AUTO_VOCAB", "true").strip().lower()
    return value in ("1", "true", "yes", "on")


def token_digest(token: str) -> str:
    """What server logs may say about a token: its length and a short hash."""
    return f"len{len(token)}:{hashlib.sha256(token.encode()).hexdigest()[:8]}"


def _section3_span(text: str) -> tuple[int, int] | None:
    """(start of §3 body, end of §3 body) in ``text``; None without a ``## 3.``."""
    heads = list(_HEADING_RE.finditer(text))
    for i, h in enumerate(heads):
        line_end = text.find("\n", h.start())
        line_end = len(text) if line_end < 0 else line_end
        if text[h.end() : line_end].startswith("3."):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return line_end, end
    return None


def unsupported_reason(readme_text: str, contract: Contract) -> str | None:
    """Why this README must not be auto-edited, or None when it may be."""
    if contract.version == "unknown":
        return "README has no 'Standard version' line"
    span = _section3_span(readme_text)
    if span is None:
        return "README has no '## 3.' vocabulary section"
    begins = list(_BEGIN_RE.finditer(readme_text))
    ends = [m.start() for m in re.finditer(re.escape(BLOCK_END), readme_text)]
    if len(begins) > 1 or len(ends) > 1 or len(begins) != len(ends):
        return "README auto-vocabulary block is malformed"
    if begins:
        b = begins[0]
        if b.group("v") != BLOCK_VERSION:
            return f"README auto-vocabulary block version {b.group('v')!r} is not known"
        start, end = span
        if not (start <= b.start() < ends[0] <= end):
            return "README auto-vocabulary block is outside §3"
    return None


def _clean_printed(value: str) -> str:
    """The printed form, safe on one Markdown line (no backticks, quotes, markers)."""
    text = re.sub(r"[`\"<>\r\n\t]+", " ", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:PRINTED_MAX]


def add_to_readme(
    readme_text: str,
    contract: Contract,
    additions: list[VocabAddition],
    *,
    run_id: str,
    day: str,
) -> tuple[str, Contract]:
    """Return (new README text, its contract) with ``additions`` in §3.

    Pure: nothing is written. The text outside the inserted lines is unchanged,
    and the new contract must equal the old one plus exactly the new tokens.
    Raises ``AutoVocabUnsupportedError`` otherwise.
    """
    if reason := unsupported_reason(readme_text, contract):
        raise AutoVocabUnsupportedError(reason)
    new = []
    seen = set(contract.known_tokens)
    for a in additions:
        if a.label not in LABELS or not _TOKEN_SHAPE_RE.fullmatch(a.token or ""):
            raise AutoVocabUnsupportedError(f"not a vocabulary token ({a.label})")
        if a.token in seen:
            continue
        seen.add(a.token)
        new.append(a)
    if not new:
        return readme_text, contract
    nl = "\r\n" if "\r\n" in readme_text else "\n"
    lines = "".join(
        f"- {day} auto-added by run {run_id}: {a.label} `{a.token}` "
        f'(printed as "{_clean_printed(a.printed)}"){nl}'
        for a in new
    )
    begin = _BEGIN_RE.search(readme_text)
    if begin:
        pos = readme_text.index(BLOCK_END)
        updated = readme_text[:pos] + lines + readme_text[pos:]
    else:
        _, end = _section3_span(readme_text)  # type: ignore[misc]  # checked above
        pos = end
        while pos > 0 and readme_text[pos - 1] in "\r\n \t":
            pos -= 1
        block = (
            f"{nl}{nl}**Auto-added vocabulary** (oncofiles, #854): each value below was "
            f"printed in a filed document. Correct a wrong line here; the pipeline only "
            f"appends.{nl}{nl}{BLOCK_BEGIN}{nl}{lines}{BLOCK_END}"
        )
        updated = readme_text[:pos] + block + readme_text[pos:]
    try:
        after = parse_contract(updated)
    except ContractError as exc:
        raise AutoVocabUnsupportedError(f"edited README does not parse ({exc})") from exc
    want = contract.known_tokens | {a.token for a in new}
    same = (
        after.patient_display == contract.patient_display
        and after.patient_token == contract.patient_token
        and after.version == contract.version
        and after.folders == contract.folders
        and after.birth_dates == contract.birth_dates
        and after.known_tokens == want
    )
    if not same:
        raise AutoVocabUnsupportedError("edited README changes more than the new tokens")
    return updated, after
