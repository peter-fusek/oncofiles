"""Generic deterministic verifier for model-extracted facts (#714).

The model (``curated.llm_extract``) says what is printed; this module decides
trust. It has NO layout knowledge — no table columns, no lab-specific line
formats. A candidate becomes ``machine`` only when all of these hold:

* its evidence line occurs verbatim on the page (whitespace normalised only);
* the label, the unit and the value's digits occur inside that evidence (the
  value outside the label and outside the printed reference range, so
  "CA 19-9" never yields 19 and a range bound never passes as the value);
* its date is printed on the page (``reread_verify.date_printed``) and is not
  a birth date (README contract / patient context); without a model date the
  document date is used, as the rule parsers do. A printed date RANGE
  ("3.5.2027 - 9.5.2027:") dates the fact at the range END
  (``date_source=printed_range``, both ends kept, #726) only when that very
  range is printed on the page; harmless decoration of the model's answer
  (surrounding spaces, a trailing ``:`` ``.`` ``,``, any dash) is tolerated,
  its meaning never (#826);
* the label maps to a code through the DB vocabulary (``fact_codes`` aliases
  via ``facts_rules.Vocabulary``: data, not layout rules);
* the unit converts to the code's canonical unit (``fact_codes`` data) and the
  value is plausible for the code.

Anything else stays ``unverified`` with reason codes. The output has the same
shape as ``facts_rules`` facts, so the two can be compared key by key.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date

from oncofiles.curated.facts_rules import (
    _ABSOLUTE_LABEL,
    _DIFFERENTIAL,
    _UNIT_HINT,
    PLAUSIBLE,
    Vocabulary,
    _flag,
    _num,
    _unit_key,
    _upper_from_text,
    norm_label,
)
from oncofiles.curated.reocr import _DATE_TOKEN, _date_key
from oncofiles.curated.reread_verify import date_printed

# v2 (#826): a printed date range is parsed (dated at its end, both ends must
# be printed on the page as that range); trailing ":" "." "," on a date.
# v3 (#833): filler dots between a label and its value ("Glukóza .....5.2")
# count as whitespace when the value is looked up in the evidence, unless
# the run of dots directly follows a digit.
VERIFIER_VERSION = 3


# Facts parsed from LM Studio's ``reasoning_content`` channel (content empty)
# are NEVER machine (infra rule, #714): ``verify`` always adds this reason,
# and any future write path MUST store such facts as unverified with it.
REASONING_CHANNEL_REASON = "llm_reasoning_channel"
REASONING_CHANNEL_NEVER_MACHINE = True


def from_reasoning_channel(fact_or_candidate: dict) -> bool:
    """True for a candidate or fact that came from the reasoning channel."""
    meta = fact_or_candidate.get("metadata") or {}
    source = fact_or_candidate.get("answer_source") or meta.get("answer_source")
    return source == "reasoning"


# Text-only extraction from the STORED sidecar text is not an independent
# source (design rule on #714): when that text is a Cowork-era transcription
# (non-local sidecar → ``cowork_ocr``), or the stored counterpart of a fact
# carries ``cowork_ocr`` / ``ocr_uncertain`` / ``handwritten``, the LLM read
# the same unreliable text the rule parser read. Such facts keep those reasons
# and stay unverified; the LLM text path never upgrades them to machine and
# never removes those reasons. Agreement there is a parser cross-check only.
# Independent confirmation needs a new read of the source (#678 re-read).
TEXT_PATH_NEVER_UPGRADES_TRANSCRIPTION_REASONS = True
TRANSCRIPTION_REASONS = frozenset({"cowork_ocr", "ocr_uncertain", "handwritten"})


def stored_reasons(row: dict) -> list[str]:
    """``unverified_reasons`` of a stored fact row (parsed ``meta`` or ``metadata_json``)."""
    meta = row.get("meta")
    if meta is None:
        try:
            meta = json.loads(row.get("metadata_json") or "{}")
        except ValueError:
            meta = {}
    return list((meta or {}).get("unverified_reasons") or [])


def _match_key(f: dict) -> tuple:
    return (f.get("code"), (f.get("occurred_at") or "")[:10], f.get("value_canonical"))


def inherit_transcription_reasons(facts: list[dict], stored_rows: list[dict]) -> list[dict]:
    """Copy the transcription reasons of a matching stored fact (same code,
    day and value) onto the LLM fact; such a fact becomes unverified. Only
    ever ADDS reasons. Returns ``facts`` (changed in place)."""
    if not TEXT_PATH_NEVER_UPGRADES_TRANSCRIPTION_REASONS:
        return facts
    inherited: dict[tuple, set[str]] = {}
    for r in stored_rows:
        hits = TRANSCRIPTION_REASONS.intersection(stored_reasons(r))
        if hits and r.get("code"):
            inherited.setdefault(_match_key(r), set()).update(hits)
    for f in facts:
        hits = inherited.get(_match_key(f))
        if not hits:
            continue
        meta = f.setdefault("metadata", {})
        reasons = list(meta.get("unverified_reasons") or [])
        reasons += sorted(h for h in hits if h not in reasons)
        meta["unverified_reasons"] = reasons
        meta["inherited_from_stored"] = sorted(hits)
        f["verification"] = "unverified"
    return facts


def transcription_pages(stored_rows: list[dict], source_reason: str | None,
                        pages) -> set[int]:  # fmt: skip
    """Pages whose text is a transcription: every page when the sidecar
    itself is (``source_reason``), else pages holding a stored fact with a
    transcription reason."""
    if source_reason in TRANSCRIPTION_REASONS:
        return {int(p) for p in pages}
    return {
        int(r.get("page_number") or 1)
        for r in stored_rows
        if TRANSCRIPTION_REASONS.intersection(stored_reasons(r))
    }


class NoBirthDateError(RuntimeError):
    """A write of model-extracted facts was attempted without a known DOB."""


def require_dob_for_write(birth_dates) -> None:
    """MUST be called by every future write path for LLM-extracted facts
    (#714 Phase 2b). Without a known birth date the ``birth_date`` guard is
    inactive, so a DOB printed in a header could be stored as a result date:
    the write is refused. The shadow preview (Phase 2a) writes nothing and
    only reports ``birth_date_guard: inactive_no_dob``."""
    if not birth_dates:
        raise NoBirthDateError("no birth date known for this patient: refusing to store facts")


REASONS = (
    "evidence_not_found",  # the evidence line is not on the page
    "label_not_in_evidence",
    "value_not_in_evidence",  # value digits (outside label / ref range) not in the evidence
    "comparator_not_in_evidence",
    "unit_not_in_evidence",
    "date_not_printed",  # the model's date is not printed on this page
    "date_unparsed",
    "date_missing",  # no model date and no document date
    "birth_date",
    "label_unmapped",  # no fact_codes alias matches; review list → add an alias (data)
    "label_fuzzy",  # matched with one typo only
    "unit_unconvertible",  # no conversion to the canonical unit in fact_codes
    "implausible",
    "ocr_uncertain",  # "[?]" in the evidence
    "llm_reasoning_channel",  # parsed from reasoning_content: never machine
)

_NUMERIC = re.compile(r"-?\d+(?:[.,]\d+)?")
_RANGE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*[-–]\s*(-?\d+(?:[.,]\d+)?)\s*$")
_DMY = re.compile(r"^\s*(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})(?:[\s,]+(\d{1,2}):(\d{2}))?\s*$")
_YMD = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2}))?\s*$")
_HIGH = {"H", "HH", "↑", "▲"}
_LOW = {"L", "LL", "↓", "▼"}


def ws(s: str | None) -> str:
    """Whitespace normalisation (and Unicode NFC) — the only normalisation."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip()


# Decoration of a model's date answer that carries no meaning (#826): the
# colon of a header line ("3.5.2027 - 9.5.2027:"), a sentence dot or comma.
_DATE_TRAIL = re.compile(r"[\s:.,]+$")
_DASH = "[-\u2013\u2014]"  # hyphen, en dash, em dash
_DM = r"(\d{1,2})\.\s?(\d{1,2})\."
_RANGE_BODY = rf"{_DM}(?:\s?(\d{{4}}))?\s*{_DASH}\s*{_DM}\s?(\d{{4}})"
_DATE_RANGE = re.compile(rf"^{_RANGE_BODY}$")
_DATE_RANGE_ON_PAGE = re.compile(rf"(?<![\d.]){_RANGE_BODY}(?!\d)")


def undecorate_date(s: str | None) -> str:
    """``s`` without surrounding whitespace and without a trailing ``:``,
    ``.`` or ``,`` — the shape of the answer only, never its digits."""
    return _DATE_TRAIL.sub("", (s or "").strip())


def _range_key(m: re.Match) -> tuple[str, str]:
    """(start, end) of a range match as ``reocr._date_key`` keys — the date
    normalisation of the re-OCR loss guard (#804). A start printed without a
    year keeps that partial form ("3.5."), so only the same form matches."""
    d0, m0, y0, d1, m1, y1 = m.groups()
    return _date_key(f"{d0}.{m0}.{y0 or ''}"), _date_key(f"{d1}.{m1}.{y1}")


def parse_printed_range(s: str | None) -> tuple[str, str, tuple[str, str]] | None:
    """'3.5.2027 - 9.5.2027', '3.5.–9.5.2027' → (start ISO, end ISO, key).

    Both ends are full day.month dates; only the START may lack its year,
    which is then the year of the end printed in the same range. No 2-digit
    years, no day-only start, no swapped or reversed ends: anything else is
    not a range (``None``). ``key`` is what ``range_printed`` looks for."""
    m = _DATE_RANGE.match(undecorate_date(s))
    if not m:
        return None
    d0, m0, y0, d1, m1, y1 = m.groups()
    try:
        start = date(int(y0 or y1), int(m0), int(d0))
        end = date(int(y1), int(m1), int(d1))
    except ValueError:
        return None
    if start > end:
        return None
    return start.isoformat(), end.isoformat(), _range_key(m)


def range_printed(key: tuple[str, str], page_text: str) -> bool:
    """Is this very range printed on the page: both ends, in this order,
    joined by a dash, the start with or without its year exactly as in
    ``key``? Two dates that merely both occur on the page are not a range."""
    return any(_range_key(m) == key for m in _DATE_RANGE_ON_PAGE.finditer(page_text or ""))


def date_exactly_printed(day: str, page_text: str) -> bool:
    """Is ``day`` (ISO) printed on the page with its full year? Compared by
    ``reocr._date_key`` (#804): no 2-digit year, no bare day.month."""
    y, mo, d = day[:10].split("-")
    key = f"{int(d)}.{int(mo)}.{y}"
    return any(_date_key(t.group(0)) == key for t in _DATE_TOKEN.finditer(page_text or ""))


def parse_printed_date(s: str | None) -> str | None:
    """'5.3.2027', '05.03.2027 07:40', '2027-03-05' → ISO date[Thh:mm]."""
    if not s:
        return None
    m = _DMY.match(s)
    if m:
        d, mo, y, h, mi = m.groups()
    elif m := _YMD.match(s):
        y, mo, d, h, mi = m.groups()
    else:
        return None
    try:
        out = date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None
    if h is not None and int(h) < 24 and int(mi) < 60:
        return f"{out}T{int(h):02d}:{mi}"
    return out


def _value_pattern(value: str) -> re.Pattern:
    v = value.strip()
    if _NUMERIC.fullmatch(v):
        whole, _, frac = v.replace(",", ".").partition(".")
        body = re.escape(whole) + (rf"[.,]{frac}0*" if frac else r"(?![.,]\d)")
        return re.compile(rf"(?<![\d.,]){body}(?!\d)")
    return re.compile(rf"(?<!\w){re.escape(ws(v))}(?!\w)", re.I)


# A dot leader (#833): two or more "." / "…" (a single "…" counts as one
# leader), optionally one space apart (". . . ."). The whole run must not
# follow a digit: "3...4.71" could be a truncated "3.", so it stays as it is.
# A single "." is never a leader (decimal or date context).
_DOT_LEADER = re.compile(r"(?<![\d.…])(?<![.…] )(?:…|\.(?= ?[.…]))(?: ?[.…])*")


def blank_dot_leaders(text: str, printed: str | None = None) -> str:
    """``text`` with every dot leader replaced by spaces of the same length,
    so the value patterns (which refuse a "." right before a number) see
    "Glukóza          5.2". The leaders are located in ``printed`` — the
    same text before anything was blanked out of it, same length — so a
    digit that was blanked (a label ending in one) still blocks the leader.
    Nothing is added or moved: only "." and "…" become spaces."""
    source = printed if printed is not None and len(printed) == len(text) else text
    for m in _DOT_LEADER.finditer(source):
        text = text[: m.start()] + " " * (m.end() - m.start()) + text[m.end() :]
    return text


def _blank(text: str, needle: str | None) -> str:
    """``text`` with the first case-insensitive occurrence of ``needle`` blanked."""
    if not needle:
        return text
    i = text.lower().find(ws(needle).lower())
    if i < 0:
        return text
    return text[:i] + " " * len(ws(needle)) + text[i + len(ws(needle)) :]


def _birth_isos(birth_dates) -> set[str]:
    out = set()
    for b in birth_dates or ():
        b = str(b).strip()
        if re.fullmatch(r"\d{8}", b):
            b = f"{b[:4]}-{b[4:6]}-{b[6:]}"
        out.add(b[:10])
    return out


def _code_for(vocab: Vocabulary, label: str, unit: str | None) -> tuple[dict | None, bool]:
    """(fact_codes row, fuzzy?) — vocabulary lookup plus the %/count split,
    which follows the unit printed next to the value (data, not layout)."""
    row = vocab.match(label)
    fuzzy = False
    if row is None and (row := vocab.match_fuzzy(label)) is not None:
        fuzzy = True
    if row is None:
        return None, False
    u = (unit or "").strip()
    if u == "%" and row.get("unit_canonical") != "%":
        row = vocab.by_code.get(row["code"] + "_PCT") or row
    elif u and u != "%" and row["code"].endswith("_PCT") and _UNIT_HINT.search(u):
        row = vocab.by_code.get(row["code"][: -len("_PCT")]) or row
    elif not u and row["code"] in _DIFFERENTIAL and not _ABSOLUTE_LABEL.search(norm_label(label)):
        row = vocab.by_code.get(row["code"] + "_PCT") or row  # unitless differential = %
    return row, fuzzy


def verify(  # noqa: PLR0912, PLR0915
    cand: dict,
    *,
    page_text: str,
    vocab: Vocabulary,
    report_date: str | None = None,
    birth_dates=(),
    source_reason: str | None = None,
) -> dict:
    """One candidate → one fact dict (``facts_rules`` shape), ``machine`` or
    ``unverified`` with ``metadata.unverified_reasons``."""
    reasons: list[str] = []
    evidence = ws(cand.get("evidence"))
    label = ws(cand.get("label_as_printed"))
    value = (cand.get("value") or "").strip()
    unit = ws(cand.get("unit_as_printed")) or None
    ref = ws(cand.get("ref_range_as_printed")) or None
    comparator = cand.get("comparator")
    ev_low = evidence.lower()

    if not evidence or evidence not in ws(page_text):
        reasons.append("evidence_not_found")
    if not label or label.lower() not in ev_low:
        reasons.append("label_not_in_evidence")
    if unit and unit.lower() not in ev_low:
        reasons.append("unit_not_in_evidence")
    rest = _blank(evidence, label)
    if ref and ref.lower() in rest.lower():
        rest = _blank(rest, ref)
    rest = blank_dot_leaders(rest, evidence)  # filler dots are whitespace (#833)
    m = _value_pattern(value).search(rest) if value else None
    if not m:
        reasons.append("value_not_in_evidence")
    elif comparator and not rest[: m.start()].rstrip().endswith(comparator):
        reasons.append("comparator_not_in_evidence")
    if "[?]" in evidence:
        reasons.append("ocr_uncertain")
    if source_reason:
        reasons.append(source_reason)
    if REASONING_CHANNEL_NEVER_MACHINE and from_reasoning_channel(cand):
        reasons.append(REASONING_CHANNEL_REASON)

    numeric = bool(_NUMERIC.fullmatch(value))
    value_num = _num(value) if numeric else None

    # date
    printed = cand.get("date_as_printed")
    range_meta: dict = {}
    if printed:
        date_source = "printed"
        when = parse_printed_date(printed)
        if when is not None:
            if not date_printed(when[:10], page_text):
                reasons.append("date_not_printed")
        elif rng := parse_printed_range(printed):
            # a printed range dates the fact at its END (#726); the range
            # itself, both ends, must be printed on this page
            start, when, key = rng
            date_source = "printed_range"
            range_meta = {"date_precision": "range", "date_range_start": start,
                          "date_range_end": when}  # fmt: skip
            if not range_printed(key, page_text):
                reasons.append("date_not_printed")
            if start in _birth_isos(birth_dates):
                reasons.append("birth_date")
        elif when := parse_printed_date(undecorate_date(printed)):
            # a decorated single date ("9.5.2027:") must be on the page as is
            if not (date_exactly_printed(when, page_text) and date_printed(when[:10], page_text)):
                reasons.append("date_not_printed")
        else:
            reasons.append("date_unparsed")
    else:
        when = report_date
        date_source = "document"
        if not when:
            reasons.append("date_missing")
    if when and when[:10] in _birth_isos(birth_dates):
        reasons.append("birth_date")

    # vocabulary, units, plausibility (fact_codes data)
    row, fuzzy = _code_for(vocab, label, unit)
    if row is None:
        reasons.append("label_unmapped")
    elif fuzzy:
        reasons.append("label_fuzzy")
    unit_canonical = row.get("unit_canonical") if row else None
    value_canonical = None
    if row and numeric:
        uk, ck = _unit_key(unit), _unit_key(unit_canonical)
        conv = {_unit_key(k): f for k, f in (row.get("unit_conversions") or {}).items()}
        if not uk or uk == ck:
            value_canonical = value_num
        elif uk in conv:
            value_canonical = round(value_num * float(conv[uk]), 6)
        else:
            reasons.append("unit_unconvertible")
        if value_canonical is not None and row["code"] in PLAUSIBLE:
            lo_p, hi_p = PLAUSIBLE[row["code"]]
            if not lo_p <= value_canonical <= hi_p:
                reasons.append("implausible")

    lo = hi = None
    ref_text = None
    if ref:
        rm = _RANGE.match(ref)
        if rm:
            lo, hi = _num(rm.group(1)), _num(rm.group(2))
        else:
            ref_text = ref
            hi = _upper_from_text(ref)
    printed_flag = ws(cand.get("flag_as_printed")) or None
    if printed_flag in _HIGH:
        flag = "H"
    elif printed_flag in _LOW:
        flag = "L"
    else:
        flag = _flag(value_num, lo, hi, None)

    meta: dict = {"verifier": VERIFIER_VERSION, "date_source": date_source,
                  "answer_source": cand.get("answer_source") or "content"}  # fmt: skip
    meta.update(range_meta)
    if cand.get("date_role"):
        meta["date_role"] = cand["date_role"]
    if comparator:
        meta["comparator"] = comparator
    if printed_flag:
        meta["flag_printed"] = printed_flag
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        meta["unverified_reasons"] = reasons
    return {
        "category": (row or {}).get("category", "lab"),
        "code": row["code"] if row else None,
        "label": label,
        "value_num": value_num,
        "value_text": None if numeric else value,
        "unit": unit,
        "ref_low": lo,
        "ref_high": hi,
        "ref_text": ref_text,
        "flag": flag,
        "value_canonical": value_canonical,
        "unit_canonical": unit_canonical if value_canonical is not None else None,
        "occurred_at": when,
        "page_number": cand.get("page_number"),
        "evidence": evidence[:300],
        "verification": "unverified" if reasons else "machine",
        "metadata": meta,
    }
