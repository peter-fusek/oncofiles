"""The README filename standard for curated folders (#562).

    YYYYMMDD-PatientName-Institution-DocumentType-DoctorName-HospitalName[-Subject][-nn].ext

Six mandatory hyphen-separated fields, every field CamelCase ASCII with no
diacritics, spaces or underscores, so ``basename.split('-')`` parses the whole
name. Missing values are the literal ``Unknown``; the position says which field
is missing. Target length ≤ 120 characters: truncate field 6 first, then 7.

Pure functions only — no I/O — so the invariants are cheap to lock in tests.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

UNKNOWN = "Unknown"
MAX_LEN = 120
SUBJECT_MAX = 30

_DATE_RE = re.compile(r"^\d{8}$")
_FIELD_RE = re.compile(r"^[A-Za-z0-9]+$")
_NN_RE = re.compile(r"^\d{2}$")
# date-Patient-Institution-Type-Doctor-Hospital[-Subject][-nn].ext
_NAME_RE = re.compile(
    r"^(?P<date>\d{8})-(?P<patient>[A-Za-z0-9]+)-(?P<institution>[A-Za-z0-9]+)"
    r"-(?P<doc_type>[A-Za-z0-9]+)-(?P<doctor>[A-Za-z0-9]+)-(?P<hospital>[A-Za-z0-9]+)"
    r"(?:-(?P<subject>[A-Za-z][A-Za-z0-9]*))?(?:-(?P<nn>\d{2}))?\.(?P<ext>[a-z0-9]+)$"
)


def ascii_fold(value: str) -> str:
    """Strip diacritics: ``Nováková`` → ``Novakova``, ``Pôrsôk`` → ``Porsok``."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def camel(value: str | None) -> str:
    """Turn free text into one CamelCase ASCII token; empty → ``Unknown``.

    Existing CamelCase is preserved (``SampleHospital`` stays), each word's first
    letter is upper-cased, and every non-alphanumeric character is a word break.
    ``Klinika klinickej onkológie`` → ``KlinikaKlinickejOnkologie``.
    """
    words = re.split(r"[^A-Za-z0-9]+", ascii_fold(value or ""))
    token = "".join(w[:1].upper() + w[1:] for w in words if w)
    return token or UNKNOWN


def surname_token(value: str | None) -> str:
    """Doctor field: surname only, no titles. ``MUDr. Jana Kováčová`` → ``Kovacova``.

    Accepts ``Surname, Given (titles)`` (the sidecar form) or ``Titles Given Surname``.
    """
    if not value or not value.strip():
        return UNKNOWN
    text = re.sub(r"\(.*?\)", " ", value)
    if "," in text:
        surname = text.split(",", 1)[0]
    else:
        parts = [
            p
            for p in re.split(r"\s+", text.strip())
            if p and not p.endswith(".") and p.lower() not in _TITLES
        ]
        surname = parts[-1] if parts else ""
    return camel(surname)


_TITLES = {"mudr", "mgr", "phd", "ing", "doc", "prof", "csc", "mph", "mba", "bc", "rndr"}


@dataclass(frozen=True)
class NameFields:
    date: str  # YYYYMMDD or "Unknown"
    patient: str
    institution: str
    doc_type: str
    doctor: str
    hospital: str
    subject: str | None = None
    nn: int | None = None


def _clean(value: str | None) -> str:
    token = camel(value) if value and not _FIELD_RE.match(value) else (value or UNKNOWN)
    return token


def build_filename(fields: NameFields, ext: str, *, max_len: int = MAX_LEN) -> str:
    """Build a standard filename. Raises ValueError on an invalid date.

    The date must be ``YYYYMMDD``. ``Unknown`` is not accepted for the date: a
    document with no reliable clinical date stays in the inbox for review
    (README §5), it does not get a placeholder date.
    """
    if not _DATE_RE.match(fields.date or ""):
        raise ValueError(f"invalid document date {fields.date!r} — must be YYYYMMDD")
    ext = (ext or "").lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]+", ext):
        raise ValueError(f"invalid extension {ext!r}")
    head = [
        fields.date,
        _clean(fields.patient),
        _clean(fields.institution),
        _clean(fields.doc_type),
        _clean(fields.doctor),
    ]
    hospital = _clean(fields.hospital)
    subject = camel(fields.subject)[:SUBJECT_MAX] if fields.subject else None
    if subject == UNKNOWN:
        subject = None
    suffix = f"-{fields.nn:02d}" if fields.nn else ""
    tail = f"{suffix}.{ext}"

    def _assemble(h: str, s: str | None) -> str:
        parts = [*head, h] + ([s] if s else [])
        return "-".join(parts) + tail

    name = _assemble(hospital, subject)
    if len(name) > max_len:  # truncate field 6 first …
        over = len(name) - max_len
        hospital = hospital[: max(1, len(hospital) - over)]
        name = _assemble(hospital, subject)
    if len(name) > max_len and subject:  # … then field 7
        over = len(name) - max_len
        subject = subject[: len(subject) - over] or None
        name = _assemble(hospital, subject)
    return name


def parse_filename(name: str) -> NameFields | None:
    """Parse a standard filename; None if it does not follow the standard."""
    m = _NAME_RE.match(name or "")
    if not m:
        return None
    nn = m.group("nn")
    return NameFields(
        date=m.group("date"),
        patient=m.group("patient"),
        institution=m.group("institution"),
        doc_type=m.group("doc_type"),
        doctor=m.group("doctor"),
        hospital=m.group("hospital"),
        subject=m.group("subject"),
        nn=int(nn) if nn else None,
    )


def is_standard(name: str, *, patient_token: str | None = None) -> bool:
    fields = parse_filename(name)
    if fields is None:
        return False
    return patient_token is None or fields.patient == patient_token


def with_collision_suffix(name: str, taken: set[str]) -> str:
    """Return ``name`` or the first ``-02``, ``-03``… variant not in ``taken``."""
    if name not in taken:
        return name
    fields = parse_filename(name)
    if fields is None:
        raise ValueError(f"not a standard filename: {name!r}")
    ext = name.rsplit(".", 1)[1]
    for n in range(2, 100):
        candidate = build_filename(
            NameFields(**{**fields.__dict__, "nn": n}),
            ext,
        )
        if candidate not in taken:
            return candidate
    raise ValueError(f"no free collision suffix for {name!r}")
