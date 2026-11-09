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
    r"^\s*(?P<label>[^\[\]{}]+?)\s*(?:\[(?P<abbr>[^\]]{1,12})\])?\s*\.\.\.\s*"
    r"(?P<val>[<>]?\s*-?\d+(?:[.,]\d+)?|[A-Za-z0-9+/-]+)\s*(?P<arrow>[▲▼])?\s*"
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
    s = re.sub(r"\s+v\s+(sere|plazme|krvi|moci)$", "", s)
    s = re.sub(r"^[sbp]\s*-\s*", "", s)  # S-Glukóza, B-…, P-…
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .:")


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

    @classmethod
    def from_rows(cls, rows: list[dict]) -> Vocabulary:
        v = cls()
        for r in rows:
            for alias in [r["code"], *r.get("aliases", [])]:
                v.by_alias.setdefault(norm_label(alias), r)
        return v

    def match(self, label: str, abbr: str | None = None) -> dict | None:
        for key in (abbr, label):
            if key and (hit := self.by_alias.get(norm_label(key))):
                return hit
        return None


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


def _fact(
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
) -> dict:
    code_row = vocab.match(label, abbr)
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
    return {
        "category": (code_row or {}).get("category", "lab"),
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


def parse_bory(pages: list[str], vocab: Vocabulary, *, unreliable: bool = False) -> list[dict]:
    out: list[dict] = []
    when = None
    for page_no, text in enumerate(pages, start=1):
        for line in text.splitlines():
            if m := _BORY_ODBER.search(line):
                when = _iso(m.group(1))
                continue
            if "..." not in line:
                continue
            body = _SECTION_PREFIX.sub("", line.strip(), count=1)
            for chunk in body.split(" ; ") if " ; " in body else body.split(";"):
                m = _BORY_ITEM.match(chunk)
                if not m or not m.group("label").strip():
                    continue
                out.append(
                    _fact(
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
                )
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
            out.append(
                _fact(
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
            )
    return out


def extract_lab_facts(pages: list[str], vocab: Vocabulary, *, unreliable: bool = False):
    """(facts, extractor id) — the first format that yields anything wins."""
    for name, fn in (("rule:bory-v1", parse_bory), ("rule:okb-v1", parse_okb)):
        facts = fn(pages, vocab, unreliable=unreliable)
        if facts:
            return facts, name
    return [], None
