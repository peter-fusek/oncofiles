"""LLM-first extraction of the non-lab kinds (#726 item 4, #711 phase 2b).

Sibling of ``curated.llm_extract`` (lab values, #714): the LOCAL model reads
one page of stored sidecar text and lists, as printed, diagnoses, biomarker /
genomic results, treatment courses, cycles, medication doses, procedures
(surgery, biopsy, imaging, radiotherapy, endoscopy), CT lesion sizes and
planned dates — each with its verbatim evidence passage. It never decides
trust: every candidate goes through ``curated.verifier_clinical`` (generic,
no layout knowledge), which accepts it as ``machine`` or keeps it
``unverified`` with reason codes.

Nothing is invented: what a page does not print stays empty. The prompt says
"copy, never compute", items without evidence or without their one required
value are dropped here, and the verifier rejects anything whose evidence is
not on the page or whose key tokens are not inside the evidence.

* One call per page (text, not image), one call at a time (one LM Studio
  slot); must run inside ``cloud_guard.local_only()`` and only ever talks to
  ``curated.llm`` (LM Studio), never to a cloud client.
* Strict JSON instructions (no ``response_format`` by default, v2); the
  answer is parsed defensively. The call itself (settings, fallbacks,
  diagnostics) is the shared path in ``curated.llm_extract_call`` (#767).
  The worked examples (invented data, years 2027+) are built from
  ``EXAMPLES`` and must themselves verify as machine (tested).
* A truncated, empty or unparseable answer leaves the page NOT assessed —
  never "nothing found"; the write path stores nothing for such a document.

Bump ``EXTRACTOR_VERSION`` on ANY change to the prompt, the schema or the
normalisation: it is stored as ``extracted_by`` and in the device row.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable

from oncofiles import cloud_guard
from oncofiles.curated import llm
from oncofiles.curated import llm_extract_call as xcall
from oncofiles.curated.llm_extract import _text
from oncofiles.curated.llm_extract_call import AnswerError, PageResult
from oncofiles.database._facts import LLM_CLINICAL_FAMILY

# v2: no response_format, reasoning fallback; v3: shared call path, sampling
# switch, bounded answer budget and timeout (#767); v4: optional prefilled
# closed think block, timeout override (#770); v5: optional reasoning-off
# marker in the system message instead of /no_think (#770); v6: qwen sampling
# profiles follow the Qwen3.8 model card, prefill removed (#770); v7: the
# evidence must hold the printed date too (date on the line above → the
# evidence spans those lines; outside the evidence, i.e. more than two lines
# from the value → date null), dates copied as printed, one worked example
# for each case (#773).
EXTRACTOR_VERSION = "llm-clinical-v7"
EXTRACTOR_FAMILY = LLM_CLINICAL_FAMILY  # prefix of every version: re-extraction scope
METHOD = "stored_sidecar_text"

# answer key -> printed fields (each item also has "evidence")
KINDS: dict[str, tuple[str, ...]] = {
    "diagnoses": (
        "text_as_printed", "code_as_printed", "date_as_printed", "role", "site_as_printed",
        "stage_as_printed",
    ),
    "biomarkers": (
        "marker_as_printed", "variant_as_printed", "result_as_printed", "method_as_printed",
        "date_as_printed",
    ),
    "treatment_courses": (
        "regimen_as_printed", "line_as_printed", "intent_as_printed", "start_date_as_printed",
        "end_date_as_printed",
    ),
    "cycles": (
        "regimen_as_printed", "cycle_as_printed", "date_as_printed", "status",
        "reason_as_printed",
    ),
    "medications": (
        "drug_as_printed", "dose_as_printed", "unit_as_printed", "route_as_printed",
        "start_date_as_printed", "end_date_as_printed", "status", "cycle_as_printed",
    ),
    "procedures": ("type", "description_as_printed", "date_as_printed", "end_date_as_printed"),
    "lesions": (
        "lesion_as_printed", "size_as_printed", "unit_as_printed", "date_as_printed",
        "description_as_printed",
    ),
    "planned": ("what", "description_as_printed", "date_as_printed"),
}  # fmt: skip
ENUMS: dict[tuple[str, str], tuple[str, ...]] = {
    ("diagnoses", "role"): ("primary", "metastasis", "other"),
    ("cycles", "status"): ("administered", "postponed"),
    ("medications", "status"): ("administered", "prescribed", "mentioned"),
    ("procedures", "type"): ("surgery", "biopsy", "imaging", "radiotherapy", "endoscopy", "other"),
    ("planned", "what"): ("cycle", "imaging", "visit", "other"),
}
# an unknown enum value becomes this (None: left empty)
ENUM_FALLBACK = {("procedures", "type"): "other", ("planned", "what"): "other"}


def _required(kind: str, item: dict) -> bool:
    """The one value without which an item says nothing (else it is dropped)."""
    g = item.get
    return {
        "diagnoses": bool(g("text_as_printed")),
        "biomarkers": bool(g("marker_as_printed"))
        and bool(g("result_as_printed") or g("variant_as_printed")),
        "treatment_courses": bool(g("regimen_as_printed")),
        "cycles": bool(g("cycle_as_printed") or g("date_as_printed")),
        "medications": bool(g("drug_as_printed")),
        "procedures": bool(g("description_as_printed")),
        "lesions": bool(g("lesion_as_printed")) and bool(g("size_as_printed")),
        "planned": bool(g("description_as_printed")) and bool(g("date_as_printed")),
    }[kind]


def _field_schema(kind: str, field: str) -> dict:
    values = ENUMS.get((kind, field))
    if values:
        return {"type": ["string", "null"], "enum": [*values, None]}
    return {"type": ["string", "null"]}


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(KINDS),
    "properties": {
        kind: {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [*fields, "evidence"],
                "properties": {
                    **{f: _field_schema(kind, f) for f in fields},
                    "evidence": {"type": "string"},
                },
            },
        }
        for kind, fields in KINDS.items()
    },
}
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "page_clinical_facts", "strict": True, "schema": SCHEMA},
}


def _item(kind: str, **values) -> dict:
    return {**dict.fromkeys(KINDS[kind]), **values}


def _answer(**lists) -> dict:
    return {kind: lists.get(kind, []) for kind in KINDS}


# Worked examples. Invented data only (public repo rule): no real patient,
# report, value or date — years 2027 and later.
_EX1 = "\n".join([
    "Ambulantná správa, onkológia",
    "Dg.: C18.7 Zhubný nádor sigmoidea, pT3 pN1 M1, štádium IV, dg. 03/2027",
    "Metastázy v pečeni (S7, S8).",
    "Molekulová genetika: KRAS mutácia c.35G>A (p.G12D), NRAS wild-type, MSI: MSS (PCR).",
    "Liečba: 1. línia paliatívna chemoterapia mFOLFOX6 od 2.4.2027.",
    "CT 10.6.2027: v S7 ložisko 31 mm.",
    "Dnes podaný 5. cyklus: Oxaliplatina 85 mg/m2 i.v., 5-FU 2400 mg/m2 i.v. 46 h.",
    "Ďalší cyklus plánovaný 28.6.2027, kontrolné CT 20.7.2027.",
])  # fmt: skip
_EX1_LINES = _EX1.splitlines()
_EX2 = "\n".join([
    "CT hrudníka, brucha a panvy zo dňa 12.7.2027",
    "Pečeň: v S7 hypodenzné ložisko 23 x 18 mm (pri CT 3.4.2027 27 x 20 mm), v S8 ložisko 9 mm.",
    "Záver: parciálna regresia metastáz v pečeni.",
    "Kontrola na onkológii 2.8.2027.",
])  # fmt: skip
_EX2_LINES = _EX2.splitlines()
_EX3 = "\n".join([
    "Prepúšťacia správa",
    "Dg.: C20 Adenokarcinóm rekta, ypT2 N0 (0/14) M0",
    "Neoadjuvantná rádioterapia 50,4 Gy/28 fr. 10.6.2027 - 19.7.2027, súbežne Kapecitabín "
    "825 mg/m2 p.o. 2x denne.",
    "Operácia 5.9.2027: nízka predná resekcia rekta (LAR).",
    "Histológia: adenokarcinóm G2, MMR: pMMR (IHC), HER2 negatívny.",
])  # fmt: skip
_EX3_LINES = _EX3.splitlines()
_EX4 = "\n".join([
    "Cyklus 7 FOLFIRI odložený pre neutropéniu, nový termín 18.10.2027.",
    "Rp.: Dexametazón 8 mg tbl. p.o. 1-0-0, 3 dni po chemoterapii.",
])  # fmt: skip
_EX4_LINES = _EX4.splitlines()
_EX5 = "\n".join([
    "Imunohistochémia zo dňa 4.2.2028:",
    "HER2 negatívny.",
    "CT 14.3.2028: pľúca bez ložísk.",
    "Pečeň bez zmeny.",
    "Slezina bez zmeny.",
    "Nadoblička vpravo: uzol 12 mm.",
])  # fmt: skip
_EX5_LINES = _EX5.splitlines()
_EX6 = "Pacientka bez ťažkostí, kontrola o 3 mesiace."

EXAMPLES: list[tuple[str, dict]] = [
    (_EX1, _answer(
        diagnoses=[
            _item("diagnoses", text_as_printed="Zhubný nádor sigmoidea", code_as_printed="C18.7",
                  date_as_printed="03/2027", role="primary", site_as_printed="sigmoidea",
                  stage_as_printed="pT3 pN1 M1, štádium IV", evidence=_EX1_LINES[1]),
            _item("diagnoses", text_as_printed="Metastázy v pečeni (S7, S8)", role="metastasis",
                  site_as_printed="pečeni", evidence=_EX1_LINES[2]),
        ],
        biomarkers=[
            _item("biomarkers", marker_as_printed="KRAS", variant_as_printed="c.35G>A (p.G12D)",
                  result_as_printed="mutácia", evidence=_EX1_LINES[3]),
            _item("biomarkers", marker_as_printed="NRAS", result_as_printed="wild-type",
                  evidence=_EX1_LINES[3]),
            _item("biomarkers", marker_as_printed="MSI", result_as_printed="MSS",
                  method_as_printed="PCR", evidence=_EX1_LINES[3]),
        ],
        treatment_courses=[
            _item("treatment_courses", regimen_as_printed="mFOLFOX6", line_as_printed="1. línia",
                  intent_as_printed="paliatívna", start_date_as_printed="2.4.2027",
                  evidence=_EX1_LINES[4]),
        ],
        cycles=[
            _item("cycles", cycle_as_printed="5. cyklus", status="administered",
                  evidence=_EX1_LINES[6]),
        ],
        medications=[
            _item("medications", drug_as_printed="Oxaliplatina", dose_as_printed="85",
                  unit_as_printed="mg/m2", route_as_printed="i.v.", status="administered",
                  cycle_as_printed="5. cyklus", evidence=_EX1_LINES[6]),
            _item("medications", drug_as_printed="5-FU", dose_as_printed="2400",
                  unit_as_printed="mg/m2", route_as_printed="i.v.", status="administered",
                  cycle_as_printed="5. cyklus", evidence=_EX1_LINES[6]),
        ],
        procedures=[
            _item("procedures", type="imaging", description_as_printed="CT",
                  date_as_printed="10.6.2027", evidence=_EX1_LINES[5]),
        ],
        lesions=[
            _item("lesions", lesion_as_printed="S7", size_as_printed="31", unit_as_printed="mm",
                  date_as_printed="10.6.2027", description_as_printed="ložisko",
                  evidence=_EX1_LINES[5]),
        ],
        planned=[
            _item("planned", what="cycle", description_as_printed="Ďalší cyklus plánovaný",
                  date_as_printed="28.6.2027", evidence=_EX1_LINES[7]),
            _item("planned", what="imaging", description_as_printed="kontrolné CT",
                  date_as_printed="20.7.2027", evidence=_EX1_LINES[7]),
        ],
    )),
    (_EX2, _answer(
        procedures=[
            _item("procedures", type="imaging",
                  description_as_printed="CT hrudníka, brucha a panvy",
                  date_as_printed="12.7.2027", evidence=_EX2_LINES[0]),
        ],
        lesions=[
            _item("lesions", lesion_as_printed="S7", size_as_printed="23 x 18",
                  unit_as_printed="mm", description_as_printed="hypodenzné ložisko",
                  evidence=_EX2_LINES[1]),
            _item("lesions", lesion_as_printed="S7", size_as_printed="27 x 20",
                  unit_as_printed="mm", date_as_printed="3.4.2027",
                  description_as_printed="hypodenzné ložisko", evidence=_EX2_LINES[1]),
            _item("lesions", lesion_as_printed="S8", size_as_printed="9", unit_as_printed="mm",
                  description_as_printed="ložisko", evidence=_EX2_LINES[1]),
        ],
        planned=[
            _item("planned", what="visit", description_as_printed="Kontrola na onkológii",
                  date_as_printed="2.8.2027", evidence=_EX2_LINES[3]),
        ],
    )),
    (_EX3, _answer(
        diagnoses=[
            _item("diagnoses", text_as_printed="Adenokarcinóm rekta", code_as_printed="C20",
                  role="primary", site_as_printed="rekta", stage_as_printed="ypT2 N0 (0/14) M0",
                  evidence=_EX3_LINES[1]),
        ],
        biomarkers=[
            _item("biomarkers", marker_as_printed="MMR", result_as_printed="pMMR",
                  method_as_printed="IHC", evidence=_EX3_LINES[4]),
            _item("biomarkers", marker_as_printed="HER2", result_as_printed="negatívny",
                  evidence=_EX3_LINES[4]),
        ],
        medications=[
            _item("medications", drug_as_printed="Kapecitabín", dose_as_printed="825",
                  unit_as_printed="mg/m2", route_as_printed="p.o.", status="administered",
                  evidence=_EX3_LINES[2]),
        ],
        procedures=[
            _item("procedures", type="radiotherapy",
                  description_as_printed="Neoadjuvantná rádioterapia 50,4 Gy/28 fr.",
                  date_as_printed="10.6.2027", end_date_as_printed="19.7.2027",
                  evidence=_EX3_LINES[2]),
            _item("procedures", type="surgery",
                  description_as_printed="nízka predná resekcia rekta (LAR)",
                  date_as_printed="5.9.2027", evidence=_EX3_LINES[3]),
        ],
    )),
    (_EX4, _answer(
        treatment_courses=[
            _item("treatment_courses", regimen_as_printed="FOLFIRI", evidence=_EX4_LINES[0]),
        ],
        cycles=[
            _item("cycles", regimen_as_printed="FOLFIRI", cycle_as_printed="Cyklus 7",
                  date_as_printed="18.10.2027", status="postponed",
                  reason_as_printed="neutropéniu", evidence=_EX4_LINES[0]),
        ],
        medications=[
            _item("medications", drug_as_printed="Dexametazón", dose_as_printed="8",
                  unit_as_printed="mg", route_as_printed="p.o.", status="prescribed",
                  evidence=_EX4_LINES[1]),
        ],
    )),
    (_EX5, _answer(  # date on the line above → both lines; date 3 lines away → null
        biomarkers=[
            _item("biomarkers", marker_as_printed="HER2", result_as_printed="negatívny",
                  date_as_printed="4.2.2028", evidence="\n".join(_EX5_LINES[:2])),
        ],
        procedures=[
            _item("procedures", type="imaging", description_as_printed="CT",
                  date_as_printed="14.3.2028", evidence=_EX5_LINES[2]),
        ],
        lesions=[
            _item("lesions", lesion_as_printed="Nadoblička vpravo", size_as_printed="12",
                  unit_as_printed="mm", description_as_printed="uzol", evidence=_EX5_LINES[5]),
        ],
    )),
    (_EX6, _answer()),
]  # fmt: skip

_INSTRUCTIONS = """You copy clinical facts from ONE page of a medical document: diagnoses, \
biomarker and genetic test results, treatment courses (regimens), chemotherapy cycles, \
medication doses, procedures (surgery, biopsy, imaging, radiotherapy, endoscopy), lesion sizes \
measured on imaging, and planned dates. The page is OCR text, mostly Slovak or Czech, \
sometimes English.

Answer with ONLY a JSON object with these eight lists, each possibly empty: "diagnoses", \
"biomarkers", "treatment_courses", "cycles", "medications", "procedures", "lesions", "planned". \
Every item has "evidence": the shortest passage of the page that holds ALL of the item's \
values, its dates included (one line, or at most three consecutive lines when a date is printed \
on a line above or below the value), copied character for character. Every other field, dates \
too, is copied exactly as printed (never reformatted), or null when it is not printed.

- diagnoses: "text_as_printed" (the diagnosis words), "code_as_printed" (ICD-10 / MKCH code, \
only if printed), "date_as_printed" (date of diagnosis, only if printed), "role" ("primary" for \
the main tumour, "metastasis" for a secondary site, else "other"), "site_as_printed", \
"stage_as_printed" (TNM or stage exactly as printed).
- biomarkers (tissue or genetic tests): "marker_as_printed" (gene or marker such as KRAS, NRAS, \
BRAF, MSI, MMR, HER2, PD-L1), "variant_as_printed" (e.g. HGVS), "result_as_printed" (mutácia, \
wild-type, negatívny, MSS, pMMR, ...), "method_as_printed" (PCR, NGS, IHC, ...), \
"date_as_printed".
- treatment_courses: "regimen_as_printed" (each regimen once), "line_as_printed", \
"intent_as_printed", "start_date_as_printed", "end_date_as_printed".
- cycles: "regimen_as_printed", "cycle_as_printed" (e.g. "5. cyklus", "C3"), \
"date_as_printed" (the day it was given, or for a postponed cycle its new date), "status" \
("administered" or "postponed"), "reason_as_printed" (why it was postponed).
- medications: "drug_as_printed", "dose_as_printed" (the number only), "unit_as_printed" \
(mg, mg/m2, mg/kg, ...), "route_as_printed" (i.v., p.o., ...), "start_date_as_printed", \
"end_date_as_printed", "status" ("administered", "prescribed" or "mentioned"), \
"cycle_as_printed".
- procedures: "type" ("surgery", "biopsy", "imaging", "radiotherapy", "endoscopy" or \
"other"), "description_as_printed", "date_as_printed", "end_date_as_printed".
- lesions: "lesion_as_printed" (lesion label or site), "size_as_printed" (the number, or \
"23 x 18", exactly as printed), "unit_as_printed" ("mm" or "cm"), "date_as_printed" (date of the \
examination the size belongs to; null when it belongs to the examination this page reports \
and that date is not inside the evidence), "description_as_printed".
- planned: "what" ("cycle", "imaging", "visit" or "other"), "description_as_printed", \
"date_as_printed" (the planned date; an item without a printed date is left out).

Rules:
1. Copy, never compute or infer. Never add a code, date, dose, line, stage or result that is \
not printed; never expand an abbreviation; never convert units.
2. A date is given only when it is inside the item's evidence. A date printed outside the \
item's evidence (more than two lines from the value) stays null (example 5: the lesion is three \
lines below 14.3.2028, so its date is null).
3. Skip laboratory values and blood tumour markers (they are read elsewhere), birth dates, \
names, identifiers, addresses and phone numbers.
4. A postponed cycle with its new date goes to "cycles" only, not to "planned".
5. A regimen named on the page is listed once in "treatment_courses", also when only one of \
its cycles is mentioned.
6. A page without such facts gives eight empty lists.
"""


def _build_prompt() -> str:
    parts = [_INSTRUCTIONS]
    for n, (page, answer) in enumerate(EXAMPLES, 1):
        parts.append(
            f"\nExample {n}. Page:\n{page}\nAnswer:\n{json.dumps(answer, ensure_ascii=False)}\n"
        )
    return "".join(parts)


SYSTEM_PROMPT = _build_prompt()


def parse_answer(text: str) -> dict:
    """The kind lists of a model answer. Tolerates reasoning blocks, ```json
    fences and text around the JSON. Raises ``LocalLLMError`` when none."""
    text = llm.strip_think(text or "")
    blobs = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)]
    blobs.append(text)
    decoder = json.JSONDecoder()
    for blob in blobs:
        for i, ch in enumerate(blob):
            if ch != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(blob[i:])
            except ValueError:
                continue
            if isinstance(obj, dict) and any(isinstance(obj.get(k), list) for k in KINDS):
                return {k: obj.get(k) if isinstance(obj.get(k), list) else [] for k in KINDS}
    raise AnswerError("no_json", "local LLM answer has no clinical facts JSON")


def _valid_answer(obj) -> bool:
    return (
        isinstance(obj, dict)
        and set(obj) == set(KINDS)
        and all(isinstance(obj[k], list) for k in KINDS)
    )


def parse_reasoning_channel(text: str) -> dict:
    """The kind lists from ``reasoning_content`` — accepted ONLY when the
    channel holds exactly one complete top-level JSON value carrying exactly
    the schema's kind lists. A draft plus a final answer, a partial object or
    a different shape is ambiguous: ``AnswerError("reasoning_ambiguous")``,
    the page is not assessed (same rule as the lab extractor, #714)."""
    body = llm.strip_think(text or "")
    decoder = json.JSONDecoder()
    values, i = [], 0
    while i < len(body):
        if body[i] not in "{[":
            i += 1
            continue
        try:
            obj, i = decoder.raw_decode(body, i)
        except ValueError:
            raise AnswerError("reasoning_ambiguous", "reasoning channel: partial JSON") from None
        values.append(obj)
    if len(values) != 1:
        msg = f"reasoning channel: {len(values)} JSON values, need exactly one"
        raise AnswerError("reasoning_ambiguous", msg)
    if not _valid_answer(values[0]):
        raise AnswerError("reasoning_ambiguous", "reasoning channel: JSON fails the schema")
    return {k: values[0][k] for k in KINDS}


def normalize_item(kind: str, raw) -> tuple[dict | None, str | None]:
    """(candidate, None) or (None, drop reason). Never invents a field."""
    if not isinstance(raw, dict):
        return None, "not_an_object"
    evidence = _text(raw.get("evidence"))
    if not evidence:
        return None, "no_evidence"
    item: dict = {"kind": kind}
    for f in KINDS[kind]:
        value = _text(raw.get(f))
        if (kind, f) in ENUMS and value is not None:
            value = value.lower()
            if value not in ENUMS[(kind, f)]:
                value = ENUM_FALLBACK.get((kind, f))
        item[f] = value
    for (k, f), fallback in ENUM_FALLBACK.items():  # procedure type / planned what: never empty
        if k == kind and item.get(f) is None:
            item[f] = fallback
    item["evidence"] = evidence
    if not _required(kind, item):
        return None, "missing_required"
    return item, None


def _structured_output() -> bool:
    from oncofiles import config

    return config.LLM_CLINICAL_STRUCTURED_OUTPUT


def extract_page(
    text: str,
    *,
    page_number: int,
    chat: Callable[..., str] | None = None,
    cfg: xcall.CallSettings | None = None,
) -> PageResult:
    """Blocking. Candidate items of one page (not yet verified).
    ``cfg`` (from ``llm_extract_call.settings``) defaults to the env settings."""
    if not cloud_guard.local_only_job():
        raise cloud_guard.CloudForbiddenError("llm_extract_clinical must run inside local_only()")
    res = PageResult(page_number=page_number)
    if not (text or "").strip():
        return res
    lists = xcall.call_and_parse(
        res, system=SYSTEM_PROMPT, user=f"Page {page_number}:\n{text}",
        response_format=RESPONSE_FORMAT if _structured_output() else None,
        parse_content=parse_answer, parse_reasoning=parse_reasoning_channel,
        cfg=cfg or xcall.settings(), chat=chat, label="clinical extract",
    )  # fmt: skip
    if lists is None:
        return res
    for kind, raws in lists.items():
        for raw in raws:
            cand, why = normalize_item(kind, raw)
            if cand is None:
                res.dropped[f"{kind}:{why}"] += 1
            else:
                cand["page_number"] = page_number
                cand["answer_source"] = res.answer_source
                res.candidates.append(cand)
    return res


def summarize(items: list[dict]) -> tuple[dict, dict]:
    """Counts per kind (machine / unverified) and unverified reasons — no values."""
    counts: dict[str, Counter] = {k: Counter() for k in KINDS}
    reasons: Counter = Counter()
    for it in items:
        counts[it["kind"]][it["verification"]] += 1
        reasons.update(it["reasons"])
    return (
        {k: {"machine": c["machine"], "unverified": c["unverified"]} for k, c in counts.items()},
        dict(sorted(reasons.items())),
    )


def extract_document(
    pages: list[tuple[int, str]],
    *,
    vocab,
    report_date: str | None = None,
    birth_dates=(),
    source_reason: str | None = None,
    chat: Callable[..., str] | None = None,
    stop: Callable[[], bool] | None = None,
    cfg: xcall.CallSettings | None = None,
) -> dict:
    """Blocking. Extract + verify every page, one model call at a time.
    Returns verified items and counts; nothing is stored."""
    from oncofiles.curated import verifier_clinical

    cfg = cfg or xcall.settings()
    results: list[PageResult] = []
    page_diag: dict[str, dict] = {}

    text_of = dict(pages)
    items: list[dict] = []
    dropped: Counter = Counter()
    errors = truncated = 0
    seconds = 0.0
    not_assessed: list[int] = []
    finish: dict[str, str | None] = {}
    error_kinds: Counter = Counter()
    sources: Counter = Counter()
    for number, text in pages:
        if stop and stop():
            not_assessed.append(number)
            continue
        pr = extract_page(text, page_number=number, chat=chat, cfg=cfg)
        results.append(pr)
        page_diag[str(number)] = pr.diagnostics()
        seconds += pr.seconds
        dropped.update(pr.dropped)
        finish[str(number)] = pr.finish_reason
        truncated += pr.truncated
        if not pr.assessed:
            if pr.error is not None:
                errors += 1
                error_kinds[pr.error_kind or "other"] += 1
            not_assessed.append(number)
            continue
        if pr.answer_source:
            sources[pr.answer_source] += 1
        for cand in pr.candidates:
            items.append(
                verifier_clinical.verify_item(
                    cand, page_text=text_of[number], vocab=vocab, report_date=report_date,
                    birth_dates=birth_dates, source_reason=source_reason,
                )
            )  # fmt: skip
    counts, reasons = summarize(items)
    dob_known = bool(birth_dates)
    return {
        "items": items, "method": METHOD, "pages": len(pages),
        "pages_used": len(pages) - len(not_assessed), "pages_not_assessed": not_assessed,
        "pages_truncated": truncated, "page_errors": errors, "finish_reasons": finish,
        "error_kinds": dict(sorted(error_kinds.items())),
        "answer_sources": dict(sorted(sources.items())),
        "page_diagnostics": page_diag, "warnings": xcall.warnings(results),
        "dob_known": dob_known, "birth_date_guard": "active" if dob_known else "inactive_no_dob",
        "counts": counts, "unverified_reasons": reasons, "dropped": dict(sorted(dropped.items())),
        "seconds": round(seconds, 1),
    }  # fmt: skip
