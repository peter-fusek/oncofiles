"""Filed documents whose name fails the README standard (#919, item 2).

The curated runner files inbox documents under a standard name, but documents
filed before the runner existed (Cowork / manual era) keep whatever name they
had, and nothing ever revisits them. This module plans the standard name of
such a filed document from what the folder already knows — its sidecar front
matter, its folder's DocumentType and, for a near-standard name, the name's own
valid fields — with the SAME builder as inbox filing (``naming.build_filename``
+ ``naming.is_standard``). The runner performs the rename (``runner._normalise_filed``).

Rules:

* the date must be printed in the document's text and pass the same checks as
  ``classify.validate`` (not a README birth date, not in the future, not before
  1900). No such date → no rename, review code ``name_not_standard``; a date is
  never guessed;
* Institution / Doctor / HospitalName come from the front matter when they
  resolve to a README §3 token (case-insensitive), else from the field of a
  near-standard name, else ``Unknown``; a new token is never invented here
  (the README vocabulary only grows through inbox filing, #854);
* DocumentType is the folder's: a rename never moves a document;
* a month-only note (``YYYY-MM-…``) is left alone with code
  ``month_only_date`` (maintainer decision pending, #919 item 4);
* a Google-native file keeps no extension (#922 rule).

Pure functions, no I/O. Env (runtime, read at call time):
``CURATED_NORMALISE_FILED`` (default true; a dry run always shows the plan)
and ``CURATED_NORMALISE_MAX`` (renames per real run, default 10).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date

from oncofiles.curated.classify import date_in_text
from oncofiles.curated.contract import Contract
from oncofiles.curated.naming import (
    NATIVE_EXT,
    UNKNOWN,
    NameFields,
    build_filename,
    camel,
    has_title,
    is_google_native,
    is_standard,
    subject_token,
    surname_token,
    with_collision_suffix,
)

DEFAULT_MAX = 10

# Codes (report only; never document content).
CODE_NAME_NOT_STANDARD = "name_not_standard"  # review: no rename possible
CODE_MONTH_ONLY = "month_only_date"  # skipped: maintainer decision pending
CODE_NO_SIDECAR = "no_sidecar"  # skipped: the backfill adds one first
# ``why`` of a name_not_standard entry
WHY_NO_PRINTED_DATE = "no_printed_date"
WHY_BUILT_NOT_STANDARD = "built_name_not_standard"
WHY_NO_FREE_NAME = "no_free_name"

# ``2027-03-…`` without a day: a month-precision note. A dashed full date
# (``2027-03-15_…``) is not month-only.
_MONTH_ONLY_RE = re.compile(r"^\d{4}-\d{2}(?!\d)(?!-\d{2}(?!\d))")
_COMPACT_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?!\d)")
_DASHED_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?!\d)")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9]+$")


def enabled() -> bool:
    """``CURATED_NORMALISE_FILED`` (default true): may a real run rename?"""
    value = os.environ.get("CURATED_NORMALISE_FILED", "true").strip().lower()
    return value in ("1", "true", "yes", "on")


def max_per_run() -> int:
    """``CURATED_NORMALISE_MAX`` (default 10, at least 0): renames per real run."""
    try:
        return max(0, int(os.environ.get("CURATED_NORMALISE_MAX", str(DEFAULT_MAX))))
    except ValueError:
        return DEFAULT_MAX


def name_shape(name: str) -> str:
    """What a server log may say about a filename: its shape only.

    Every letter run becomes ``A`` and every digit ``9``; separators stay, so
    ``20270312-JanaNovakova-Lab.pdf`` → ``99999999-A-A.A``.
    """
    return re.sub(r"\d", "9", re.sub(r"[^\W\d_]+", "A", name or ""))


def is_month_only(name: str) -> bool:
    return bool(_MONTH_ONLY_RE.match(name or ""))


def _stem(name: str, native: bool) -> str:
    if native or "." not in name:
        return name
    return name.rsplit(".", 1)[0]


def near_standard_parts(name: str, contract: Contract, *, native: bool) -> list[str] | None:
    """The hyphen fields of a name that is standard except for its tail.

    ``date-Patient-Institution-Type-Doctor-Hospital[-…]`` with an 8-digit
    date, this patient's token and alphanumeric fields (e.g. a subject that
    starts with a digit, #919). None for anything else (legacy underscore
    names, other patients' names): their fields are never reused.
    """
    parts = _stem(name, native).split("-")
    if len(parts) < 6 or not re.fullmatch(r"\d{8}", parts[0]):
        return None
    if parts[1] != contract.patient_token or not all(_TOKEN_RE.match(p) for p in parts):
        return None
    return parts


def validated_date(candidates, text: str, contract: Contract, today: date) -> str | None:
    """The first candidate date (ISO or ``YYYYMMDD``) printed in ``text`` that
    passes ``classify.validate``'s checks, as ``YYYYMMDD``; None when none does."""
    for value in candidates:
        if not isinstance(value, str):
            continue
        value = value.strip()
        m = _DASHED_DATE_RE.match(value) or _COMPACT_DATE_RE.match(value)
        if not m:
            continue
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        compact = d.strftime("%Y%m%d")
        if compact in contract.birth_dates or d > today or d.year < 1900:
            continue
        if date_in_text(d.isoformat(), text):
            return compact
    return None


def _known(contract: Contract) -> dict[str, str]:
    banned = {contract.patient_token.lower(), UNKNOWN.lower()}
    return {t.lower(): t for t in contract.known_tokens if t.lower() not in banned}


def _resolve(values, known: dict[str, str], fallback: str | None) -> str:
    """The first value that is a README token (case-insensitive), else the
    near-standard name's field, else ``Unknown``."""
    for v in values:
        if isinstance(v, str) and v.strip():
            token = known.get(camel(v).lower())
            if token and not has_title(token):
                return token
    if fallback and fallback != UNKNOWN and not has_title(fallback):
        return fallback
    return UNKNOWN


@dataclass
class Plan:
    """The outcome for one filed document: a target name or a code."""

    target: str | None = None
    code: str | None = None
    why: str | None = None
    date_source: str | None = None  # "front_matter" | "filename"


def plan_name(
    *,
    name: str,
    mime: str,
    doc_type: str,
    front: dict,
    text: str,
    contract: Contract,
    today: date,
    taken: set[str],
) -> Plan:
    """The standard name for a filed document, or why there is none.

    ``taken``: names already in the folder, the document's own pair excluded.
    """
    if is_month_only(name):
        return Plan(code=CODE_MONTH_ONLY)
    native = is_google_native(mime)
    parts = near_standard_parts(name, contract, native=native)
    name_date = _DASHED_DATE_RE.match(name) or _COMPACT_DATE_RE.match(name)
    front_date = front.get("doc_date")
    day = validated_date([front_date], text, contract, today)
    source = "front_matter"
    if day is None and name_date:
        day = validated_date([name_date.group(0)], text, contract, today)
        source = "filename"
    if day is None:
        return Plan(code=CODE_NAME_NOT_STANDARD, why=WHY_NO_PRINTED_DATE)

    known = _known(contract)
    institution = _resolve([front.get("institution")], known, parts[2] if parts else None)
    doctor_front = surname_token(front.get("doctor")) if front.get("doctor") else None
    doctor = _resolve([doctor_front], known, parts[4] if parts else None)
    hospital = _resolve([front.get("hospital")], known, parts[5] if parts else None)
    subject = subject_token(front.get("subject")) if isinstance(front.get("subject"), str) else None
    if subject is None and parts and len(parts) > 6:
        tail = parts[6:]
        if len(tail) > 1 and re.fullmatch(r"\d{2}", tail[-1]):
            tail = tail[:-1]  # a collision suffix is rebuilt, never kept
        subject = subject_token("-".join(tail))
    fields = NameFields(
        date=day,
        patient=contract.patient_token,
        institution=institution,
        doc_type=doc_type,
        doctor=doctor,
        hospital=hospital,
        subject=subject,
    )
    if native:
        ext = NATIVE_EXT
        busy = taken | {f"{n}.{NATIVE_EXT}" for n in taken}
    else:
        ext = name.rsplit(".", 1)[-1] if "." in name else "bin"
        busy = set(taken)
    try:
        while True:
            built = with_collision_suffix(build_filename(fields, ext), busy)
            final = built.removesuffix(f".{NATIVE_EXT}") if native else built
            if f"{final}.md" not in taken:
                break
            busy.add(built)  # an orphan sidecar holds the pair's name: next suffix
    except ValueError:
        return Plan(code=CODE_NAME_NOT_STANDARD, why=WHY_NO_FREE_NAME)
    if not is_standard(final, mime=mime) or final == name:
        return Plan(code=CODE_NAME_NOT_STANDARD, why=WHY_BUILT_NOT_STANDARD)
    return Plan(target=final, date_source=source)
