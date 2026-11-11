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
  document date is used, as the rule parsers do;
* the label maps to a code through the DB vocabulary (``fact_codes`` aliases
  via ``facts_rules.Vocabulary``: data, not layout rules);
* the unit converts to the code's canonical unit (``fact_codes`` data) and the
  value is plausible for the code.

Anything else stays ``unverified`` with reason codes. The output has the same
shape as ``facts_rules`` facts, so the two can be compared key by key.
"""

from __future__ import annotations

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
from oncofiles.curated.reread_verify import date_printed

VERIFIER_VERSION = 1


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
    m = _value_pattern(value).search(rest) if value else None
    if not m:
        reasons.append("value_not_in_evidence")
    elif comparator and not rest[: m.start()].rstrip().endswith(comparator):
        reasons.append("comparator_not_in_evidence")
    if "[?]" in evidence:
        reasons.append("ocr_uncertain")
    if source_reason:
        reasons.append(source_reason)

    numeric = bool(_NUMERIC.fullmatch(value))
    value_num = _num(value) if numeric else None

    # date
    printed = cand.get("date_as_printed")
    if printed:
        when = parse_printed_date(printed)
        if when is None:
            reasons.append("date_unparsed")
        elif not date_printed(when[:10], page_text):
            reasons.append("date_not_printed")
        date_source = "printed"
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

    meta: dict = {"verifier": VERIFIER_VERSION, "date_source": date_source}
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
