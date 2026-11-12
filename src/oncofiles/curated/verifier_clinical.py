"""Generic deterministic verifier for the non-lab kinds (#726 item 4, #711 phase 2b).

The local model (``curated.llm_extract_clinical``) says what is printed; this
module decides trust, exactly like ``curated.verifier`` does for lab values,
and with the same rule: NO layout knowledge. An item becomes ``machine`` only
when all of these hold:

* its evidence passage occurs verbatim on the page (whitespace normalised);
* every key token the model returned occurs inside that evidence: diagnosis
  text, code, site and stage; marker, variant, result and method; regimen,
  line and intent; drug, unit and route; lesion label, unit and description.
  Numbers (dose, lesion size) must occur as whole numbers OUTSIDE the label,
  the unit and any date of the evidence, so "S7" never yields a 7 mm lesion
  and "12.3.2027" never yields a dose of 12;
* every date is printed on the page AND inside the evidence, and is not a
  birth date (README contract / patient context);
* a biomarker label maps to a ``fact_codes`` genetic_variant / biomarker code
  (vocabulary is data); a lesion size has a convertible unit (mm, cm).

Anything else stays ``unverified`` with reason codes. Nothing is ever filled
in: a field the model left empty stays empty, and only a lesion size or a
biomarker result without a printed date takes the document date (the date the
report states it), marked ``date_source = document`` — as the lab verifier does.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from oncofiles.curated import verifier as _lab_verifier
from oncofiles.curated.reread_verify import date_printed
from oncofiles.curated.verifier import _birth_isos, _value_pattern, parse_printed_date, ws

# 2: reasoning-channel items never machine; 3: printed date forms "5.3.27"
# and month names ("5. marca 2027", "marec 2027", Slovak and Czech) parse
CLINICAL_VERIFIER_VERSION = 3

# answer key -> fields whose (non-empty) value must occur inside the evidence
TOKEN_FIELDS: dict[str, tuple[str, ...]] = {
    "diagnoses": ("text_as_printed", "code_as_printed", "site_as_printed", "stage_as_printed"),
    "biomarkers": (
        "marker_as_printed", "variant_as_printed", "result_as_printed", "method_as_printed",
    ),
    "treatment_courses": ("regimen_as_printed", "line_as_printed", "intent_as_printed"),
    "cycles": ("regimen_as_printed", "cycle_as_printed", "reason_as_printed"),
    "medications": ("drug_as_printed", "unit_as_printed", "route_as_printed", "cycle_as_printed"),
    "procedures": ("description_as_printed",),
    "lesions": ("lesion_as_printed", "unit_as_printed", "description_as_printed"),
    "planned": ("description_as_printed",),
}  # fmt: skip
DATE_FIELDS: dict[str, tuple[str, ...]] = {
    "diagnoses": ("date_as_printed",),
    "biomarkers": ("date_as_printed",),
    "treatment_courses": ("start_date_as_printed", "end_date_as_printed"),
    "cycles": ("date_as_printed",),
    "medications": ("start_date_as_printed", "end_date_as_printed"),
    "procedures": ("date_as_printed", "end_date_as_printed"),
    "lesions": ("date_as_printed",),
    "planned": ("date_as_printed",),
}
REASONS = (
    "evidence_not_found",
    "<field>_not_in_evidence",  # e.g. drug_not_in_evidence, variant_not_in_evidence
    "dose_not_in_evidence",  # dose digits not in the evidence (outside drug, unit, dates)
    "size_not_in_evidence",  # lesion size digits not in the evidence (outside label, dates)
    "date_unparsed",
    "date_not_printed",  # not on the page at all
    "date_not_in_evidence",  # on the page, but not inside the item's evidence
    "date_missing",  # lesion / biomarker without a printed date and no document date
    "birth_date",
    "label_unmapped",  # biomarker label without a fact_codes alias (review → add data)
    "label_fuzzy",
    "label_not_biomarker",  # the label maps to a lab / tumour-marker code
    "unit_unconvertible",  # lesion size unit other than mm / cm
    "implausible",
    "ocr_uncertain",  # "[?]" in the evidence
    "handwritten",  # source reasons, carried from the document
    "cowork_ocr",
)
BIOMARKER_CATEGORIES = ("genetic_variant", "biomarker")
LESION_MM_MAX = 1000.0  # a lesion size above 1 m is a misread, never a measurement

_NUMERIC = re.compile(r"\d+(?:[.,]\d+)?")
_MONTH_YEAR = re.compile(r"^\s*(\d{1,2})\s*[./]\s*((?:19|20)\d{2})\s*$")
_YEAR = re.compile(r"^\s*((?:19|20)\d{2})\s*$")
_DMY2 = re.compile(r"^\s*(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{2})\s*$")  # "5.3.27"
_NAMED = re.compile(r"^\s*(?:(\d{1,2})\.\s*)?([^\W\d_]+)\s+((?:19|20)\d{2})\s*$")
# Slovak and Czech month names (nominative, genitive, locative), without
# diacritics: a printed form, so data, not layout.
_MONTHS = {
    w: n
    for n, words in enumerate((
        "januar januara januari leden ledna lednu",
        "februar februara februari unor unora unoru",
        "marec marca marci brezen brezna breznu",
        "april aprila aprili duben dubna dubnu",
        "maj maja maji kveten kvetna kvetnu",
        "jun juna juni cerven cervna cervnu",
        "jul jula juli cervenec cervence cervenci",
        "august augusta auguste srpen srpna srpnu",
        "september septembra septembri zari",
        "oktober oktobra oktobri rijen rijna rijnu",
        "november novembra novembri listopad listopadu",
        "december decembra decembri prosinec prosince prosinci",
    ), 1)
    for w in words.split()
}  # fmt: skip
_DATE_LIKE = re.compile(
    r"\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s?[./]\s?(?:19|20)\d{2}"
    r"|\b\d{1,2}:\d{2}\b"
)
_NAMED_LIKE = re.compile(r"(?<!\w)(?:\d{1,2}\.\s*)?([^\W\d_]+)\s+(?:19|20)\d{2}(?!\d)")
_ICD = re.compile(r"^[A-Z]\d{2}(?:\.\d{1,2})?[A-Z]?$")
_CANCER = re.compile(
    r"karcin|carcin|zhubn|malígn|malign|nádor|tumor|sarkóm|sarcom|lymfóm|lymphom|leukém"
    r"|leukaem|melanóm|melanom|blastóm|blastom|myelóm|myelom",
    re.I,
)
_SUSPECTED = re.compile(r"\bsusp|suspekt|podozren|\bv\.\s?s\.|\bsuspected\b", re.I)
_INTENTS = (  # printed stem -> FHIR-ish intent; neoadjuvant before adjuvant
    ("neoadjuv", "neoadjuvant"), ("adjuv", "adjuvant"), ("paliat", "palliative"),
    ("palliat", "palliative"), ("kurat", "curative"), ("curat", "curative"),
)  # fmt: skip
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}
_UCUM = {  # printed dose unit (lower, no spaces) -> UCUM
    "mg/m2": "mg/m2", "mg/m²": "mg/m2", "mg/kg": "mg/kg", "mg": "mg", "g": "g",
    "g/m2": "g/m2", "g/m²": "g/m2", "µg": "ug", "ug": "ug", "mcg": "ug", "ml": "mL",
    "µg/kg": "ug/kg", "ug/kg": "ug/kg", "mg/h": "mg/h", "ml/h": "mL/h",
}  # fmt: skip
_TNM_T = re.compile(r"(?<![A-Za-z])(yc|yp|c|p|r)?T(is|[0-4xX])([a-d])?(?![A-Za-z0-9])")
_TNM_N = re.compile(r"(?<![A-Za-z])(?:yc|yp|c|p|r)?N([0-3xX])([a-c])?(?![A-Za-z0-9])")
_TNM_M = re.compile(r"(?<![A-Za-z])(?:yc|yp|c|p)?M([01xX])([a-c])?(?![A-Za-z0-9])")
_STAGE_GROUP = re.compile(r"(?:štádium|stádium|stage|št\.)\s*([0IV]{1,4}[A-C]?\d?)", re.I)


def _short(field: str) -> str:
    return field.removesuffix("_as_printed")


def _month_number(word: str) -> int | None:
    plain = unicodedata.normalize("NFD", word.lower())
    return _MONTHS.get("".join(c for c in plain if not unicodedata.combining(c)))


def parse_any_date(printed: str | None) -> tuple[str | None, str | None]:
    """(iso, precision): '5.3.2027' / '5.3.27' / '5. marca 2027' →
    ('2027-03-05', 'day'), '03/2027' / 'marec 2027' → ('2027-03', 'month'),
    '2027' → ('2027', 'year'). Never guesses a day or a year."""
    if not printed:
        return None, None
    full = parse_printed_date(printed)
    if full:
        return full, "datetime" if "T" in full else "day"
    if m := _DMY2.match(printed):  # two-digit year: 00-68 → 20xx (strptime's %y)
        d, mo, yy = (int(g) for g in m.groups())
        try:
            return date(2000 + yy if yy <= 68 else 1900 + yy, mo, d).isoformat(), "day"
        except ValueError:
            return None, None
    if (m := _NAMED.match(printed)) and (month := _month_number(m.group(2))):
        year = int(m.group(3))
        if m.group(1) is None:
            return f"{year:04d}-{month:02d}", "month"
        try:
            return date(year, month, int(m.group(1))).isoformat(), "day"
        except ValueError:
            return None, None
    if m := _MONTH_YEAR.match(printed):
        month, year = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}", "month"
        return None, None
    if m := _YEAR.match(printed):
        return m.group(1), "year"
    return None, None


def _blank_all(text: str, needle: str | None) -> str:
    """``text`` with every case-insensitive occurrence of ``needle`` blanked."""
    n = ws(needle)
    if not n:
        return text
    return re.sub(re.escape(n), lambda m: " " * len(m.group(0)), text, flags=re.I)


def _numbers_in(numbers: str, evidence: str, *, blank=()) -> bool:
    """Every number of ``numbers`` occurs whole in ``evidence`` outside the
    ``blank`` strings and outside any date or time."""
    rest = evidence
    for b in blank:
        rest = _blank_all(rest, b)
    rest = _DATE_LIKE.sub(lambda m: " " * len(m.group(0)), rest)
    rest = _NAMED_LIKE.sub(  # "5. marca 2027", only when the word is a month name
        lambda m: " " * len(m.group(0)) if _month_number(m.group(1)) else m.group(0), rest
    )
    found = _NUMERIC.findall(numbers or "")
    return bool(found) and all(_value_pattern(n).search(rest) for n in found)


def _num(s: str | None) -> float | None:
    if not s or not re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*", s):
        return None
    return float(s.strip().replace(",", "."))


def _check_date(printed: str, *, evidence: str, page: str, births: set[str], reasons: list):
    iso, precision = parse_any_date(printed)
    if iso is None:
        reasons.append("date_unparsed")
        return None
    if precision in ("day", "datetime") and not re.search(r"[^\W\d_]", printed):
        on_page, in_evidence = date_printed(iso[:10], page), date_printed(iso[:10], evidence)
    else:  # month / year, or a day with a month name: the printed form itself
        needle = re.compile(rf"(?<!\w){re.escape(ws(printed))}(?!\w)", re.I)
        on_page, in_evidence = bool(needle.search(page)), bool(needle.search(evidence))
    if not on_page:
        reasons.append("date_not_printed")
    elif not in_evidence:
        reasons.append("date_not_in_evidence")
    if any(b.startswith(iso[:10]) for b in births):
        reasons.append("birth_date")
    return {"iso": iso, "precision": precision, "printed": ws(printed)}


def parse_stage(printed: str | None) -> dict:
    """Parts of a printed stage string; only substrings that are printed."""
    if not printed:
        return {}
    out: dict = {}
    if m := _TNM_T.search(printed):
        out["prefix"], out["t"] = m.group(1), f"T{m.group(2)}{m.group(3) or ''}"
    if m := _TNM_N.search(printed):
        out["n"] = f"N{m.group(1)}{m.group(2) or ''}"
    if m := _TNM_M.search(printed):
        out["m"] = f"M{m.group(1)}{m.group(2) or ''}"
    if m := _STAGE_GROUP.search(printed):
        out["stage_group"] = m.group(1)
    for system in ("UICC", "AJCC"):
        if system in printed:
            out["system"] = system
    return {k: v for k, v in out.items() if v}


def line_of_therapy(printed: str | None) -> int | None:
    if not printed:
        return None
    if m := re.search(r"\d+", printed):
        return int(m.group(0))
    if m := re.match(r"\s*([ivx]+)\b", printed, re.I):
        return _ROMAN.get(m.group(1).lower())
    return None


def intent_of(printed: str | None) -> str | None:
    low = (printed or "").lower()
    return next((intent for stem, intent in _INTENTS if stem in low), None)


def dose_unit(printed: str | None) -> tuple[str | None, str | None]:
    """(UCUM unit, normalisation basis) of a printed dose unit; unknown → None."""
    key = re.sub(r"\s+", "", (printed or "").lower())
    if not key:
        return None, None
    ucum = _UCUM.get(key)
    if "/m2" in key or "/m²" in key:
        basis = "per_m2"
    elif "/kg" in key:
        basis = "per_kg"
    elif ucum in ("mg", "g", "ug", "mL"):
        basis = "flat"
    else:
        basis = None
    return ucum, basis


def _vocab_row(vocab, label: str):
    row = vocab.match(label)
    if row is not None:
        return row, False
    row = vocab.match_fuzzy(label)
    return row, row is not None


def verify_item(  # noqa: PLR0912, PLR0915
    cand: dict,
    *,
    page_text: str,
    vocab,
    report_date: str | None = None,
    birth_dates=(),
    source_reason: str | None = None,
) -> dict:
    """One candidate of any non-lab kind → one verified item:
    ``{"kind", "page_number", "evidence", "verification", "reasons", "fields",
    "dates", "derived"}``. ``fields`` are the printed values as returned (only
    whitespace-normalised); ``derived`` holds what is computed from them
    deterministically (codes, numbers, units, parsed stage parts)."""
    kind = cand["kind"]
    fields = {
        k: v
        for k, v in cand.items()
        if k not in ("kind", "page_number", "evidence", "answer_source")
    }
    reasons: list[str] = []
    evidence = ws(cand.get("evidence"))
    ev_low = evidence.lower()
    page = ws(page_text)
    births = _birth_isos(birth_dates)

    if not evidence or evidence not in page:
        reasons.append("evidence_not_found")
    for field in TOKEN_FIELDS[kind]:
        value = ws(fields.get(field))
        if value and value.lower() not in ev_low:
            reasons.append(f"{_short(field)}_not_in_evidence")
    if "[?]" in evidence:
        reasons.append("ocr_uncertain")
    if source_reason:
        reasons.append(source_reason)
    # Same rule as the lab verifier (#714): an answer read from the model's
    # reasoning channel is never machine-trusted, whatever else checks out.
    if _lab_verifier.REASONING_CHANNEL_NEVER_MACHINE and _lab_verifier.from_reasoning_channel(cand):
        reasons.append(_lab_verifier.REASONING_CHANNEL_REASON)

    dates: dict[str, dict] = {}
    for field in DATE_FIELDS[kind]:
        if fields.get(field):
            d = _check_date(fields[field], evidence=evidence, page=page, births=births,
                            reasons=reasons)  # fmt: skip
            if d:
                dates[_short(field)] = d

    derived: dict = {}
    if kind == "diagnoses":
        code = ws(fields.get("code_as_printed")).upper().replace(" ", "")
        derived["icd10"] = code if _ICD.match(code) else None
        derived["stage"] = parse_stage(fields.get("stage_as_printed"))
        text = fields.get("text_as_printed") or ""
        derived["is_primary_cancer"] = fields.get("role") == "primary" and bool(
            (derived["icd10"] or "").startswith(("C", "D0")) or _CANCER.search(text)
        )
        derived["certainty"] = "provisional" if _SUSPECTED.search(text) else None
    elif kind == "biomarkers":
        row, fuzzy = _vocab_row(vocab, fields.get("marker_as_printed") or "")
        if row is None:
            reasons.append("label_unmapped")
        elif row.get("category") not in BIOMARKER_CATEGORIES:
            reasons.append("label_not_biomarker")
            row = None
        elif fuzzy:
            reasons.append("label_fuzzy")
        derived["code"] = row["code"] if row else None
        derived["record_type"] = (row or {}).get("category") or (
            "genetic_variant" if fields.get("variant_as_printed") else "biomarker"
        )
    elif kind == "treatment_courses":
        derived["line"] = line_of_therapy(fields.get("line_as_printed"))
        derived["intent"] = intent_of(fields.get("intent_as_printed"))
    elif kind == "cycles":
        m = re.search(r"\d+", fields.get("cycle_as_printed") or "")
        derived["cycle_number"] = int(m.group(0)) if m else None
    elif kind == "medications":
        dose = ws(fields.get("dose_as_printed"))
        blank = (fields.get("drug_as_printed"), fields.get("unit_as_printed"),
                 fields.get("cycle_as_printed"))  # fmt: skip
        if dose and not _numbers_in(dose, evidence, blank=blank):
            reasons.append("dose_not_in_evidence")
        derived["dose_value"] = _num(dose)
        derived["unit_ucum"], derived["basis"] = dose_unit(fields.get("unit_as_printed"))
    elif kind == "lesions":
        size = ws(fields.get("size_as_printed"))
        if not _numbers_in(size, evidence, blank=(fields.get("lesion_as_printed"),)):
            reasons.append("size_not_in_evidence")
        unit = re.sub(r"\s+", "", (fields.get("unit_as_printed") or "").lower())
        factor = {"mm": 1.0, "cm": 10.0}.get(unit)
        dims = [float(n.replace(",", ".")) for n in _NUMERIC.findall(size)]
        if factor is None:
            reasons.append("unit_unconvertible")
        elif dims:
            mm = [round(d * factor, 3) for d in dims]
            derived["dims_mm"] = mm
            derived["size_mm"] = max(mm)
            if not 0 < derived["size_mm"] <= LESION_MM_MAX:
                reasons.append("implausible")

    if kind in ("lesions", "biomarkers"):  # a measured value needs a time, as labs do
        if "date" in dates:
            derived["date_source"] = "printed"
        elif report_date:
            dates["date"] = {"iso": report_date[:10], "precision": "day", "printed": None}
            derived["date_source"] = "document"
            if any(b.startswith(report_date[:10]) for b in births):
                reasons.append("birth_date")
        else:
            reasons.append("date_missing")

    reasons = list(dict.fromkeys(reasons))
    return {
        "kind": kind,
        "page_number": cand.get("page_number"),
        "evidence": evidence[:500],
        "verification": "unverified" if reasons else "machine",
        "reasons": reasons,
        "fields": {k: (ws(v) if isinstance(v, str) else v) for k, v in fields.items()},
        "dates": dates,
        "derived": derived,
    }
