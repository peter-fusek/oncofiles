"""LLM-first fact extraction (#714), phase 2a: the local model reads one page.

Decision 2026-11-11: extraction relies on the LOCAL model, not on per-layout
Python rules. This module only asks the model what is printed; it never
decides trust. Every candidate goes through ``curated.verifier`` (generic,
no layout knowledge), which accepts it as ``machine`` or keeps it
``unverified`` with reason codes.

* One call per page of sidecar text (text, not image: cheap).
* Strict JSON (a JSON schema is sent as ``response_format``; the answer is
  still parsed defensively: code fences, reasoning, trailing text).
* Prompt rules: copy, never compute; unknown labels as printed; never a value
  without its verbatim evidence line. Items without evidence are dropped here.
* Must run inside ``cloud_guard.local_only()``; it only ever talks to
  ``curated.llm`` (LM Studio), never to a cloud client.

Phase 2a runs in SHADOW mode only (``curated_llm_extract_preview``): results
are compared with the stored rule facts as keys and counts, nothing is written.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from oncofiles import cloud_guard
from oncofiles.curated import llm

# Bump on ANY change to the prompt, the schema or the item normalisation.
EXTRACTOR_VERSION = "llm-extract-v1"

DATE_ROLES = ("sample", "report", "other")
_COMPARATORS = ("<=", ">=", "<", ">")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "label_as_printed",
                    "value",
                    "comparator",
                    "unit_as_printed",
                    "ref_range_as_printed",
                    "flag_as_printed",
                    "date_as_printed",
                    "date_role",
                    "evidence",
                ],  # fmt: skip
                "properties": {
                    "label_as_printed": {"type": "string"},
                    "value": {"type": "string"},
                    "comparator": {"type": ["string", "null"]},
                    "unit_as_printed": {"type": ["string", "null"]},
                    "ref_range_as_printed": {"type": ["string", "null"]},
                    "flag_as_printed": {"type": ["string", "null"]},
                    "date_as_printed": {"type": ["string", "null"]},
                    "date_role": {"type": ["string", "null"], "enum": [*DATE_ROLES, None]},
                    "evidence": {"type": "string"},
                },
            },
        }
    },
}
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "page_facts", "strict": True, "schema": SCHEMA},
}

# Worked examples use invented values and dates (public repo rule: no real data).
SYSTEM_PROMPT = """You copy measured results (laboratory values, tumour markers, other \
measurements) from ONE page of a medical document. The page is OCR text, mostly Slovak \
or Czech, sometimes English.

Answer with ONLY a JSON object {"facts": [...]}. One item per result:
- "label_as_printed": the test name exactly as printed, including prefixes such as \
"S-", "B-", "U-", abbreviations and brackets.
- "value": the result exactly as printed, as a string ("0,45", "12", "negat.").
- "comparator": "<", ">", "<=" or ">=" when printed in front of the value, else null.
- "unit_as_printed": the unit exactly as printed, or null when none is printed.
- "ref_range_as_printed": the reference range exactly as printed, or null.
- "flag_as_printed": the abnormality mark exactly as printed (H, L, ↑, ↓, ▲, ▼, *), or null.
- "date_as_printed": the date this result belongs to, exactly as printed (prefer the \
sample / collection date, else the report date), or null when no date on the page applies.
- "date_role": "sample" (odber, odobrané, collected), "report" (vydané, report, \
visit or exam date) or "other".
- "evidence": the ONE line of the page that holds this result, copied character for character.

Rules:
1. Copy, never compute. Every value, unit, range and date must be on the page exactly as \
you give it. Never convert units, round, add, average or derive anything.
2. Unknown or unusual test names are returned exactly as printed. Never rename, \
translate or "correct" a label.
3. Never return a value without its verbatim evidence line. If you cannot copy the \
line, leave the result out.
4. A line with several results gives several items, each with the same evidence line.
5. Skip identifiers, birth dates, phone numbers, addresses, medicine doses, page \
numbers and dates that stand alone.
6. A page without results gives {"facts": []}.

Example 1. Page:
Odber: 05.03.2027 07:40
S-Glukóza   5,83   mmol/l   3,9 - 5,6   H
Answer:
{"facts": [{"label_as_printed": "S-Glukóza", "value": "5,83", "comparator": null, \
"unit_as_printed": "mmol/l", "ref_range_as_printed": "3,9 - 5,6", "flag_as_printed": "H", \
"date_as_printed": "05.03.2027 07:40", "date_role": "sample", \
"evidence": "S-Glukóza   5,83   mmol/l   3,9 - 5,6   H"}]}

Example 2. Page:
Kontrola 12.4.2028. Lab.: CRP 17,3 mg/l, Leukocyty 9,1.
Answer:
{"facts": [{"label_as_printed": "CRP", "value": "17,3", "comparator": null, \
"unit_as_printed": "mg/l", "ref_range_as_printed": null, "flag_as_printed": null, \
"date_as_printed": "12.4.2028", "date_role": "report", \
"evidence": "Kontrola 12.4.2028. Lab.: CRP 17,3 mg/l, Leukocyty 9,1."}, \
{"label_as_printed": "Leukocyty", "value": "9,1", "comparator": null, \
"unit_as_printed": null, "ref_range_as_printed": null, "flag_as_printed": null, \
"date_as_printed": "12.4.2028", "date_role": "report", \
"evidence": "Kontrola 12.4.2028. Lab.: CRP 17,3 mg/l, Leukocyty 9,1."}]}

Example 3. Page:
Vydané: 2.2.2029
CEA        <0,5 ng/ml
HBsAg      negat.
Answer:
{"facts": [{"label_as_printed": "CEA", "value": "0,5", "comparator": "<", \
"unit_as_printed": "ng/ml", "ref_range_as_printed": null, "flag_as_printed": null, \
"date_as_printed": "2.2.2029", "date_role": "report", "evidence": "CEA        <0,5 ng/ml"}, \
{"label_as_printed": "HBsAg", "value": "negat.", "comparator": null, \
"unit_as_printed": null, "ref_range_as_printed": null, "flag_as_printed": null, \
"date_as_printed": "2.2.2029", "date_role": "report", "evidence": "HBsAg      negat."}]}

Example 4. Page (an unknown test stays as printed):
Odobraté 30.9.2027
Q-Faktor X7   2,22   arb.j.
Answer:
{"facts": [{"label_as_printed": "Q-Faktor X7", "value": "2,22", "comparator": null, \
"unit_as_printed": "arb.j.", "ref_range_as_printed": null, "flag_as_printed": null, \
"date_as_printed": "30.9.2027", "date_role": "sample", \
"evidence": "Q-Faktor X7   2,22   arb.j."}]}

Example 5. Page:
Pacientka prichádza na kontrolu, bez ťažkostí. Pokračovať v liečbe.
Answer:
{"facts": []}
"""

# LM Studio counts reasoning tokens into max_tokens, and enable_thinking=false
# may be ignored by the model (#689): a small budget ends in an empty answer
# with finish_reason=length. Use the full reasoning budget (as re-OCR does)
# until thinking-off is proven on the production model.
EXTRACT_BUDGET = llm.REASONING_BUDGET
METHOD = "stored_sidecar_text"


@dataclass
class PageResult:
    page_number: int
    candidates: list[dict] = field(default_factory=list)
    dropped: Counter = field(default_factory=Counter)  # reason -> count
    error: str | None = None
    seconds: float = 0.0
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length" or "truncated" in (self.error or "")

    @property
    def assessed(self) -> bool:
        """False for a truncated, empty or unparseable answer: the page was
        NOT read, which is never the same as "the model found nothing"."""
        return self.error is None and not self.truncated


def _text(v) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return format(v, "g") if isinstance(v, float) else str(v)
    if isinstance(v, str):
        return v.strip() or None
    return None


def parse_answer(text: str) -> list:
    """The raw ``facts`` list from a model answer.

    Tolerates reasoning blocks, ```json fences, text before/after the JSON and
    a bare top-level list. Raises ``LocalLLMError`` when no JSON is found."""
    text = llm.strip_think(text or "")
    blobs = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)]
    blobs.append(text)
    decoder = json.JSONDecoder()
    for blob in blobs:
        for i, ch in enumerate(blob):
            if ch not in "{[":
                continue
            try:
                obj, _ = decoder.raw_decode(blob[i:])
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("facts"), list):
                return obj["facts"]
            if isinstance(obj, list):
                return obj
    raise llm.LocalLLMError("local LLM answer has no facts JSON")


def normalize_item(raw) -> tuple[dict | None, str | None]:
    """(candidate, None) or (None, drop reason). Never invents a field."""
    if not isinstance(raw, dict):
        return None, "not_an_object"
    evidence = _text(raw.get("evidence"))
    label = _text(raw.get("label_as_printed") or raw.get("label"))
    value = _text(raw.get("value"))
    if not evidence:
        return None, "no_evidence"
    if not label or not value:
        return None, "no_label_or_value"
    comparator = _text(raw.get("comparator"))
    if comparator not in _COMPARATORS:
        comparator = None
    for c in _COMPARATORS:  # "<0,5" given as the value
        if value.startswith(c) and len(value) > len(c):
            comparator, value = comparator or c, value[len(c) :].strip()
            break
    date = _text(raw.get("date_as_printed"))
    role = _text(raw.get("date_role"))
    role = role.lower() if role else None
    if date and role not in DATE_ROLES:
        role = "other"
    return {
        "label_as_printed": label,
        "value": value,
        "comparator": comparator,
        "unit_as_printed": _text(raw.get("unit_as_printed") or raw.get("unit")),
        "ref_range_as_printed": _text(raw.get("ref_range_as_printed")),
        "flag_as_printed": _text(raw.get("flag_as_printed")),
        "date_as_printed": date,
        "date_role": role if date else None,
        "evidence": evidence,
    }, None


def _thinking() -> bool:
    from oncofiles import config

    return config.LLM_EXTRACT_THINKING == "on"


def extract_page(
    text: str, *, page_number: int, chat: Callable[..., str] | None = None
) -> PageResult:
    """Blocking. Candidate facts of one page (not yet verified)."""
    if not cloud_guard.local_only_job():
        raise cloud_guard.CloudForbiddenError("llm_extract must run inside local_only()")
    res = PageResult(page_number=page_number)
    if not (text or "").strip():
        return res
    call = chat or llm.chat
    user = f"Page {page_number}:\n{text}"
    thinking = _thinking()
    budget = EXTRACT_BUDGET
    info: dict = {}
    t0 = time.monotonic()
    try:
        try:
            answer = call(SYSTEM_PROMPT, user, max_tokens=budget, thinking=thinking,
                          response_format=RESPONSE_FORMAT, info=info)  # fmt: skip
        except llm.LocalLLMError as exc:
            if "HTTP 400" not in str(exc) or "truncated" in str(exc):
                raise
            # this server/model rejects structured output: strict instructions only
            info.clear()
            answer = call(SYSTEM_PROMPT, user, max_tokens=budget, thinking=thinking, info=info)
        res.finish_reason = info.get("finish_reason")
        if res.finish_reason == "length":
            raise llm.LocalLLMError("local LLM answer truncated: finish_reason=length")
        raw_items = parse_answer(answer)
    except llm.LocalLLMError as exc:
        res.finish_reason = res.finish_reason or info.get("finish_reason")
        res.error = str(exc)[:200]
        res.seconds = time.monotonic() - t0
        return res
    res.seconds = time.monotonic() - t0
    for raw in raw_items:
        cand, why = normalize_item(raw)
        if cand is None:
            res.dropped[why] += 1
        else:
            cand["page_number"] = page_number
            res.candidates.append(cand)
    return res


def extract_document(
    pages: list[tuple[int, str]],
    *,
    vocab,
    report_date: str | None = None,
    birth_dates=(),
    source_reason: str | None = None,
    chat: Callable[..., str] | None = None,
    stop: Callable[[], bool] | None = None,
) -> dict:
    """Blocking. Extract + verify every page; one model call at a time (one
    LM Studio slot). Returns verified fact dicts (``facts_rules`` shape) and
    counts; nothing is stored."""
    from oncofiles.curated import verifier

    text_of = dict(pages)
    facts: list[dict] = []
    dropped: Counter = Counter()
    errors = truncated = 0
    seconds = 0.0
    not_assessed: list[int] = []
    finish: dict[str, str | None] = {}
    for number, text in pages:
        if stop and stop():
            not_assessed.append(number)
            continue
        pr = extract_page(text, page_number=number, chat=chat)
        seconds += pr.seconds
        dropped.update(pr.dropped)
        finish[str(number)] = pr.finish_reason
        truncated += pr.truncated
        if not pr.assessed:
            errors += pr.error is not None
            not_assessed.append(number)
            continue
        for cand in pr.candidates:
            facts.append(
                verifier.verify(
                    cand, page_text=text_of[number], vocab=vocab, report_date=report_date,
                    birth_dates=birth_dates, source_reason=source_reason,
                )
            )  # fmt: skip
    dob_known = bool(birth_dates)
    return {
        "facts": facts, "method": METHOD, "pages": len(pages),
        "pages_used": len(pages) - len(not_assessed), "pages_not_assessed": not_assessed,
        "pages_truncated": truncated, "page_errors": errors, "finish_reasons": finish,
        "dob_known": dob_known,
        "birth_date_guard": "active" if dob_known else "inactive_no_dob",
        # dated facts whose date could not be checked against a birth date
        "dates_unchecked_no_dob": 0 if dob_known else sum(1 for f in facts if f["occurred_at"]),
        "dropped": dict(dropped), "seconds": round(seconds, 1),
    }  # fmt: skip


def _key(code, day, value) -> tuple:
    from oncofiles.curated.reread_verify import value_hash

    return (code, (day or "")[:10], value_hash(float(value)))


def shadow_compare(stored: list[dict], llm_facts: list[dict], *, not_assessed_pages=()) -> dict:
    """Stored (rule) facts vs LLM facts of ONE document, as counts and codes
    only — never values, labels or evidence. A key is (code, day, value hash).

    Pages the model did not assess (truncated, empty or unparseable answer)
    are left out on BOTH sides: such a page is unknown, never "LLM found 0"."""
    skip = {int(p) for p in not_assessed_pages}
    stored = [r for r in stored if int(r.get("page_number") or 1) not in skip]
    llm_facts = [f for f in llm_facts if int(f.get("page_number") or 1) not in skip]
    rule = Counter(
        _key(r["code"], r.get("occurred_at"), r["value_canonical"])
        for r in stored
        if r.get("code") and r.get("value_canonical") is not None
    )
    keyed = [f for f in llm_facts if f.get("code") and f.get("value_canonical") is not None]
    mine = Counter(_key(f["code"], f["occurred_at"], f["value_canonical"]) for f in keyed)
    machine = Counter(
        _key(f["code"], f["occurred_at"], f["value_canonical"])
        for f in keyed
        if f["verification"] == "machine"
    )
    both = rule & mine
    rule_cd = {(c, d) for c, d, _ in rule}
    mine_cd = {(c, d) for c, d, _ in mine}
    reasons: Counter = Counter()
    for f in llm_facts:
        reasons.update((f.get("metadata") or {}).get("unverified_reasons") or [])
    return {
        "rule_facts": sum(rule.values()),
        "llm_facts": len(llm_facts),
        "llm_machine": sum(1 for f in llm_facts if f["verification"] == "machine"),
        "llm_unverified": sum(1 for f in llm_facts if f["verification"] != "machine"),
        "agree": sum(both.values()),
        "agree_machine": sum((rule & machine).values()),
        "rule_only": sum((rule - mine).values()),
        "llm_only": sum((mine - rule).values()),
        # same code and day in both, but a different value
        "value_mismatch": len({(c, d) for c, d, _ in (rule - mine)} & mine_cd),
        "rule_only_codes": sorted({c for c, _ in rule_cd} - {c for c, _ in mine_cd}),
        "llm_only_codes": sorted({c for c, _ in mine_cd} - {c for c, _ in rule_cd}),
        "unverified_reasons": dict(sorted(reasons.items())),
    }
