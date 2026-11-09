"""Rule-based lab facts from sidecar text (#603). No model call.

Two regular printout formats seen in real folders:

* **Bory** hospital-system printout, one section per line::

      Odber: 29.01.2026 12:24,
      Krvný obraz: Leukocyty [WBC] ...10.89 ▲  [10^9/l] {3.80 - 10.70 } ; Hemoglobín ...

* **NOÚ OKB** table (fixed columns, two or more spaces between them)::

      Odobrané: 13.03.2026 08:52   Prijaté: …
      CA 19-9 v sére        22294.3    U/ml      0.0 - 30.9     ****   /
      eGF CKD-EPI           1.92       ml/s      > 1.49         v norme

Every fact keeps the exact text it came from (``evidence``), and the value
digits are always taken from that text. ``[?]`` anywhere in the evidence, or
an unreliable page, makes the fact ``unverified``.
"""

from __future__ import annotations

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
    rf"^(?P<label>\S(?:.*?\S)?)\s{{2,}}(?P<val>{_NUM})\s+(?P<unit>[^\s\d][^\s]*)?"
    rf"(?:\s+(?P<lo>-?[\d.,]+)\s*-\s*(?P<hi>-?[\d.,]+)|\s+(?P<cmp>[<>]\s*[\d.,]+))?(?P<rest>.*)$"
)
_SECTION_PREFIX = re.compile(r"^[A-ZÁČĎÉÍĽĹŇÓÔŔŠŤÚÝŽa-záäčďéíľĺňóôŕšťúýž .]+:\s+")


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_label(s: str) -> str:
    s = _strip_accents(s).lower().strip()
    s = re.sub(r"^[^a-z0-9]+", "", s)  # "; S-Chloridy" → "s-chloridy"
    s = re.sub(r"\s+v\s+(sere|plazme|krvi|moci)$", "", s)
    s = re.sub(r"^[sbp]\s*-\s*", "", s)  # S-Glukóza, B-…, P-…
    s = re.sub(r"^u\s*-\s*", "u-", s)  # "U- Krv" == "U-Krv"
    s = s.replace(".", " ")  # "abs.počet" == "abs. počet"
    s = re.sub(r"\s*%", "%", s)  # "Neutrofilie %" == "Neutrofilie%"
    s = re.sub(r"\s+", " ", s)
    return s.strip(" :")


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
        return None


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
    if code_row and unit and unit.strip() == "%" and code_row.get("unit_canonical") != "%":
        # "Neutrofily 43.3 %" is the percentage, not the absolute count
        code_row = vocab.by_code.get(code_row["code"] + "_PCT")
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
        "verification": "unverified" if (unreliable or "[?]" in evidence) else "machine",
    }
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
    for page_no, text in enumerate(pages, start=1):
        if "Ref. medze" not in text and "Hodnota" not in text:
            continue
        when = None
        in_table = False
        for line in text.splitlines():
            if m := _OKB_ODOBRANE.search(line):
                when = _iso(m.group(1))
            if line.lstrip().startswith("Test") and "Hodnota" in line:
                in_table = True
                continue
            if not in_table or not line.strip():
                continue
            m = _OKB_ROW.match(line.strip())
            if not m:
                continue
            cmp = m.group("cmp")
            lo, hi = _num(m.group("lo")), _num(m.group("hi"))
            if cmp:
                bound = _num(cmp)
                lo, hi = (bound, None) if cmp.strip().startswith(">") else (None, bound)
            f = _fact(
                vocab,
                label=m.group("label"),
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


_MD_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_MD_HEADER = re.compile(r"\b(test|parameter|vy\u0161etrenie)\b.*\bhodnota\b", re.I)
_MD_VALUE = re.compile(rf"^\s*(?P<val>{_NUM}|\d\++|[A-Za-z]+)\s*(?:\((?P<note>[^)]*)\))?\s*$")


def parse_markdown_table(
    pages: list[str], vocab: Vocabulary, *, unreliable: bool = False
) -> list[dict]:
    """Lab tables written as markdown by older sidecar writers::

    Odobrané: 19.08.2026 08:21 · …
    | Test | Hodnota | Jednotky | Ref. medze |
    |---|---|---|---|
    | WBC | 3.84 | G/l | 3.5 - 10 |
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


def extract_lab_facts(pages: list[str], vocab: Vocabulary, *, unreliable: bool = False):
    """(facts, extractor id) — the first format that yields anything wins."""
    for name, fn in (
        ("rule:bory-v1", parse_bory),
        ("rule:okb-v1", parse_okb),
        ("rule:mdtable-v1", parse_markdown_table),
    ):
        facts = fn(pages, vocab, unreliable=unreliable)
        if facts:
            return facts, name
    return [], None
