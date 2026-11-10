"""Rule-based lab facts from sidecar text (#603). No model call.

Two regular printout formats seen in real folders:

* **Bory** hospital-system printout, one section per line::

      Odber: 14.04.2027 09:10,
      Krvný obraz: Leukocyty [WBC] ...11.42 ▲  [10^9/l] {3.80 - 10.70 } ; Hemoglobín ...

* **NOÚ OKB** table (fixed columns, two or more spaces between them)::

      Odobrané: 21.05.2027 08:05   Prijaté: …
      CA 19-9 v sére        812.4      U/ml      0.0 - 30.9     ****   /
      eGF CKD-EPI           1.64       ml/s      > 1.49         v norme

Every fact keeps the exact text it came from (``evidence``), and the value
digits are always taken from that text. ``[?]`` anywhere in the evidence, or
an unreliable page, makes the fact ``unverified``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field

_DT_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2}))?")
_BORY_ODBER = re.compile(r"Odber:\s*(\d{1,2}\.\d{1,2}\.\d{4}(?:\s+\d{1,2}:\d{2})?)")
_OKB_ODOBRANE = re.compile(r"Odobran[ée]:\s*(\d{1,2}\.\d{1,2}\.\d{4}(?:\s+\d{1,2}:\d{2})?)")
_BORY_ITEM = re.compile(
    r"^\s*(?P<label>[^\W\d_][^\[\]{}]{0,60}?)\s*(?:\[(?P<abbr>[^\]]{1,12})\])?\s*"
    r"(?<!\.)\.\.\.(?!\.)\s*"
    r"(?P<val>\d\++(?=\s|$|\[)|[<>]?\s*-?\d+(?:[.,]\d+)?|[A-Za-z0-9+/-]+)\s*(?P<arrow>[▲▼])?\s*"
    r"(?:\[(?P<unit>[^\]]+)\])?\s*(?:\{\s*(?P<lo>-?[\d.,]+)\s*-\s*(?P<hi>-?[\d.,]+)\s*\})?"
)
_NUM = r"[<>]?\s*-?\d+(?:[.,]\d+)?"
_OKB_ROW = re.compile(
    # "(?:.*?\S)??" is lazy: a one-letter label ("K") must not swallow the value (#660)
    rf"^(?P<label>\S(?:.*?\S)??)\s{{2,}}(?P<val>{_NUM})\s+(?P<unit>[^\s\d][^\s]*)?"
    rf"(?:\s+(?P<lo>-?[\d.,]+)\s*-\s*(?P<hi>-?[\d.,]+)|\s+(?P<cmp>[<>]\s*[\d.,]+))?(?P<rest>.*)$"
)
# OCR'd sheets sometimes keep one space before the value: accepted only with a
# unit AND a printed range, so prose never matches (#660).
_OKB_ROW_LOOSE = re.compile(
    rf"^(?P<label>[^\W\d_][^\d]*?\S)\s+(?P<val>{_NUM})\s+(?P<unit>[^\s\d][^\s]*)"
    rf"\s+(?P<lo>-?[\d.,]+)\s*-\s*(?P<hi>-?[\d.,]+)(?P<rest>.*)$"
)
_TRAILING_UNSURE = re.compile(r"\s*/?\s*\[\?\]\s*$")
_LABEL_TAIL = re.compile(r"^[^\W\d_][^\d:;|]{0,24}$")
_SECTION_PREFIX = re.compile(r"^[A-ZÁČĎÉÍĽĹŇÓÔŔŠŤÚÝŽa-záäčďéíľĺňóôŕšťúýž .]+:\s+")


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_label(s: str) -> str:
    s = _strip_accents(s).lower().strip()
    s = re.sub(r"\(\s*str\.?\s*\d+\s*\)", "", s).strip()  # "(str. 2)" page suffix
    s = re.sub(r"^[^a-z0-9]+", "", s)  # "; S-Chloridy" → "s-chloridy"
    s = re.sub(r"\s+v\s+(sere|plazme|krvi|moci)$", "", s)
    s = re.sub(r"^[sbp]\s*-\s*", "", s)  # S-Glukóza, B-…, P-…
    s = re.sub(r"^u\s*-\s*", "u-", s)  # "U- Krv" == "U-Krv"
    s = s.replace(".", " ")  # "abs.počet" == "abs. počet"
    s = re.sub(r"\s*%", "%", s)  # "Neutrofilie %" == "Neutrofilie%"
    s = re.sub(r"\s+", " ", s).strip(" :")
    s = re.sub(r" s( v)?$", "", s)  # serum suffix: "KREA S", "OSM S V"
    return s


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.replace(",", ".").replace(" ", "").lstrip("<>")
    try:
        return float(s)
    except ValueError:
        return None


def _iso(dt: str | None) -> str | None:
    if not dt:
        return None
    m = _DT_RE.search(dt)
    if not m:
        return None
    d, mo, y, h, mi = m.groups()
    out = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return f"{out}T{int(h):02d}:{mi}" if h else out


def _unit_key(u: str | None) -> str:
    return (u or "").lower().replace("µ", "u").replace("μ", "u").replace(" ", "")


@dataclass
class Vocabulary:
    by_alias: dict[str, dict] = field(default_factory=dict)
    by_code: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: list[dict]) -> Vocabulary:
        v = cls()
        for r in rows:
            v.by_code[r["code"]] = r
            for alias in [r["code"], *r.get("aliases", [])]:
                v.by_alias.setdefault(norm_label(alias), r)
        return v

    def match(self, label: str, abbr: str | None = None) -> dict | None:
        for key in (abbr, label):
            if key and (hit := self.by_alias.get(norm_label(key))):
                return hit
        # "NEUT abs. 2,1" / "abs. Neutrofily": the absolute count of a differential
        key = norm_label(label)
        base = re.sub(r"^abs( pocet)? | abs( pocet)?$", "", key)
        if base != key and (hit := self.by_alias.get(base)):
            code = hit["code"].removesuffix("_PCT")
            if code in _DIFFERENTIAL:
                return self.by_code.get(code)
        return None

    def match_fuzzy(self, label: str) -> dict | None:
        """One-typo tolerance for OCR'd labels ("Neutrofilly"), #660 guardrails:
        edit distance 1, label of at least 8 characters, and every alias within
        that distance must belong to ONE code. Callers keep such facts
        unverified (reason ``label_fuzzy``)."""
        key = norm_label(label)
        if len(key) < 8:
            return None
        codes = {
            r["code"]
            for alias, r in self.by_alias.items()
            if len(alias) >= 8 and abs(len(alias) - len(key)) <= 1 and _within_one(alias, key)
        }
        return self.by_code.get(codes.pop()) if len(codes) == 1 else None


def _within_one(a: str, b: str) -> bool:
    """Levenshtein distance <= 1 (one substitution, insertion or deletion)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + 1 :] == b[i + 1 :] if len(a) == len(b) else a[i:] == b[i + 1 :]


# Lines that are never document data: reviewer/agent/README notes written into
# older sidecars, form placeholders, staff sign-offs, software versions.
_NOT_DATA = re.compile(
    r"^\s*(\*\*|>|#|NOTE\b|Note\b)|\(agent\)|README|Identical content|doctor is\b|"
    r"Tel\.|telef|build \d|v\u00fdsledky uvo\u013enil|uvo\u013enil\(a\)|izol\u00e1cia od|"
    r"Vysledok:|V\u00fdsledok:|\.{4,}|^\W*$",
    re.I,
)
_QUALITATIVE = re.compile(
    r"^(negat\w*|pozit\w*|neg|pos|stopy|\+{1,4}|[1-4]\+|norm\w*|nehodnoti\w*|\d+-\d+)$", re.I
)
_UNIT_HINT = re.compile(
    r"(/|\^|%|\bg\b|\bfl\b|\bpg\b|\bs\b|kat|mol|\barb\b|mosm|cm3|mmhg|\bkg\b|\bcm\b)",
    re.I,
)
_DOSING = re.compile(r"mg\s*/\s*m2|mg/m\u00b2|\bmg\b|\bml/h\b", re.I)


def _plausible(fact: dict, code_row: dict | None) -> bool:
    """A lab fact needs a known code, or a number with a lab-like unit, or a
    known qualitative result. Everything else is not a fact (#603 review)."""
    if _NOT_DATA.search(fact["evidence"]) or _NOT_DATA.search(fact["label"]):
        return False
    if len(fact["label"]) > 60 or not re.match(r"[^\W\d_]", fact["label"]):
        return False
    if code_row:
        return True
    if fact["value_num"] is not None:
        return bool(fact["unit"]) and bool(_UNIT_HINT.search(fact["unit"]))
    return bool(fact["value_text"] and _QUALITATIVE.match(fact["value_text"].strip()))


# Plausible canonical-unit ranges (#637 review): a value outside its range is
# never stored under the code (wrong unit or a percentage read as a count).
PLAUSIBLE = {
    "NEUT": (0, 50), "LYMPH": (0, 20), "MONO": (0, 5), "EOS": (0, 5), "BASO": (0, 5),
    "NEUT_PCT": (0, 100), "LYMPH_PCT": (0, 100), "MONO_PCT": (0, 100),
    "EOS_PCT": (0, 100), "BASO_PCT": (0, 100),
    "WBC": (0, 300), "RBC": (0, 10), "HGB": (20, 250), "HCT": (0, 1), "PLT": (0, 3000),
    "MCV": (40, 150), "MCH": (10, 60), "MCHC": (200, 450),
    "CREA": (5, 2000), "UREA": (0, 100), "GLU": (0, 60), "NA": (90, 200), "K": (1, 10),
    "CRP": (0, 1000), "ALB": (5, 70), "TP": (20, 120), "P": (0, 10), "CA": (0, 6),
}  # fmt: skip

# Generic reference ranges (canonical units) for values printed WITHOUT a range
# (#635). Deliberately permissive (the widest common Slovak lab ranges), never
# sex-dependent codes, and never applied when the lab printed its own range:
# such a flag carries metadata flag_source=generic.
GENERIC_REF = {
    "WBC": (3.5, 10.7), "NEUT": (1.7, 7.0), "PLT": (140, 420),
    "CEA": (None, 5.0), "CA19_9": (None, 37.0), "CRP": (None, 5.0),
}  # fmt: skip
# HGB depends on sex: used only when the patient's sex is known (#635 v10)
GENERIC_REF_BY_SEX = {
    "HGB": {"female": (120, 155), "male": (135, 175)},
}
_DIFFERENTIAL = {"NEUT", "LYMPH", "MONO", "EOS", "BASO"}
# labels that always mean the absolute count ("abs.počet NEUT", "NEUT#", "ANC")
_ABSOLUTE_LABEL = re.compile(r"abs|#|^anc$")


def _upper_from_text(ref: str | None) -> float | None:
    """Upper limit from a text reference with only "<" bounds (#635).

    Several bounds (e.g. non-smoker / smoker) → the lowest, so a value above
    the strictest limit is flagged rather than silently passing."""
    if not ref or ">" in ref:
        return None
    bounds = [_num(b) for b in re.findall(r"<\s*(\d+(?:[.,]\d+)?)", ref)]
    bounds = [b for b in bounds if b is not None]
    return min(bounds) if bounds else None


def _flag(value: float | None, lo: float | None, hi: float | None, arrow: str | None) -> str | None:
    if arrow == "▲":
        return "H"
    if arrow == "▼":
        return "L"
    if value is None:
        return None
    if lo is not None and value < lo:
        return "L"
    if hi is not None and value > hi:
        return "H"
    return None


def _fact(  # noqa: PLR0913
    vocab: Vocabulary,
    *,
    label: str,
    abbr: str | None,
    raw_value: str,
    unit: str | None,
    lo: float | None,
    hi: float | None,
    ref_text: str | None,
    arrow: str | None,
    when: str | None,
    page: int,
    evidence: str,
    unreliable: bool,
) -> dict | None:
    code_row = vocab.match(label, abbr)
    fuzzy = False
    if code_row is None and (code_row := vocab.match_fuzzy(label)) is not None:
        fuzzy = True
    if (
        code_row
        and code_row["code"] in _DIFFERENTIAL
        and not unit
        and not _ABSOLUTE_LABEL.search(norm_label(label))
    ):
        # "Neutrofily 53,4" without a unit is the percentage; the count is "abs.počet"
        code_row = vocab.by_code.get(code_row["code"] + "_PCT")
    if code_row and unit and unit.strip() == "%" and code_row.get("unit_canonical") != "%":
        # "Neutrofily 51.2 %" is the percentage, not the absolute count
        code_row = vocab.by_code.get(code_row["code"] + "_PCT")
    elif (
        code_row
        and unit
        and unit.strip() != "%"
        and code_row["code"].endswith("_PCT")
        and _UNIT_HINT.search(unit)
    ):
        # a count with a real unit ("10^9/l") is never the percentage code
        code_row = vocab.by_code.get(code_row["code"][: -len("_PCT")])
    value = _num(raw_value)
    numeric = value is not None and re.fullmatch(_NUM, raw_value.strip()) is not None
    unit_canonical = code_row["unit_canonical"] if code_row else None
    value_canonical = None
    if numeric and code_row:
        uk, ck = _unit_key(unit), _unit_key(unit_canonical)
        conv = {_unit_key(k): f for k, f in (code_row.get("unit_conversions") or {}).items()}
        if not uk or uk == ck:
            value_canonical = value
        elif uk in conv:
            value_canonical = round(value * float(conv[uk]), 6)
    if code_row and numeric and code_row["code"] in PLAUSIBLE:
        lo_p, hi_p = PLAUSIBLE[code_row["code"]]
        canon = value_canonical if value_canonical is not None else value
        if canon is None or not (lo_p <= canon <= hi_p):
            code_row, value_canonical, unit_canonical = None, None, None  # never under the code
            implausible = True
        else:
            implausible = False
    else:
        implausible = False
    unit_s = unit.strip() if unit else None
    if not code_row and unit_s and _DOSING.search(unit_s + " " + label):
        category = "medication"  # chemo dosing lines are not lab values
    else:
        category = (code_row or {}).get("category", "lab")
    fact = {
        "category": category,
        "code": code_row["code"] if code_row else None,
        "label": label.strip(),
        "value_num": value if numeric else None,
        "value_text": None if numeric else raw_value.strip(),
        "unit": unit.strip() if unit else None,
        "ref_low": lo,
        "ref_high": hi,
        "ref_text": ref_text,
        "flag": _flag(value if numeric else None, lo, hi, arrow),
        "value_canonical": value_canonical,
        "unit_canonical": unit_canonical if value_canonical is not None else None,
        "occurred_at": when,
        "page_number": page,
        "evidence": evidence.strip()[:300],
        "verification": "machine",
    }
    reasons = [
        r
        for r, hit in (
            ("unreliable_source", unreliable),
            ("ocr_uncertain", "[?]" in evidence),
            ("implausible", implausible),
            ("label_fuzzy", fuzzy and code_row is not None),
        )
        if hit
    ]
    if reasons:
        fact["verification"] = "unverified"
        fact["metadata"] = {"unverified_reasons": reasons}
    if arrow and lo is None and hi is None and fact["flag"]:
        fact["metadata"] = {**(fact.get("metadata") or {}), "flag_source": "arrow"}
    if (
        fact["code"] in GENERIC_REF
        and lo is None
        and hi is None
        and fact["flag"] is None
        and value_canonical is not None
    ):
        g_lo, g_hi = GENERIC_REF[fact["code"]]
        if flag := _flag(value_canonical, g_lo, g_hi, None):
            fact["flag"] = flag
            fact["metadata"] = {**(fact.get("metadata") or {}), "flag_source": "generic"}
    if category == "medication":
        return fact if not _NOT_DATA.search(evidence) else None
    return fact if _plausible(fact, code_row) else None


def parse_bory(pages: list[str], vocab: Vocabulary, *, unreliable: bool = False) -> list[dict]:
    out: list[dict] = []
    when = None
    for page_no, text in enumerate(pages, start=1):
        for line in text.splitlines():
            if m := _BORY_ODBER.search(line):
                when = _iso(m.group(1))
                continue
            if "..." not in line or _NOT_DATA.match(line):
                continue
            body = _SECTION_PREFIX.sub("", line.strip(), count=1)
            for chunk in body.split(" ; ") if " ; " in body else body.split(";"):
                m = _BORY_ITEM.match(chunk)
                if not m or not m.group("label").strip():
                    continue
                f = _fact(
                    vocab,
                    label=_SECTION_PREFIX.sub("", m.group("label")),
                    abbr=m.group("abbr"),
                    raw_value=m.group("val"),
                    unit=m.group("unit"),
                    lo=_num(m.group("lo")),
                    hi=_num(m.group("hi")),
                    ref_text=None,
                    arrow=m.group("arrow"),
                    when=when,
                    page=page_no,
                    evidence=chunk,
                    unreliable=unreliable,
                )
                if f:
                    out.append(f)
    return out


def parse_okb(pages: list[str], vocab: Vocabulary, *, unreliable: bool = False) -> list[dict]:
    out: list[dict] = []
    when = None
    in_table = False  # a table continues on the next page without its header (#660)
    for page_no, text in enumerate(pages, start=1):
        if not in_table and "Ref. medze" not in text and "Hodnota" not in text:
            continue
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            if m := _OKB_ODOBRANE.search(line):
                when = _iso(m.group(1))
            if line.lstrip().startswith("Test") and "Hodnota" in line:
                in_table = True
                continue
            if not in_table or not line.strip():
                continue
            if _REF_LINE.match(line) and out and out[-1]["page_number"] == page_no:
                _attach_ref_text(out[-1], line.split(":", 1)[1].strip())
                continue
            # a trailing "[?]" keeps the row (unverified via the evidence), #660
            row = _TRAILING_UNSURE.sub("", line.strip())
            m = _OKB_ROW.match(row) or _OKB_ROW_LOOSE.match(row)
            if not m:
                continue
            label = m.group("label")
            nxt = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            # a long label wrapped onto the next line ("… železa v" / "sére")
            if (
                vocab.match(label) is None
                and _LABEL_TAIL.match(nxt)
                and vocab.match(f"{label} {nxt}") is not None
            ):
                label = f"{label} {nxt}"
            cmp = m.groupdict().get("cmp")
            lo, hi = _num(m.group("lo")), _num(m.group("hi"))
            if cmp:
                bound = _num(cmp)
                lo, hi = (bound, None) if cmp.strip().startswith(">") else (None, bound)
            f = _fact(
                vocab,
                label=label,
                abbr=None,
                raw_value=m.group("val"),
                unit=m.group("unit"),
                lo=lo,
                hi=hi,
                ref_text=cmp.strip() if cmp else None,
                arrow=None,
                when=when,
                page=page_no,
                evidence=line,
                unreliable=unreliable,
            )
            if f:
                out.append(f)
    return out


_REF_LINE = re.compile(r"^\s*refer(?:en\w*)?\.?\s*medze\s*:", re.I)


def _attach_ref_text(fact: dict, ref: str) -> None:
    """A text reference printed under the row it belongs to (#635)."""
    if fact["ref_low"] is not None or fact["ref_high"] is not None:
        return
    fact["ref_text"] = ref[:120]
    hi = _upper_from_text(ref)
    if hi is not None:
        fact["ref_high"] = hi
        fact["flag"] = _flag(fact["value_num"], None, hi, None)  # the lab's own range wins
        (fact.get("metadata") or {}).pop("flag_source", None)


_MD_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_MD_HEADER = re.compile(r"\b(test|parameter|vy\u0161etrenie)\b.*\bhodnota\b", re.I)
_MD_VALUE = re.compile(rf"^\s*(?P<val>{_NUM}|\d\++|[A-Za-z]+)\s*(?:\((?P<note>[^)]*)\))?\s*$")


def parse_markdown_table(
    pages: list[str], vocab: Vocabulary, *, unreliable: bool = False
) -> list[dict]:
    """Lab tables written as markdown by older sidecar writers::

    Odobrané: 03.03.2027 07:15 · …
    | Test | Hodnota | Jednotky | Ref. medze |
    |---|---|---|---|
    | WBC | 4.21 | G/l | 3.5 - 10 |
    | HGB | 119 (znížené) | g/l | 120 - 156 |
    """
    out: list[dict] = []
    for page_no, text in enumerate(pages, start=1):
        when = None
        cols: list[str] | None = None
        for line in text.splitlines():
            if m := _OKB_ODOBRANE.search(line):
                when = _iso(m.group(1))
            row = _MD_ROW.match(line)
            if not row:
                cols = None if line.strip() else cols
                continue
            cells = [c.strip() for c in row.group(1).split("|")]
            if _MD_HEADER.search(" ".join(cells)):
                cols = [c.lower() for c in cells]
                continue
            if cols is None or all(set(c) <= set("-: ") for c in cells):
                continue
            label = cells[0]
            vm = _MD_VALUE.match(cells[1]) if len(cells) > 1 else None
            if not label or not vm:
                continue
            unit = cells[2] if len(cells) > 2 and cells[2] else None
            ref = cells[3] if len(cells) > 3 else ""
            lo = hi = None
            ref_text = None
            if rm := re.match(r"^\s*(-?[\d.,]+)\s*-\s*(-?[\d.,]+)\s*$", ref):
                lo, hi = _num(rm.group(1)), _num(rm.group(2))
            elif cm := re.match(r"^\s*([<>])\s*([\d.,]+)\s*$", ref):
                bound = _num(cm.group(2))
                lo, hi = (bound, None) if cm.group(1) == ">" else (None, bound)
                ref_text = ref.strip()
            elif ref.strip():
                ref_text = ref.strip()[:120]
                hi = _upper_from_text(ref_text)  # "nefajčiar <2,5; fajčiar <5"
            note = (vm.group("note") or "").lower()
            arrow = (
                "▲"
                if "zvýš" in note or "zvys" in note
                else ("▼" if "zníž" in note or "zniz" in note else None)
            )
            f = _fact(
                vocab,
                label=label,
                abbr=None,
                raw_value=vm.group("val"),
                unit=unit,
                lo=lo,
                hi=hi,
                ref_text=ref_text,
                arrow=arrow,
                when=when,
                page=page_no,
                evidence=line,
                unreliable=unreliable,
            )
            if f:
                out.append(f)
    return out


_INLINE_ITEM = re.compile(
    r"^\s*(?P<label>[^\W\d_][^;:]{0,40}?)\s+(?P<val>[<>]?\d+(?:[.,]\d+)?)\s*(?P<unit>[^\d\s;(][^\d;(]{0,15})?\s*$"
)
# "Neutrofily 53,4 (abs 2,10)": the percentage and the absolute count in one item
_INLINE_ABS = re.compile(
    r"^\s*(?P<label>[^\W\d_][^;:()]{0,40}?)\s+(?P<val>\d+(?:[.,]\d+)?)\s*%?\s*"
    r"\(\s*abs\.?\s*(?P<abs>\d+(?:[.,]\d+)?)\s*\)\s*$"
)
# items are separated by ";" or by ", " before the next label (decimal commas have no space)
_INLINE_SPLIT = re.compile(r";|,\s+(?=[^\W\d_])")
_INLINE_ARROW = {"↑": "▲", "↓": "▼", "▲": "▲", "▼": "▼"}
# vital signs ("TK 120/80, P 72, SpO2 98%, TT 36,6") are not lab results: "P" is the pulse
_VITALS = re.compile(r"\bTK\s*\d+\s*/\s*\d+|\bSpO2\b|\bTT\s*\d", re.I)
# study statistics in reference articles ("HR 1.42; 95% CI 1.1-1.8; P .003")
_STATS = re.compile(
    r"\b(HR|OR|RR)\s*[=:]?\s*\d|\b\d{2}\s*%\s*CI\b|\bCI\b|\bP\s*[=<>]|\bcohort\b", re.I
)
_DATE_RANGE_BEFORE = re.compile(r"(\d{1,2}\.|\d{4})\s?[–-]\s?$")


_SEG_DATE = re.compile(r"(?<![\d.])(\d{1,2})\.\s?(\d{1,2})\.(?:\s?(\d{4}))?(?![\d,])")


def _segment_date(segment: str) -> tuple[int, int, int | None, str | None] | None:
    """(day, month, year or None, range start) printed in the prefix of a ";"
    segment ("labs 9.9.: WBC …"). A range ("3.–4.9.2027", "14.7.2027-15.7.2027")
    is returned with its END; the caller dates the fact at the midpoint and
    keeps both ends in the metadata (#659)."""
    if ":" not in segment:
        return None
    prefix = segment.replace("*", "").rsplit(":", 1)[0]
    hits = list(_SEG_DATE.finditer(prefix))
    if not hits:
        return None
    hit = hits[-1]
    d, mo, y = (int(g) if g else None for g in hit.groups())
    if not (1 <= d <= 31 and 1 <= mo <= 12):
        return None
    before = prefix[: hit.start()]
    if not _DATE_RANGE_BEFORE.search(before):
        return d, mo, y, None
    if len(hits) > 1 and re.fullmatch(r"\s?[–-]\s?", prefix[hits[-2].end() : hit.start()]):
        d0, mo0, y0 = (int(g) if g else None for g in hits[-2].groups())  # "14.7.2027-15.7.2027"
    elif m := re.search(r"(\d{1,2})\.\s?[–-]\s?$", before):
        d0, mo0, y0 = int(m.group(1)), mo, y  # "3.–4.9.2027"
    else:
        return d, mo, y, "?"
    y0 = y0 or y
    start = f"{y0:04d}-{mo0:02d}-{d0:02d}" if y0 else f"{mo0:02d}-{d0:02d}"
    return d, mo, y, start


def _continues(prev: str, nxt: str) -> bool:
    """Is ``nxt`` the visual continuation of the result list in ``prev``?

    Only list lines (>= 2 ";" items) not ending a sentence are joined, and only
    when the next line carries on the list: it starts with the value of a split
    item, the previous line ends with a label that still lacks its value, or
    the next line is itself a ";" list continuing the same run (#660)."""
    p, n = prev.rstrip(), nxt.strip()
    if not n or p.count(";") < 2 or p.endswith((".", ":")):
        return False
    tail = p.rsplit(";", 1)[-1]
    label_without_value = bool(tail.strip()) and not re.search(r"\d", tail)
    return (
        bool(re.match(r"^[<>]?\s*\d", n))
        or label_without_value
        or (p.endswith(";") and n.count(";") >= 1 and not _LINE_DATE.search(n))
    )


def _join_wrapped(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if out and _continues(out[-1], line):
            out[-1] = out[-1].rstrip() + " " + line.strip()
        else:
            out.append(line)
    return out


def _inline_items(line: str) -> list[tuple]:
    """(label, value, unit, arrow, segment date) for every result item of a line."""
    out = []
    seg_date = None
    for segment in line.split(";"):
        seg_date = _segment_date(segment) or seg_date  # later items keep the last date
        parts = re.split(r",\s+(?=[^\W\d_])", segment)
        for item in parts:
            item = re.sub(r"^.*:\s*", "", item.replace("*", ""))  # "Lab 1.2.2027: **CEA …"
            arrow = next((_INLINE_ARROW[c] for c in item if c in _INLINE_ARROW), None)
            item = re.sub(r"[↑↓▲▼]", "", item).strip().rstrip(".").strip()
            if len(parts) == 1:  # "CA 19-9 812,4 (zakrúžkované)" in a ";" list
                item = re.sub(r"\s*\([^()\d,]*\)$", "", item)
            if a := _INLINE_ABS.match(item):
                out.append((a.group("label"), a.group("val"), None, None, seg_date))
                out.append((a.group("label") + " abs.", a.group("abs"), None, None, seg_date))
            elif m := _INLINE_ITEM.match(item):
                out.append((m.group("label"), m.group("val"), m.group("unit"), arrow, seg_date))
    return out


_LINE_DATE = re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})\b")


def _year_for(day: int, month: int, ref: str | None) -> int | None:
    """Year of a day-month date seen in a document dated ``ref``.

    A measurement cannot postdate its report: the same year when that gives a
    date on or before ``ref``; the year before when the month is at least six
    months later ("15.12." in a January report); otherwise None (a date just
    after the report is a print or control date, not a sample date)."""
    if not ref or not re.match(r"\d{4}-\d{2}-\d{2}", ref):
        return None
    year, ref_month, ref_day = int(ref[:4]), int(ref[5:7]), int(ref[8:10])
    if (month, day) <= (ref_month, ref_day):
        return year
    return year - 1 if month - ref_month >= 6 else None


# Block headers (#661, maintainer decision 2026-11-10): a lab block in a report
# is often introduced by a line that is only its date or date range
# ("14.7.2027 - 15.7.2027:", "Vykonané vyšetrenia: laboratórne 3.8.2027:") or by
# the sampling line ("Odobrané 3.8.2027 07:40 …"); its result lines follow,
# maybe after specimen labels ("sérum OKB"). Strict on purpose: anything else
# between the header and the values ends the block.
_HEADER_DATES = re.compile(
    r"^\s*(?:[^\d|:;]{0,40}:\s*)?[^\d|:;]{0,20}?"
    r"(?P<dates>\d{1,2}\.\s?\d{1,2}\.(?:\s?\d{4})?"
    r"(?:\s?[–-]\s?\d{1,2}\.(?:\s?\d{1,2}\.)?(?:\s?\d{4})?)?)\s*:\s*$"
)
_SAMPLED = re.compile(r"^\s*Odobran[éáa]\s+(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})\b", re.I)
_SPECIMEN = re.compile(r"^[^\d;|:]{1,30}$")  # "sérum OKB", "krv EDTA"
_NUMBERS_ONLY = re.compile(r"^[\d\s,.<>]+$")  # a wrapped value ("99,9")


def _block_header(line: str) -> tuple[int, int, int | None, str | None] | None:
    """(day, month, year or None, range start) when ``line`` opens a lab block."""
    text = line.replace("*", "").strip()
    if not text or text.startswith(("|", "#")):
        return None
    if m := _SAMPLED.match(text):
        d, mo, y = (int(g) for g in m.groups())
        return (d, mo, y, None) if 1 <= d <= 31 and 1 <= mo <= 12 else None
    if m := _HEADER_DATES.match(text):
        return _segment_date(m.group("dates") + ":")
    return None


def _keeps_block(line: str, is_results: bool) -> bool:
    """Can ``line`` sit between a block header and the block's values?"""
    text = line.replace("*", "").strip()
    return (
        not text
        or is_results
        or bool(_NUMBERS_ONLY.match(text))
        or (bool(_SPECIMEN.match(text)) and not text.startswith(("|", "#")))
    )


def _line_range_start(before: str, end_iso: str | None) -> str | None:
    """Start day of "5.–6.5.2027" (same month and year as the end)."""
    m = re.search(r"(\d{1,2})\.\s?[–-]\s?$", before)
    if not m or not end_iso:
        return None
    return f"{end_iso[:8]}{int(m.group(1)):02d}"


def _date_range_midpoint(fact: dict, meta: dict, start: str | None) -> None:
    """Date a range at its middle (maintainer decision, #659): the range itself
    is printed, so it is a dated value. ``date_range_start``/``_end`` keep the
    window for dedupe. An unknown start keeps the end and is marked open."""
    from datetime import date as _date

    end = fact.get("occurred_at")
    meta["date_range_end"] = end
    if not end or not start or start == "?":
        meta["date_range_open"] = True
        return
    if len(start) == 5:  # "MM-DD": the year of the end
        start = f"{end[:4]}-{start}"
    try:
        d0, d1 = _date.fromisoformat(start[:10]), _date.fromisoformat(end[:10])
    except ValueError:
        meta["date_range_open"] = True
        return
    if d0 > d1:
        meta["date_range_open"] = True
        return
    meta["date_range_start"] = d0.isoformat()
    fact["occurred_at"] = (d0 + (d1 - d0) / 2).isoformat()


def parse_inline_results(
    pages: list[str],
    vocab: Vocabulary,
    *,
    report_date: str | None = None,
    unreliable: bool = False,
) -> list[dict]:
    """Result lines quoted inside clinic reports (#635)::

        CEA S 38,2; CA 19-9 655,1; KREA S 61,0; NA v sére 139,0

    Verification (#659): a missing printed unit is fine (the code's canonical
    unit, ``unit_source=implied``); a printed date range is dated at its
    midpoint. Only a value without any date of its own (``date_source=report``)
    is unverified, with reason ``date_unknown``. At least two items must match known codes, so a
    sentence with numbers is never read as results.
    """
    out: list[dict] = []
    seen_keys: set = set()
    for page_no, text in enumerate(pages, start=1):
        prev = ""
        block = None  # (header date, header line) of the lab block we are in (#661)
        for line in _join_wrapped(text.splitlines()):
            skip = _NOT_DATA.search(line) or _VITALS.search(line) or _STATS.search(line)
            items = [] if skip else _inline_items(line)
            is_results = False
            if header := _block_header(line):
                block = (header, line.replace("*", "").strip()[:160])
            if len(_INLINE_SPLIT.split(line)) >= 3 and items:
                parsed = [(it, vocab.match(it[0]) or vocab.match_fuzzy(it[0])) for it in items]
                is_results = sum(1 for _, c in parsed if c) >= 2
                if is_results:
                    dm = _LINE_DATE.search(line) or _LINE_DATE.search(prev)
                    when = (
                        f"{int(dm.group(3)):04d}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}"
                        if dm
                        else report_date
                    )
                    src_line = line if _LINE_DATE.search(line) else prev
                    ranged = bool(dm and _DATE_RANGE_BEFORE.search(src_line[: dm.start()]))
                    for (label, val, unit, arrow, seg_date), code_row in parsed:
                        if not code_row:
                            continue  # only known codes from free text
                        item_when, date_source = when, ("printed" if dm else "report")
                        own_date, from_block = seg_date, False
                        if not own_date and not dm and block:
                            own_date, from_block = block[0], True  # the block's header date
                        if own_date:  # the segment's own date beats any other date in the line
                            d, mo, y, range_start = own_date
                            year = y or _year_for(d, mo, report_date or when)
                            if year:
                                item_when = f"{year:04d}-{mo:02d}-{d:02d}"
                                date_source = "printed" if y else "printed_no_year"
                                if range_start:
                                    date_source = "printed_range"
                        unit = (unit or "").strip() or None
                        printed_unit = bool(unit and _UNIT_HINT.search(unit))
                        f = _fact(
                            vocab,
                            label=label,
                            abbr=None,
                            raw_value=val,
                            unit=unit if printed_unit else None,
                            lo=None,
                            hi=None,
                            ref_text=None,
                            arrow=arrow,
                            when=item_when,
                            page=page_no,
                            evidence=line,
                            unreliable=unreliable,
                        )
                        if not f:
                            continue
                        meta = {**(f.get("metadata") or {}), "date_source": date_source}
                        if from_block and date_source != "report":
                            meta["date_evidence"] = block[1]  # the header line, verbatim
                            meta["date_from"] = "block_header"
                        start = own_date[3] if own_date else None
                        if ranged and not own_date:
                            meta["date_source"] = "printed_range"  # "5.–6.5.2027"
                            start = _line_range_start(src_line[: dm.start()], f["occurred_at"])
                        if meta["date_source"] == "printed_range":
                            _date_range_midpoint(f, meta, start)
                        if not printed_unit:
                            meta["unit_source"] = "implied"  # canonical unit of the code
                        reasons = list(meta.pop("unverified_reasons", []))
                        if meta["date_source"] == "report":
                            reasons.append("date_unknown")  # quoted without its own date
                        if meta.get("date_range_open"):
                            reasons.append("date_unknown")
                        if reasons:
                            meta["unverified_reasons"] = sorted(set(reasons))
                        f["verification"] = "unverified" if reasons else "machine"
                        f["metadata"] = meta
                        key = (f["code"], f["occurred_at"], f["value_num"], f["value_text"])
                        if key in seen_keys:
                            continue  # the same line quoted twice in one report
                        seen_keys.add(key)
                        out.append(f)
            if not _block_header(line) and not _keeps_block(line, is_results):
                block = None  # prose, a table or another dated line ends the block
            if line.strip():
                prev = line
    return out


def extract_lab_facts(
    pages: list[str],
    vocab: Vocabulary,
    *,
    unreliable: bool = False,
    report_date: str | None = None,
    inline: bool = False,
    sex: str | None = None,
):
    """(facts, extractor id) — the first format that yields anything wins."""
    for name, fn in (
        ("rule:bory-v1", parse_bory),
        ("rule:okb-v1", parse_okb),
        ("rule:mdtable-v1", parse_markdown_table),
    ):
        facts = fn(pages, vocab, unreliable=unreliable)
        if facts:
            return _sex_generic(facts, sex), name
    if inline:  # results quoted inside clinic reports
        facts = parse_inline_results(pages, vocab, report_date=report_date, unreliable=unreliable)
        if facts:
            return _sex_generic(facts, sex), "rule:inline-v1"
    return [], None


def _sex_generic(facts: list[dict], sex: str | None) -> list[dict]:
    """Sex-specific generic flags (HGB), only without a printed range or flag."""
    key = (sex or "").strip().lower()
    for f in facts:
        ranges = GENERIC_REF_BY_SEX.get(f.get("code") or "")
        if (
            not ranges
            or key not in ranges
            or f.get("flag")
            or f.get("ref_low") is not None
            or f.get("ref_high") is not None
            or f.get("value_canonical") is None
        ):
            continue
        lo, hi = ranges[key]
        if flag := _flag(f["value_canonical"], lo, hi, None):
            f["flag"] = flag
            f["metadata"] = {**(f.get("metadata") or {}), "flag_source": "generic"}
    return facts


def corroborations(rows: list[dict], *, window_days: int = 60) -> list[tuple[int, str, int]]:
    """Report-quoted values that are a quote of a real draw (#659).

    A fact whose only reason to be unverified is ``date_unknown`` (dated to its
    report) is a quote of draw D when D is in another document, 1–60 days
    earlier, has the same code with the same canonical value and unit, and at
    least two codes of the quoting report day equal D's values. Returns
    (fact id, draw day, id of the matching draw fact). Exact matches only: a
    different value is never re-dated."""
    from datetime import date as _date

    def meta(r: dict) -> dict:
        try:
            return json.loads(r.get("metadata_json") or "{}")
        except ValueError:
            return {}

    def day(r: dict) -> str:
        return (r.get("occurred_at") or "")[:10]

    def as_date(d: str):
        try:
            return _date.fromisoformat(d)
        except ValueError:
            return None

    draws: dict[tuple, dict] = {}
    for r in rows:
        if r["verification"] != "unverified" and r.get("value_canonical") is not None:
            draws.setdefault((r["source_document_id"], day(r)), {})[r["code"]] = (
                r["value_canonical"], r.get("unit_canonical"), r["id"],
            )  # fmt: skip
    quoted: dict[tuple, dict] = {}
    candidates = []
    for r in rows:
        if r["verification"] != "unverified" or r.get("value_canonical") is None:
            continue
        if meta(r).get("unverified_reasons") != ["date_unknown"]:
            continue
        quoted.setdefault((r["source_document_id"], day(r)), {})[r["code"]] = (
            r["value_canonical"], r.get("unit_canonical"),
        )  # fmt: skip
        candidates.append(r)
    out = []
    for r in candidates:
        d0 = as_date(day(r))
        own = quoted[(r["source_document_id"], day(r))]
        best = None
        for (doc, dday), values in draws.items():
            d1 = as_date(dday)
            if doc == r["source_document_id"] or not d0 or not d1:
                continue
            if not 0 < (d0 - d1).days <= window_days:
                continue
            hit = values.get(r["code"])
            if not hit or hit[:2] != (r["value_canonical"], r.get("unit_canonical")):
                continue
            same = sum(1 for c, v in own.items() if values.get(c, (None,))[:2] == v)
            if same >= 2 and (best is None or dday > best[1]):
                best = (r["id"], dday, hit[2])
        if best:
            out.append(best)
    return out


def unverified_metric(rows: list[dict]) -> dict:
    """Share of unverified coded values and the reasons (benchmark ≤ 20 %, #658)."""
    from collections import Counter

    total = len(rows)
    reasons: Counter = Counter()
    unverified = 0
    for r in rows:
        if r["verification"] != "unverified":
            continue
        unverified += 1
        try:
            m = json.loads(r.get("metadata_json") or "{}")
        except ValueError:
            m = {}
        for reason in m.get("unverified_reasons") or ["unspecified"]:
            reasons[reason] += 1
    return {
        "coded": total,
        "unverified": unverified,
        "unverified_pct": round(100 * unverified / total, 1) if total else 0.0,
        "reasons": dict(reasons.most_common()),
    }
