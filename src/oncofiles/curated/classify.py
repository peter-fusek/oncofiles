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
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from oncofiles import config
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
    user = f"TEXT:\n{text[:MAX_TEXT_CHARS]}"
    try:
        answer = llm.chat(system, user)
    except llm.LocalLLMError as exc:
        if "truncated" not in str(exc):
            raise
        # 2026-11-08: 3 of 11 documents ran out of reasoning budget. One retry
        # with twice the budget (and time) before the document goes to review,
        # but only if the context window actually leaves more room than before.
        est = llm.estimate_prompt_tokens(system, user)
        if llm.clamp_budget(2 * llm.REASONING_BUDGET, est) <= llm.clamp_budget(
            llm.REASONING_BUDGET, est
        ):
            raise
        answer = llm.chat(
            system,
            user,
            max_tokens=2 * llm.REASONING_BUDGET,
            timeout=2 * config.LOCAL_LLM_TIMEOUT_S,
        )
    return llm.parse_json_object(answer)


CAREGIVER_DOC_TYPES = {"Reference", "Advocate"}

# Words that can stand next to a surname without being a given name.
_NOT_GIVEN_NAMES = {
    "pacient",
    "pacientka",
    "meno",
    "priezvisko",
    "rodne",
    "cislo",
    "adresa",
    "poistenec",
    "poistenka",
    "datum",
    "pani",
    "pan",
    "mudr",
    "mgr",
    "ing",
    "phd",
    "bc",
    "rodina",
    "rod",
    "manzel",
    "manzelka",
    "syn",
    "dcera",
    "otec",
    "matka",
}
_NAME = r"[^\W\d_][^\W\d_]+"


def _fold(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    ).lower()


def other_person_named(patient_display: str, text: str) -> str | None:
    """Name of a different person sharing the patient's surname, if the text names one
    and never names the patient (#578: a son's dermatology report and a husband's
    membership invoice were filed under the patient). Deterministic, no model.
    """
    parts = patient_display.split()
    if len(parts) < 2 or not text:
        return None
    given = _fold(parts[0])
    folded = _fold(text)
    stem = _fold(parts[-1])
    stem = stem[:-3] if stem.endswith("ova") else stem
    if len(stem) < 3:
        return None
    g, st = re.escape(given), re.escape(stem)
    # The patient counts as named only with given name AND surname together
    # (a doctor may share the given name); relatives may then appear too.
    if re.search(rf"\b{g}[,\s]+{st}\w*\b|\b{st}\w*[,\s]+{g}\b", folded):
        return None
    pattern = re.compile(
        rf"\b({_NAME})[,\s]+({re.escape(stem)}\w*)\b|\b({re.escape(stem)}\w*)[,\s]+({_NAME})\b"
    )
    for m in pattern.finditer(folded):
        other, sur = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if other not in _NOT_GIVEN_NAMES and not other.startswith(stem):
            start = m.start()
            return text[start : m.end()].strip()
    return None


def validate(
    contract: Contract,
    raw: dict,
    text: str,
    *,
    today: date | None = None,
    max_age_days: int | None = None,
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
        elif max_age_days is not None and (today - date.fromisoformat(iso)).days > max_age_days:
            # Oncology contract rule: an old date on a *new* scan is suspect
            # (the classic failure is a historical date read off the page).
            reasons.append(f"date {iso} is more than {max_age_days // 365} years old — suspect")
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
    # Reference/advocate material is written or collected by the caregiver,
    # whose name is expected there; everything else must be the patient's.
    other = (
        None
        if doc_type in CAREGIVER_DOC_TYPES
        else other_person_named(contract.patient_display, text)
    )
    if other:
        reasons.append(f"document names another person ({other}), not {contract.patient_display}")

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
