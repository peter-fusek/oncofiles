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
* a month-precision date (#919, maintainer decision 2026-11-21): a sidecar
  ``doc_date: "YYYY-MM"``, a month-only note (``YYYY-MM-…``) or a ``YYYYMM00``
  name gets the date token ``YYYYMM00`` when that month and year is printed on
  its own in the text and passes ``classify.validate``'s month checks (not the
  README birth month, not in the future, not before 1900). After the rename
  a SIDECAR's front matter gets ``doc_date: "YYYY-MM"`` and
  ``doc_date_precision: month`` (key-only edit, body byte for byte;
  ``with_month_keys``); an original (a text note without a sidecar) is never
  written: its ``YYYYMM00`` name means month precision. A full printed day
  always wins over a month;
* a Google-native file keeps no extension (#922 rule); one whose title ends in
  an extension (``….docx``) keeps that one;
* hard guards (#946 dry run), month-precision targets included: a target the
  checker refuses is skipped with ``target_not_standard``, one whose extension
  differs from the source's with ``extension_changed`` (an extensionless
  binary included); never planned.

Pure functions, no I/O. Env (runtime, read at call time):
``CURATED_NORMALISE_FILED`` (default **false**: a missing env never renames;
a dry run always shows the plan)
and ``CURATED_NORMALISE_MAX`` (renames per real run, default 10).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date

from oncofiles.curated.classify import _parse_month, date_in_text, month_in_text
from oncofiles.curated.contract import Contract
from oncofiles.curated.naming import (
    NATIVE_EXT,
    PRECISION_KEY,
    PRECISION_MONTH,
    UNKNOWN,
    NameFields,
    build_filename,
    camel,
    date_precision,
    has_title,
    is_google_native,
    is_standard,
    month_token,
    subject_token,
    surname_token,
    with_collision_suffix,
)

DEFAULT_MAX = 10

# Codes (report only; never document content).
CODE_NAME_NOT_STANDARD = "name_not_standard"  # review: no rename possible
CODE_NO_SIDECAR = "no_sidecar"  # skipped: the backfill adds one first
# Hard guards of the planner (#946 dry-run finding), skipped with these codes:
CODE_TARGET_NOT_STANDARD = "target_not_standard"  # naming.is_standard(target) False
CODE_EXTENSION_CHANGED = "extension_changed"  # target extension != source extension
GUARD_CODES = (CODE_TARGET_NOT_STANDARD, CODE_EXTENSION_CHANGED)
# ``why`` of a name_not_standard entry
WHY_NO_PRINTED_DATE = "no_printed_date"
WHY_BUILT_NOT_STANDARD = "built_name_not_standard"
WHY_NO_FREE_NAME = "no_free_name"

# ``2027-03-…`` without a day: a month-precision note. A dashed full date
# (``2027-03-15_…``) is not month-only.
_MONTH_ONLY_RE = re.compile(r"^\d{4}-\d{2}(?!\d)(?!-\d{2}(?!\d))")
_COMPACT_MONTH_RE = re.compile(r"^(\d{4})(\d{2})00(?!\d)")  # a YYYYMM00 name
_FRONT_BLOCK_RE = re.compile(r"\A---\n(.*?)\n---(?=\n|\Z)", re.S)
_COMPACT_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?!\d)")
_DASHED_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?!\d)")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9]+$")


def enabled() -> bool:
    """``CURATED_NORMALISE_FILED`` (default false, infra review of #946): may a
    real run rename? Only an explicit true value turns renaming on."""
    value = os.environ.get("CURATED_NORMALISE_FILED", "false").strip().lower()
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


# A dot-suffix in a Google-native title that reads as a file extension (a
# letter, at most five characters: ``.docx``, ``.pdf``), e.g. a Google Doc
# converted from an upload or a shortcut to a PDF. ``Kontrola 12.3.2027`` has
# none: ``2027`` is part of the title.
_NATIVE_EXT_RE = re.compile(r"(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{1,5}")


def source_extension(name: str, *, native: bool) -> str:
    """The extension a rename must keep, lower case; '' when there is none.

    A binary file's extension is whatever follows its last dot. A Google-native
    file has no extension through the Drive API (#922), but its title may still
    end in one (``….docx``): that suffix is kept like a binary's, never dropped.
    """
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[1]
    if native and not _NATIVE_EXT_RE.fullmatch(ext):
        return ""
    return ext.lower()


def _target_extension(target: str) -> str:
    # a built name's fields hold no dots, so the last dot starts the extension
    return target.rsplit(".", 1)[1].lower() if "." in target else ""


def _stem(name: str, native: bool) -> str:
    if native:
        ext = source_extension(name, native=True)
        return name[: -len(ext) - 1] if ext else name
    return name.rsplit(".", 1)[0] if "." in name else name


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


def name_month(name: str) -> str | None:
    """``YYYY-MM`` of a month-only name (``2027-03-…``) or a ``YYYYMM00`` name."""
    if is_month_only(name):
        return name[:7]
    m = _COMPACT_MONTH_RE.match(name or "")
    return f"{m.group(1)}-{m.group(2)}" if m else None


def validated_month(candidates, text: str, contract: Contract, today: date) -> str | None:
    """The first candidate month (``YYYY-MM`` / ``YYYY-MM-00``) printed on its own
    in ``text`` that passes ``classify.validate``'s month checks, as ``YYYYMM00``."""
    births = {b[:6] for b in contract.birth_dates}
    for value in candidates:
        month = _parse_month(value)
        if month is None:
            continue
        y, mo = int(month[:4]), int(month[5:])
        if f"{y:04d}{mo:02d}" in births or (y, mo) > (today.year, today.month) or y < 1900:
            continue
        if month_in_text(month, text):
            return month_token(month)
    return None


def with_month_keys(text: str, year_month: str) -> str | None:
    """``text`` with ``doc_date: "YYYY-MM"`` and ``doc_date_precision: "month"``
    in its front matter (#919), or None when nothing needs to change or the
    front matter cannot be edited safely (an unclosed ``---`` block).

    Only those two keys change; everything after the front matter stays byte
    for byte. A text without front matter gets a new block in front of it.
    """
    want = {"doc_date": year_month, PRECISION_KEY: PRECISION_MONTH}
    m = _FRONT_BLOCK_RE.match(text)
    if m is None:
        if text.startswith("---"):
            return None  # looks like front matter but is not closed: never guess
        block = "\n".join(f"{k}: {json.dumps(v)}" for k, v in want.items())
        return f"---\n{block}\n---\n{text}"
    front, rest = m.group(1), text[m.end() :]
    new = front
    for key, value in want.items():
        line = f"{key}: {json.dumps(value)}"
        pattern = re.compile(rf"^{re.escape(key)}:.*$", re.M)
        if pattern.search(new):
            new = pattern.sub(lambda _m, line=line: line, new, count=1)
        else:
            new = f"{new}\n{line}"
    return None if new == front else f"---\n{new}\n---{rest}"


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
    precision: str | None = None  # "day" | "month" (#919)


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
    native = is_google_native(mime)
    parts = near_standard_parts(name, contract, native=native)
    name_date = _DASHED_DATE_RE.match(name) or _COMPACT_DATE_RE.match(name)
    front_date = front.get("doc_date")
    day = validated_date([front_date], text, contract, today)
    source = "front_matter"
    if day is None and name_date:
        day = validated_date([name_date.group(0)], text, contract, today)
        source = "filename"
    if day is None:  # #919: a month and year printed without a day
        day = validated_month([front_date], text, contract, today)
        source = "front_matter"
        if day is None and (month := name_month(name)):
            day = validated_month([month], text, contract, today)
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
    src_ext = source_extension(name, native=native)
    # Only a Google-native title WITHOUT an extension is built without one
    # (#922). A native title ending in ``.docx`` keeps it (#946 dry run: the
    # planner used to drop it); an extensionless binary is refused below.
    strip = native and not src_ext
    if strip:
        ext = NATIVE_EXT
        busy = taken | {f"{n}.{NATIVE_EXT}" for n in taken}
    else:
        ext = src_ext or "bin"
        busy = set(taken)
    try:
        while True:
            built = with_collision_suffix(build_filename(fields, ext), busy)
            final = built.removesuffix(f".{NATIVE_EXT}") if strip else built
            if f"{final}.md" not in taken:
                break
            busy.add(built)  # an orphan sidecar holds the pair's name: next suffix
    except ValueError:
        return Plan(code=CODE_NAME_NOT_STANDARD, why=WHY_NO_FREE_NAME)
    # Hard guards, whatever the builder returned: the checker (with the file's
    # mime, as the runner's candidate check and document_stage call it) must
    # accept the target, and the extension never changes (case aside).
    if not is_standard(final, mime=mime):
        return Plan(code=CODE_TARGET_NOT_STANDARD)
    if _target_extension(final) != src_ext:
        return Plan(code=CODE_EXTENSION_CHANGED)
    if final == name:
        return Plan(code=CODE_NAME_NOT_STANDARD, why=WHY_BUILT_NOT_STANDARD)
    return Plan(target=final, date_source=source, precision=date_precision(day))
