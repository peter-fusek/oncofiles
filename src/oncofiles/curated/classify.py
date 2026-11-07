"""Classify an extracted document against its folder contract (#562).

The local model proposes fields from the document **text** (never the
filename — README §1b lesson 7: auto-generated names here were invented). The
deterministic ``validate`` step then enforces the hard rules so a model error
can never rename a real medical record into a wrong place:

* ``doc_type`` must be one of the contract's DocumentTypes;
* the date must be a real calendar date, not in the future, not before 1900,
  never a birth date from the README, and must literally appear in the text;
* a suspected multi-document bundle, low confidence, or a missing date /
  type / institution → ``review_needed`` (stays in the inbox, README §6);
* first-time institution / department / doctor tokens are *flagged*, not blocked.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date

from oncofiles.curated import llm
from oncofiles.curated.contract import Contract
from oncofiles.curated.naming import UNKNOWN, NameFields, camel, surname_token

MIN_CONFIDENCE = 0.60
SNAP_CUTOFF = 0.8  # OCR typo tolerance when matching README vocabulary tokens
MAX_TEXT_CHARS = 24_000

SYSTEM_TEMPLATE = """You file medical documents into a patient folder that is governed by the \
contract below. Read the contract carefully: its vocabularies, classification rules and \
date rules are binding.

===== CONTRACT (README.md) =====
{readme}
===== END CONTRACT =====

Classify ONE document from its extracted TEXT. Never use the original filename. Answer with a \
single JSON object and nothing else:
{{
  "doc_date": "YYYY-MM-DD or null: clinical (exam/report/sample) date, never birth/print/scan",
  "date_evidence": "the exact text snippet the date was read from",
  "doc_type": "one of: {doc_types}",
  "institution": "Institution token from the contract, or a new CamelCase token, or Unknown",
  "doctor": "signing doctor as printed, e.g. 'MUDr. Jana Kovacova', or null",
  "hospital": "HospitalName token from the contract vocabulary, or Unknown",
  "hospital_unit": "department as printed (free text) or null",
  "subject": "optional CamelCase slug <= 30 chars, only if needed to disambiguate, else null",
  "diagnoses": ["ICD-10 codes printed on the document"],
  "language": "sk | cs | en | ...",
  "multiple_documents": "true if the text holds several documents (new letterhead + date)",
  "confidence": 0.0,
  "summary": "one sentence in the document's language"
}}
"""


@dataclass
class Classification:
    fields: NameFields | None
    doc_type: str
    raw: dict
    review_needed: bool
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


_DATE_PATTERNS = (
    "{d}.{m}.{y}",
    "{di}.{mi}.{y}",
    "{d}. {m}. {y}",
    "{di}. {mi}. {y}",
    "{y}-{m}-{d}",
    "{d}/{m}/{y}",
    "{d}.{m}.{yy}",
)


def date_in_text(iso: str, text: str) -> bool:
    """True when the ISO date is literally printed in ``text`` in a common SK form."""
    y, m, d = iso.split("-")
    variants = {
        p.format(d=d, m=m, y=y, di=str(int(d)), mi=str(int(m)), yy=y[2:]) for p in _DATE_PATTERNS
    }
    compact = re.sub(r"[ \t]+", " ", text)
    return any(re.search(r"(?<!\d)" + re.escape(v) + r"(?!\d)", compact) for v in variants)


def _parse_iso(value) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def propose(contract: Contract, text: str) -> dict:
    """Ask the local model for the fields (blocking)."""
    system = SYSTEM_TEMPLATE.format(
        readme=contract.readme_text, doc_types=", ".join(sorted(contract.doc_types))
    )
    answer = llm.chat(system, f"TEXT:\n{text[:MAX_TEXT_CHARS]}", max_tokens=1200)
    return llm.parse_json_object(answer)


def validate(
    contract: Contract, raw: dict, text: str, *, today: date | None = None
) -> Classification:
    today = today or date.today()
    reasons: list[str] = []
    flags: list[str] = []

    def snap(token: str) -> str:
        """Snap an OCR-misspelled token to the contract vocabulary (Parsok → Porsok)."""
        if token == UNKNOWN or token in contract.known_tokens:
            return token
        match = difflib.get_close_matches(token, contract.known_tokens, n=1, cutoff=SNAP_CUTOFF)
        if match:
            flags.append(f"snapped {token!r} → {match[0]!r} (README vocabulary)")
            return match[0]
        return token

    doc_type = raw.get("doc_type") if isinstance(raw.get("doc_type"), str) else None
    if doc_type not in contract.doc_types:
        reasons.append(f"doc_type {doc_type!r} not in contract")
        doc_type = "Other"

    iso = _parse_iso(raw.get("doc_date"))
    if iso is None:
        reasons.append("no valid clinical date")
    else:
        compact = iso.replace("-", "")
        if compact in contract.birth_dates:
            reasons.append(f"date {iso} is the patient's birth date")
            iso = None
        elif date.fromisoformat(iso) > today:
            reasons.append(f"date {iso} is in the future")
            iso = None
        elif date.fromisoformat(iso).year < 1900:
            reasons.append(f"date {iso} is implausible")
            iso = None
        elif not date_in_text(iso, text):
            reasons.append(f"date {iso} does not appear in the document text")
            iso = None

    institution = snap(camel(raw.get("institution")) if raw.get("institution") else UNKNOWN)
    hospital = snap(camel(raw.get("hospital")) if raw.get("hospital") else UNKNOWN)
    doctor = snap(surname_token(raw.get("doctor")))
    if institution == UNKNOWN:
        reasons.append("institution unknown")
    for label, token in (("institution", institution), ("hospital", hospital), ("doctor", doctor)):
        if token != UNKNOWN and token not in contract.known_tokens:
            flags.append(f"first-time {label} value {token!r}")

    multiple = raw.get("multiple_documents")
    if multiple is True or (isinstance(multiple, str) and multiple.lower() == "true"):
        reasons.append("looks like several documents in one file (split needed)")

    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < MIN_CONFIDENCE:
        reasons.append(f"model confidence {confidence:.2f} < {MIN_CONFIDENCE}")

    subject = raw.get("subject") if isinstance(raw.get("subject"), str) else None
    fields = None
    if iso is not None:
        fields = NameFields(
            date=iso.replace("-", ""),
            patient=contract.patient_token,
            institution=institution,
            doc_type=doc_type,
            doctor=doctor,
            hospital=hospital,
            subject=subject or None,
        )
    return Classification(
        fields=fields,
        doc_type=doc_type,
        raw=raw,
        review_needed=bool(reasons),
        reasons=reasons,
        flags=flags,
    )
