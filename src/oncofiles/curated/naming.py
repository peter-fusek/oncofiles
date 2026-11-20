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


# Academic and professional titles (#900), folded lower case without dots.
# Slovak / Czech degrees and the usual international ones; a title is never a
# surname, so it is dropped wherever it stands (before or after the name).
TITLES = frozenset(
    {
        "mudr", "mddr", "mvdr", "pharmdr", "rndr", "phdr", "judr", "paeddr", "thdr",
        "thlic", "rsdr", "artd", "mgr", "mga", "bc", "bca", "ing", "arch", "phd", "csc",
        "drsc", "dsc", "dr", "mph", "mba", "mha", "msc", "bsc", "llm", "prim", "doc",
        "prof", "mudrmgr",
    }
)  # fmt: skip
# Titles that may be glued to the following name without a dot ("MUDrJana"):
# only their mixed-case spelling is recognised, so an all-caps surname such as
# "DRAHOS" is never cut. Longest first.
_GLUED_PREFIX = sorted(
    ("MUDr", "MDDr", "MVDr", "PharmDr", "RNDr", "PhDr", "JUDr", "PaedDr", "ThDr", "Mgr",
     "Ing", "Bc", "Doc", "Prof", "Prim", "Dr", "PhD"),
    key=len,
    reverse=True,
)  # fmt: skip
# Titles glued after a name ("KovacovaPhD"), in their usual spelling.
_GLUED_SUFFIX_RE = re.compile(r"(?<=[^\W\d_])(?:PhD|CSc|DrSc|MPH|MBA|MHA|MSc)$")
_PHD_SPACED_RE = re.compile(r"\bPh\.\s*D\.?", re.I)


def _strip_glued(word: str) -> str:
    """``MUDrMgrJana`` → ``Jana``; ``KovacovaPhD`` → ``Kovacova``; others unchanged."""
    changed = True
    while changed and word:
        changed = False
        for t in _GLUED_PREFIX:
            if len(word) > len(t) and word.startswith(t) and word[len(t)].isupper():
                word, changed = word[len(t) :], True
                break
        m = _GLUED_SUFFIX_RE.search(word)
        if m and m.start() > 0:
            word, changed = word[: m.start()], True
    return word


def _is_title(word: str) -> bool:
    return ascii_fold(word).lower().replace(".", "") in TITLES


def _name_words(segment: str) -> list[str]:
    """The words of ``segment`` that are not titles, initials or numbers.

    Dots and whitespace separate words (``MUDr.Mgr.Jana`` → three words); a
    hyphen does not, so a double-barrelled surname stays one word."""
    out = []
    for raw in re.split(r"[\s.]+", segment):
        word = re.sub(r"[^\w-]", "", raw).strip("-_")
        if not word or any(c.isdigit() for c in word) or _is_title(word):
            continue
        stripped = _strip_glued(word)
        # A title glued on means the whole name may be glued ("MUDrJanaNova"):
        # then every lower→upper case change is a word break too.
        parts = _split_camel(stripped) if stripped != word else [stripped]
        for part in parts:
            if len(part) >= 2 and not _is_title(part):  # a lone initial is no surname
                out.append(part)
    return out


def _split_camel(word: str) -> list[str]:
    parts, cur = [], ""
    for i, c in enumerate(word):
        if cur and c.isupper() and word[i - 1].islower():
            parts.append(cur)
            cur = ""
        cur += c
    return [*parts, cur] if cur else parts


def doctor_surname(value: str | None) -> str:
    """The surname as printed (diacritics kept) without any title (#900); '' if none.

    One function for the filename doctor field and the README vocabulary
    (#854): ``MUDr. Mgr. Jana Kováčová, PhD.`` → ``Kováčová``,
    ``MUDr.Mgr.Jana Nová-Kováčová`` → ``Nová-Kováčová``,
    ``Kováčová, Jana (MUDr. Mgr., PhD.)`` → ``Kováčová``. Titles are matched
    without regard to case or dots, spaced or glued, before or after the name.
    The surname is the last remaining word, except in the ``Surname, Given``
    form (a single word before the first comma, more name words after it).
    """
    if not value or not isinstance(value, str) or not value.strip():
        return ""
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", value)
    text = _PHD_SPACED_RE.sub(" PhD ", text)
    segments = [w for w in (_name_words(s) for s in text.split(",")) if w]
    if not segments:
        return ""
    first = segments[0]
    if len(first) == 1 and len(segments) > 1:
        return first[0]  # "Surname, Given"
    return first[-1]


def surname_text(value: str | None) -> str:
    """The surname as printed (diacritics kept, no titles); '' when there is none.

    Accepts ``Surname, Given (titles)`` (the sidecar form) or ``Titles Given Surname``;
    see ``doctor_surname`` (#900).
    """
    return doctor_surname(value)


def surname_token(value: str | None) -> str:
    """Doctor field: surname only, no titles. ``MUDr. Jana Kováčová`` → ``Kovacova``.

    An all-caps printed surname gets the README spelling (``KOVÁČOVÁ`` → ``Kovacova``).
    """
    surname = doctor_surname(value)
    if surname.isupper():
        surname = "-".join(p.capitalize() for p in surname.split("-"))
    return camel(surname)


def has_title(token: str | None) -> bool:
    """A filename / vocabulary token that still carries a title (#900): never file it."""
    if not token:
        return False
    return _is_title(token) or _strip_glued(token) != token


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


# An ID-like digit run (accession / record / barcode numbers) in a subject (#919).
_ID_RUN_RE = re.compile(r"\d{6,}")


def subject_token(value: str | None) -> str | None:
    """The optional Subject field (#919), or None when nothing usable is left.

    One rule shared by the builder and the checker (``_NAME_RE`` wants a
    subject that starts with a letter, and reads two bare digits as ``nn``):

    * free text → one CamelCase ASCII token (``camel``);
    * an ID-like digit run (six or more digits) is dropped wherever it stands;
    * a leading digit run is dropped (``2. cyklus`` → ``Cyklus``), so an
      all-digit subject (``12``, ``2027``) leaves nothing and is omitted;
    * at most ``SUBJECT_MAX`` characters; ``Unknown`` or empty → None.
    """
    if not value or not isinstance(value, str):
        return None
    token = camel(value)
    if token == UNKNOWN:
        return None
    token = _ID_RUN_RE.sub("", token)
    token = re.sub(r"^\d+", "", token)[:SUBJECT_MAX]
    if not token or token == UNKNOWN:
        return None
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
    subject = subject_token(fields.subject)
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


GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
# Extension the runner builds a Google-native name with and then strips (#919):
# a Google Doc's title has no extension through the Drive API.
NATIVE_EXT = "gdoc"


def is_google_native(mime: str | None) -> bool:
    return bool(mime) and mime.startswith(GOOGLE_NATIVE_PREFIX)


def is_standard(name: str, *, patient_token: str | None = None, mime: str | None = None) -> bool:
    """Does ``name`` follow the README standard?

    ``mime`` (#919): a Google-native file (``application/vnd.google-apps.*``)
    has no extension through the Drive API, so its title is standard when the
    fields are valid without one. Without ``mime`` the extension is required,
    as before.
    """
    fields = parse_filename(name)
    if fields is None and is_google_native(mime) and name and not name.endswith("."):
        fields = parse_filename(f"{name}.{NATIVE_EXT}")
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
