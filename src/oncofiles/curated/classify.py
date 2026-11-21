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
* an OCR-misspelled vocabulary token is snapped back to the README spelling,
  but only when it is clearly the *same* entity (#754): edit distance <= 2 and the
  same leading CamelCase word; for Institution / Doctor also the README token is
  printed in the text, or the model's token is not;
* a new Institution / Doctor (not in README §3) → ``review_needed``; a new
  HospitalName / Subject is accepted and *flagged* for the run report.
* with ``auto_vocab`` (#854, ``CURATED_AUTO_VOCAB``, on in the runner) a new
  Institution / Doctor is instead proposed for the README vocabulary when its
  spelling is printed in the text (``Classification.additions``; the runner
  writes it) and an unknown one is only flagged. Real ambiguity — another
  person, no or a birth date, unknown type, split, low confidence, a near
  neighbour of a README token that is a different entity — still → review.
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
from oncofiles.curated.naming import (
    UNKNOWN,
    NameFields,
    camel,
    has_title,
    subject_token,
    surname_text,
    surname_token,
)
from oncofiles.curated.vocabulary import VocabAddition

MIN_CONFIDENCE = 0.60
SNAP_CUTOFF = 0.8  # OCR typo tolerance when matching README vocabulary tokens
SNAP_MAX_EDITS = 2  # #754: a bigger "correction" is a different entity, not an OCR typo
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
  "institution_printed": "the issuing institution's name copied verbatim from the TEXT, or null",
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
    # #854: vocabulary the runner adds to README §3 before filing (auto_vocab only)
    additions: list[VocabAddition] = field(default_factory=list)


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
        if "truncated" not in str(exc) or "overflow" in str(exc):
            raise  # an overflowing prompt does not fit any better with a bigger budget
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


_CAMEL_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (case-insensitive) between two short tokens."""
    a, b = a.lower(), b.lower()
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _leading_word(token: str) -> str:
    words = _CAMEL_WORD_RE.findall(token)
    return words[0].lower() if words else token.lower()


def _same_entity(token: str, known: str) -> bool:
    """#754: an OCR typo of ``known``, not a different entity with a similar name.

    ``XyzAmbulancia`` vs ``AbcAmbulancia`` share a long suffix (difflib ratio is
    high) but differ in the leading word, so they are two departments.
    """
    if _edit_distance(token, known) > SNAP_MAX_EDITS:
        return False
    words_t, words_k = _CAMEL_WORD_RE.findall(token), _CAMEL_WORD_RE.findall(known)
    if len(words_t) <= 1 and len(words_k) <= 1:
        return True  # single-word tokens (surnames, acronyms): the distance decides
    return _leading_word(token) == _leading_word(known)


# ── #854: automatic vocabulary ──────────────────────────────────────

MAX_NEW_TOKEN_LEN = 40  # a longer CamelCase name is no usable filename field
BIRTH_WINDOW = 60  # characters around a printed name that must not speak of birth
_BIRTH_CONTEXT_RE = re.compile(
    r"narod|\bdob\b|date of birth|\bborn\b|geburt|rodne\s*cislo|\br\.\s*c\.(?!\w)"
)
_NEW_TOKEN_RE = re.compile(r"[A-Z][A-Za-z0-9]+")


def _norm(value: str) -> str:
    """Folded (no diacritics, lower case) with whitespace runs collapsed."""
    return re.sub(r"\s+", " ", _fold(value or "")).strip()


def _birth_variants(birth_dates) -> set[str]:
    out: set[str] = set()
    for b in birth_dates:
        y, m, d = b[:4], b[4:6], b[6:]
        out |= {
            p.format(d=d, m=m, y=y, di=str(int(d)), mi=str(int(m)), yy=y[2:])
            for p in _DATE_PATTERNS
        }
    return out


def printed_in_text(value: str, text: str, birth_dates=frozenset()) -> bool:
    """``value`` is printed in ``text``: the same spelling ignoring diacritics, case
    and whitespace runs, on word boundaries, at least once outside a birth context
    (a birth place next to "narodená" or the DOB is not an institution)."""
    needle = _norm(value)
    if len(re.sub(r"[^a-z0-9]", "", needle)) < 2:
        return False
    hay = _norm(text)
    births = _birth_variants(birth_dates)
    for m in re.finditer(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", hay):
        window = hay[max(0, m.start() - BIRTH_WINDOW) : m.end() + BIRTH_WINDOW]
        if _BIRTH_CONTEXT_RE.search(window) or any(b in window for b in births):
            continue
        return True
    return False


def names_patient(value: str, contract: Contract) -> bool:
    """A word of ``value`` is the patient's given name, filename token, or built on
    the surname (``Novak`` for ``Jana Novakova``) — never a vocabulary entry."""
    parts = [_fold(p) for p in contract.patient_display.split()]
    if not parts:
        return False
    given, surname = parts[0], parts[-1]
    stem = surname[:-3] if surname.endswith("ova") else surname
    token = _fold(contract.patient_token)
    words = re.findall(r"[a-z0-9]+", _fold(value))
    if token and token == "".join(words):
        return True
    return any(w in (given, token) or (len(stem) >= 3 and w.startswith(stem)) for w in words)


def _in_text(token: str, folded_text: str) -> bool:
    """``token`` printed in the text, ignoring diacritics, case, spaces and punctuation."""
    return bool(token) and re.sub(r"[^a-z0-9]", "", token.lower()) in folded_text


def validate(
    contract: Contract,
    raw: dict,
    text: str,
    *,
    today: date | None = None,
    max_age_days: int | None = None,
    auto_vocab: bool = False,
) -> Classification:
    """Apply the hard rules. ``auto_vocab`` (#854): see the module docstring."""
    today = today or date.today()
    reasons: list[str] = []
    flags: list[str] = []
    additions: list[VocabAddition] = []

    folded_text = re.sub(r"[^a-z0-9]", "", _fold(text or ""))

    def snap(token: str, label: str) -> str:
        """Snap an OCR-misspelled token to the contract vocabulary (Parsok → Porsok).

        Only a genuine misspelling of the same entity is snapped (#754); a
        similar-looking but different README token is never substituted.
        """
        if token == UNKNOWN or token in contract.known_tokens:
            return token
        same = [k for k in contract.known_tokens if k.lower() == token.lower()]
        if len(same) == 1:  # #900: the README spelling of the same (folded) token
            return same[0]
        for match in difflib.get_close_matches(
            token, contract.known_tokens, n=3, cutoff=SNAP_CUTOFF
        ):
            if not _same_entity(token, match):
                continue
            if label in ("institution", "doctor") and not (
                _in_text(match, folded_text) or not _in_text(token, folded_text)
            ):
                continue  # the model's token is printed and the README one is not
            flags.append(f"snapped {token!r} → {match!r} (README vocabulary)")
            return match
        return token

    def resolve_new(label: str, token: str, printed: list, sources: list, to_token=camel) -> str:
        """#854: a token not in README §3 → README token, new printed token, or Unknown.

        Appends to ``additions`` only for a spelling printed in the text; a near
        neighbour of a README token that #754 says is another entity → review.
        """
        if token == UNKNOWN or token in contract.known_tokens:
            return token
        if any(isinstance(v, str) and "?" in v for v in sources):
            flags.append(f"{label} reading uncertain ('?') — filed as Unknown")
            return UNKNOWN
        lower = {k.lower(): k for k in contract.known_tokens}
        for value in (v.strip() for v in printed if isinstance(v, str) and v.strip()):
            if not printed_in_text(value, text or "", contract.birth_dates):
                continue
            if names_patient(value, contract):  # first: never matched, never added
                flags.append(f"{label} value names the patient — filed as Unknown")
                return UNKNOWN
            cand = to_token(value)
            if cand.lower() in lower:
                return lower[cand.lower()]
            snapped = snap(cand, label)
            if snapped in contract.known_tokens:
                return snapped
            near = sorted(
                k for k in contract.known_tokens if _edit_distance(cand, k) <= SNAP_MAX_EDITS
            )
            if near:
                reasons.append(
                    f"new {label} {cand!r} is close to README token {near[0]!r} — same entity?"
                )
                return cand
            if (
                cand == UNKNOWN
                or not _NEW_TOKEN_RE.fullmatch(cand)
                or len(cand) > MAX_NEW_TOKEN_LEN
            ):
                flags.append(f"{label} value is not a usable token — filed as Unknown")
                return UNKNOWN
            additions.append(VocabAddition(label.capitalize(), cand, value))
            return cand
        flags.append(f"new {label} {token!r} is not printed in the text — filed as Unknown")
        return UNKNOWN

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

    institution = snap(
        camel(raw.get("institution")) if raw.get("institution") else UNKNOWN, "institution"
    )
    hospital = snap(camel(raw.get("hospital")) if raw.get("hospital") else UNKNOWN, "hospital")
    doctor = snap(surname_token(raw.get("doctor")), "doctor")
    # #919: the builder's own subject rule (no ID-like digit runs, never a
    # leading digit), so the flagged token is the one the filename will carry
    subject = subject_token(raw.get("subject"))
    subject = snap(subject, "subject") if subject else None
    if auto_vocab:
        # #854: a new institution / doctor printed in the text joins README §3;
        # an unknown one alone is no reason to keep the document in the inbox.
        institution = resolve_new(
            "institution",
            institution,
            [raw.get("institution"), raw.get("institution_printed")],
            [raw.get("institution"), raw.get("institution_printed")],
        )
        raw_doctor = raw.get("doctor") if isinstance(raw.get("doctor"), str) else None
        doctor = resolve_new(
            "doctor", doctor, [surname_text(raw_doctor)], [raw_doctor], to_token=surname_token
        )
        if institution == UNKNOWN:
            flags.append("institution unknown — filed as Unknown")
    else:
        if institution == UNKNOWN:
            reasons.append("institution unknown")
        # #754: a new institution or doctor is an entity the README does not know
        # yet — a human confirms it (and adds it to §3) before anything is filed.
        for label, token in (("institution", institution), ("doctor", doctor)):
            if token != UNKNOWN and token not in contract.known_tokens:
                reasons.append(f"new {label} {token!r} not in README vocabulary")
    if has_title(doctor):  # #900: a title is never a doctor token, new or filed
        flags.append("doctor token still carries a title — filed as Unknown")
        additions[:] = [a for a in additions if a.label != "Doctor"]
        doctor = UNKNOWN
    # A new department / subject is plausible on its own: file it, but say so.
    for label, token in (("HospitalName", hospital), ("Subject", subject)):
        if token and token != UNKNOWN and token not in contract.known_tokens:
            flags.append(f"new {label} value {token!r} (not in README)")

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

    if reasons:
        additions.clear()  # a document that waits for a human adds nothing to the README
    for a in additions:
        flags.append(f"new {a.label} {a.token!r} added to README vocabulary (printed in the text)")

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
        additions=additions,
    )
