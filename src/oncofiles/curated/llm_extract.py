"""LLM-first fact extraction (#714), phase 2a: the local model reads one page.

Decision 2026-11-11: extraction relies on the LOCAL model, not on per-layout
Python rules. This module only asks the model what is printed; it never
decides trust. Every candidate goes through ``curated.verifier`` (generic,
no layout knowledge), which accepts it as ``machine`` or keeps it
``unverified`` with reason codes.

* One call per page of sidecar text (text, not image: cheap); a dense page
  (many results on a few long lines, #826) is sent as a few short chunks.
* Strict JSON instructions (no ``response_format`` by default, v3; the
  answer is parsed defensively: code fences, reasoning, trailing text). The
  call itself (settings, fallbacks, diagnostics) is the shared path in
  ``curated.llm_extract_call`` (#767).
* Prompt rules: copy, never compute; unknown labels as printed; never a value
  without its verbatim evidence (the result's own printed segment). Items
  without evidence are dropped here.
* Must run inside ``cloud_guard.local_only()``; it only ever talks to
  ``curated.llm`` (LM Studio), never to a cloud client.

Phase 2a runs in SHADOW mode only (``curated_llm_extract_preview``): results
are compared with the stored rule facts as keys and counts, nothing is written.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable

from oncofiles import cloud_guard
from oncofiles.curated import llm
from oncofiles.curated import llm_extract_call as xcall
from oncofiles.curated.llm_extract_call import (  # noqa: F401 (re-exported)
    ERROR_KINDS,
    AnswerError,
    PageResult,
    error_kind,
)
from oncofiles.curated.ocr_markers import strip_charts_pages

# Bump on ANY change to the prompt, the schema, the item normalisation or the
# call settings. v3: no response_format by default; v4: shared call path,
# sampling switch, bounded answer budget and timeout (#767); v5: optional
# prefilled closed think block, timeout override (#770); v6: optional
# reasoning-off marker in the system message instead of /no_think (#770);
# v7: qwen sampling profiles follow the Qwen3.8 model card, prefill removed
# (the chat template renders a trailing assistant turn as finished, #770);
# v8: a truncated page is retried once as 2 line-boundary chunks (#773);
# v9: default request is the scorecard-validated one (qwen sampling +
# reasoning-off marker, env LLM_EXTRACT_SAMPLING /
# LLM_EXTRACT_REASONING_MARKER), also for writes (#773);
# v10: dense pages (#826): ``evidence`` is the result's own segment, not the
# whole line, and a page with many result-like segments is sent as density
# chunks with date / heading context (``llm_extract_call.split_by_density``).
# v11 (#933): every item carries ``date_evidence``, the printed text its date
# was taken from; the worked examples show it, and one shows a report that
# prints the draw date apart from the visit date.
EXTRACTOR_VERSION = "llm-extract-v11"

# Density split of the lab extractor (#826); the constants live with the splitter.
DENSITY = xcall.DensitySplit()

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
                    "date_evidence",
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
                    "date_evidence": {"type": ["string", "null"]},
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
_RULES = """You copy measured results (laboratory values, tumour markers, other \
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
- "date_evidence": the printed text you took the date from, copied character for \
character: the date with the word in front of it ("Odber: 05.03.2027 07:40", \
"Lab. z 10.4.2028"), or the range with its colon. It must contain "date_as_printed" \
exactly. null when "date_as_printed" is null.
- "evidence": this result's own printed piece of the page, copied character for \
character: from the test name to the end of its value, unit, range and mark. On a line \
with one result that is the line. On a line with several results (separated by ";" or \
",") it is ONLY this result's piece, up to the separator: never the whole line, never a \
neighbouring result, never the date or heading in front of it.

Rules:
1. Copy, never compute. Every value, unit, range and date must be on the page exactly as \
you give it. Never convert units, round, add, average or derive anything.
2. Unknown or unusual test names are returned exactly as printed. Never rename, \
translate or "correct" a label.
3. Never return a value without its verbatim evidence. If you cannot copy it, leave \
the result out.
4. A line with several results gives several items, each with its own short evidence.
5. Skip identifiers, birth dates, phone numbers, addresses, medicine doses, page \
numbers and dates that stand alone.
6. A page without results gives {"facts": []}.
7. A long page arrives in parts. A part may begin with "Context:" lines: date and \
heading lines repeated from earlier on the same page. Use them only to find the date of \
the results under "Text:"; never return a result from a Context line and never copy \
one into the evidence ("date_evidence" may be copied from a Context line).
8. Give a result only the date that belongs to IT. A clinic report often names the \
visit date and, elsewhere, the day the blood was taken: use the sampling date when it \
is printed for these results. If no date on the page belongs to the result, give \
null; never borrow an unrelated date.
"""


def _example(label, value, evidence, *, date, role, date_evidence, unit=None, ref=None,
             flag=None, comparator=None) -> dict:  # fmt: skip
    return {"label_as_printed": label, "value": value, "comparator": comparator,
            "unit_as_printed": unit, "ref_range_as_printed": ref, "flag_as_printed": flag,
            "date_as_printed": date, "date_role": role, "date_evidence": date_evidence,
            "evidence": evidence}  # fmt: skip


# (title, page text, facts). Every item must verify as ``machine`` against its
# page once its label is in the vocabulary (lock test), and every evidence is
# the result's own segment, never a whole multi-result line (#826).
EXAMPLES: tuple[tuple[str, str, list[dict]], ...] = (
    (
        "Page:",
        "Odber: 05.03.2027 07:40\nS-Glukóza   5,83   mmol/l   3,9 - 5,6   H",
        [_example("S-Glukóza", "5,83", "S-Glukóza   5,83   mmol/l   3,9 - 5,6   H",
                  unit="mmol/l", ref="3,9 - 5,6", flag="H",
                  date="05.03.2027 07:40", role="sample",
                  date_evidence="Odber: 05.03.2027 07:40")],
    ),
    (
        "Page (a report: the visit date and the sampling date differ; two results on one "
        "line, each item quotes only its own piece):",
        "Kontrola 12.4.2028. Lab. z 10.4.2028: CRP 17,3 mg/l, Leukocyty 9,1.",
        [_example("CRP", "17,3", "CRP 17,3 mg/l", unit="mg/l", date="10.4.2028",
                  role="sample", date_evidence="Lab. z 10.4.2028"),
         _example("Leukocyty", "9,1", "Leukocyty 9,1", date="10.4.2028", role="sample",
                  date_evidence="Lab. z 10.4.2028")],
    ),
    (
        "Page:",
        "Vydané: 2.2.2029\nCEA        <0,5 ng/ml\nHBsAg      negat.",
        [_example("CEA", "0,5", "CEA        <0,5 ng/ml", comparator="<", unit="ng/ml",
                  date="2.2.2029", role="report", date_evidence="Vydané: 2.2.2029"),
         _example("HBsAg", "negat.", "HBsAg      negat.", date="2.2.2029", role="report",
                  date_evidence="Vydané: 2.2.2029")],
    ),
    (
        "Page (an unknown test stays as printed):",
        "Odobraté 30.9.2027\nQ-Faktor X7   2,22   arb.j.",
        [_example("Q-Faktor X7", "2,22", "Q-Faktor X7   2,22   arb.j.", unit="arb.j.",
                  date="30.9.2027", role="sample", date_evidence="Odobraté 30.9.2027")],
    ),
    (
        "Page:",
        "Pacientka prichádza na kontrolu, bez ťažkostí. Pokračovať v liečbe.",
        [],
    ),
    (
        'Page (a long line of results separated by ";"):',
        "Odber: 18.11.2027 06:55\n"
        "Biochémia: Faktor M 4,71 mmol/l (2,10 - 4,20) H; Faktor N 0,83 g/l (0,50 - 1,90); "
        "Faktor P <3 U/l",
        [_example("Faktor M", "4,71", "Faktor M 4,71 mmol/l (2,10 - 4,20) H", unit="mmol/l",
                  ref="2,10 - 4,20", flag="H", date="18.11.2027 06:55", role="sample",
                  date_evidence="Odber: 18.11.2027 06:55"),
         _example("Faktor N", "0,83", "Faktor N 0,83 g/l (0,50 - 1,90)", unit="g/l",
                  ref="0,50 - 1,90", date="18.11.2027 06:55", role="sample",
                  date_evidence="Odber: 18.11.2027 06:55"),
         _example("Faktor P", "3", "Faktor P <3 U/l", comparator="<", unit="U/l",
                  date="18.11.2027 06:55", role="sample",
                  date_evidence="Odber: 18.11.2027 06:55")],
    ),
    (
        "Part of a page (the date comes from a Context line):",
        "Context:\nOdobrané: 9.1.2028 07:10\nKrvný obraz:\nText:\n"
        "Faktor R 1,15 µkat/l (0,20 - 1,40); Faktor S 88 % (30 - 120) L",
        [_example("Faktor R", "1,15", "Faktor R 1,15 µkat/l (0,20 - 1,40)", unit="µkat/l",
                  ref="0,20 - 1,40", date="9.1.2028 07:10", role="sample",
                  date_evidence="Odobrané: 9.1.2028 07:10"),
         _example("Faktor S", "88", "Faktor S 88 % (30 - 120) L", unit="%", ref="30 - 120",
                  flag="L", date="9.1.2028 07:10", role="sample",
                  date_evidence="Odobrané: 9.1.2028 07:10")],
    ),
)  # fmt: skip


def _render_examples() -> str:
    out = []
    for n, (title, page, facts) in enumerate(EXAMPLES, 1):
        answer = json.dumps({"facts": facts}, ensure_ascii=False)
        out.append(f"Example {n}. {title}\n{page}\nAnswer:\n{answer}\n")
    return "\n".join(out)


SYSTEM_PROMPT = _RULES + "\n" + _render_examples()

METHOD = "stored_sidecar_text"


def _text(v) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return format(v, "g") if isinstance(v, float) else str(v)
    if isinstance(v, str):
        return v.strip() or None
    return None


def _json_values(text: str):
    """Every top-level JSON object/list decodable in ``text`` (fenced blocks
    first). A decoded value is skipped as a whole, so a list nested inside
    another object (``{"results": []}``) is never taken for the facts list."""
    text = llm.strip_think(text or "")
    blobs = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)]
    blobs.append(text)
    decoder = json.JSONDecoder()
    for blob in blobs:
        i = 0
        while i < len(blob):
            if blob[i] not in "{[":
                i += 1
                continue
            try:
                obj, end = decoder.raw_decode(blob, i)
            except ValueError:
                i += 1
                continue
            yield obj
            i = end


def parse_answer(text: str) -> list:
    """The raw ``facts`` list from a model answer.

    Tolerates reasoning blocks, ```json fences, text before/after the JSON and
    a bare top-level list. Raises ``AnswerError`` with kind ``empty`` (blank
    answer), ``json_decode`` (no JSON at all) or ``schema_shape`` (JSON, but
    no ``facts`` list)."""
    if not llm.strip_think(text or "").strip():
        raise AnswerError("empty", "local LLM answer is empty")
    found = False
    for obj in _json_values(text):
        found = True
        if isinstance(obj, dict) and isinstance(obj.get("facts"), list):
            return obj["facts"]
        if isinstance(obj, list):
            return obj
    if found:
        raise AnswerError("schema_shape", "local LLM answer JSON has no facts list")
    raise AnswerError("json_decode", "local LLM answer has no JSON")


_NULLABLE = ("comparator", "unit_as_printed", "ref_range_as_printed", "flag_as_printed",
             "date_as_printed", "date_role", "date_evidence")  # fmt: skip
_REQUIRED_STR = ("label_as_printed", "value", "evidence")


def valid_against_schema(obj) -> bool:
    """Does ``obj`` satisfy ``SCHEMA`` (``{"facts": [items]}``, no extra keys,
    every item with all keys of the right type)? A small hand check of the
    one schema we send; no jsonschema dependency."""
    if not isinstance(obj, dict) or set(obj) != {"facts"} or not isinstance(obj["facts"], list):
        return False
    keys = set(_NULLABLE) | set(_REQUIRED_STR)
    for item in obj["facts"]:
        if not isinstance(item, dict) or set(item) != keys:
            return False
        if any(not isinstance(item[k], str) for k in _REQUIRED_STR):
            return False
        if any(item[k] is not None and not isinstance(item[k], str) for k in _NULLABLE):
            return False
        if item["date_role"] not in (*DATE_ROLES, None):
            return False
    return True


def parse_reasoning_channel(text: str) -> list:
    """The facts list from ``reasoning_content`` — accepted ONLY when the
    channel holds exactly one complete top-level JSON value, which is an
    object valid against ``SCHEMA``. A draft plus a final answer, a partial
    object or an invalid one is ambiguous: ``AnswerError("reasoning_ambiguous")``,
    the page is not assessed (infra rule, #714)."""
    body = llm.strip_think(text or "")
    decoder = json.JSONDecoder()
    values, i = [], 0
    while i < len(body):
        if body[i] not in "{[":
            i += 1
            continue
        try:
            obj, i = decoder.raw_decode(body, i)
        except ValueError:  # an unterminated or broken value
            raise AnswerError("reasoning_ambiguous", "reasoning channel: partial JSON") from None
        values.append(obj)
    if len(values) != 1:
        msg = f"reasoning channel: {len(values)} JSON values, need exactly one"
        raise AnswerError("reasoning_ambiguous", msg)
    if not valid_against_schema(values[0]):
        raise AnswerError("reasoning_ambiguous", "reasoning channel: JSON fails the schema")
    return values[0]["facts"]


_WORD = re.compile(r"[^\W\d_]+")
_JSON_WORDS = "facts null true false label as printed value comparator unit ref range flag date"
_JSON_WORDS += " role evidence sample report other"


def _fold(w: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", w) if not unicodedata.combining(c))


def known_words(vocab=None) -> frozenset[str]:
    """Words that stay readable in a masked answer: the JSON schema keys and
    the lab vocabulary (codes, aliases, units) — never patient data."""
    words = {_fold(w).lower() for w in _JSON_WORDS.split()}
    for row in getattr(vocab, "by_code", {}).values():
        texts = [
            row.get("code") or "",
            *(row.get("aliases") or []),
            row.get("unit_canonical") or "",
        ]
        texts += list((row.get("unit_conversions") or {}).keys())
        for t in texts:
            words.update(_fold(w).lower() for w in _WORD.findall(str(t)))
    return frozenset(words)


MASK_CHARS = 600


def mask(text: str, known: frozenset[str]) -> str:
    """Digits → 9; a word → a's of its length unless it is a known word.
    Same masking as the re-read fingerprint (#712)."""
    text = re.sub(r"\d", "9", text[:MASK_CHARS])
    return _WORD.sub(lambda m: m.group(0) if _fold(m.group(0)).lower() in known
                     else "a" * len(m.group(0)), text)  # fmt: skip


def answer_shape(raw: str, known: frozenset[str], *, field_used: str) -> dict:
    """The shape of a raw model answer — no digit and no unknown word.
    In-memory report only, never stored."""
    top = "none"
    for obj in _json_values(raw):
        top = "object" if isinstance(obj, dict) else "list"
        break
    return {
        "field": field_used,
        "chars": len(raw or ""),
        "has_think": "<think>" in (raw or "") or "</think>" in (raw or ""),
        "has_fence": "```" in (raw or ""),
        "top_level": top,
        "masked": mask(raw or "", known),
    }


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
    from oncofiles.curated.verifier import is_section_header

    if is_section_header(label):  # "* (U-SED)", "Moč | Záver": not a test (#814)
        return None, "section_header"
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
        "date_evidence": _text(raw.get("date_evidence")) if date else None,
        "evidence": evidence,
    }, None


# Date reasons whose returned ``date_as_printed`` is reported as a masked
# shape (#826): the report must tell WHY a date failed without showing it.
DATE_SHAPE_REASONS = frozenset({"date_unparsed", "date_not_printed"})
DATE_SHAPE_CHARS = 40
DATE_SHAPE_TOP = 5


def date_shape(printed: str | None) -> str:
    """The masked shape of a returned date: every digit → ``9``, every letter
    → ``a``, other characters kept, whitespace runs → one space, at most
    ``DATE_SHAPE_CHARS`` characters. Never the date itself."""
    text = re.sub(r"\s+", " ", printed or "").strip()[:DATE_SHAPE_CHARS]
    return "".join("9" if c.isdigit() else "a" if c.isalpha() else c for c in text)


# Label review list (#814): the labels the model returned that no
# ``fact_codes`` alias maps (``label_unmapped``) or that map only with a typo
# (``label_fuzzy``), so a maintainer can add the missing aliases (data)
# without reading a sidecar. IN-MEMORY job report only: never stored (the
# shadow preview writes nothing, a scorecard persists only its totals, the
# lab write job does not carry the list), never logged.
LABEL_REVIEW_REASONS = ("label_unmapped", "label_fuzzy")
LABEL_REVIEW_CHARS = 60
LABEL_REVIEW_UNIT_CHARS = 20
LABEL_REVIEW_TOP = 50
_DIGIT = re.compile(r"\d")


def _digits_masked(text: str) -> tuple[str, bool]:
    """``text`` with every digit → ``9``, and whether anything was masked."""
    masked = _DIGIT.sub("9", text)
    return masked, masked != text


def unmapped_labels(facts: list[dict]) -> tuple[list[dict], int]:
    """(review list, omitted count) of ONE document's LLM facts.

    One entry per distinct (reason, label, near code, unit), with its count:
    ``{"reason", "label", "label_masked", "count"}`` plus ``near_code`` for
    ``label_fuzzy`` (the code the label was fuzzily matched to) and ``unit``
    when the model returned one in its own field (lab units are vocabulary).
    The label is whitespace-normalised; a label with a digit is replaced by
    its masked shape (digits → ``9``, ``label_masked: true``), so no value can ride
    along (units are masked the same way). A label longer than
    ``LABEL_REVIEW_CHARS`` is left out and counted as omitted, as is anything
    past the ``LABEL_REVIEW_TOP`` most frequent entries; a unit longer than
    ``LABEL_REVIEW_UNIT_CHARS`` is dropped. Never a value, a date or evidence
    text."""
    seen: Counter = Counter()
    omitted = 0
    for f in facts:
        reasons = (f.get("metadata") or {}).get("unverified_reasons") or ()
        reason = next((r for r in LABEL_REVIEW_REASONS if r in reasons), None)
        if reason is None:
            continue
        label = re.sub(r"\s+", " ", str(f.get("label") or "")).strip()
        if not label or len(label) > LABEL_REVIEW_CHARS:
            omitted += 1
            continue
        label, masked = _digits_masked(label)
        unit = re.sub(r"\s+", " ", str(f.get("unit") or "")).strip()
        unit = _digits_masked(unit)[0] if len(unit) <= LABEL_REVIEW_UNIT_CHARS else ""
        near = (f.get("code") or "") if reason == "label_fuzzy" else ""
        seen[(reason, label, masked, near, unit)] += 1
    ranked = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
    omitted += sum(n for _, n in ranked[LABEL_REVIEW_TOP:])
    out = []
    for (reason, label, masked, near, unit), n in ranked[:LABEL_REVIEW_TOP]:
        item: dict = {"reason": reason, "label": label, "label_masked": masked, "count": n}
        if near:
            item["near_code"] = near
        if unit:
            item["unit"] = unit
        out.append(item)
    return out, omitted


def _structured_output() -> bool:
    from oncofiles import config

    return config.LLM_EXTRACT_STRUCTURED_OUTPUT


def extract_page(
    text: str,
    *,
    page_number: int,
    chat: Callable[..., str] | None = None,
    known: frozenset[str] | None = None,
    cfg: xcall.CallSettings | None = None,
) -> PageResult:
    """Blocking. Candidate facts of one page (not yet verified), plus
    diagnostics of the model answer (masked shape, error kind, token counts).
    ``cfg`` (from ``llm_extract_call.settings``) defaults to the env settings."""
    if not cloud_guard.local_only_job():
        raise cloud_guard.CloudForbiddenError("llm_extract must run inside local_only()")
    res = PageResult(page_number=page_number)
    if not (text or "").strip():
        return res
    known = known if known is not None else known_words()
    parts = xcall.call_page(
        res, text=text, system=SYSTEM_PROMPT,
        response_format=RESPONSE_FORMAT if _structured_output() else None,
        parse_content=parse_answer, parse_reasoning=parse_reasoning_channel,
        cfg=cfg or xcall.settings(), chat=chat,
        shape=lambda raw, used: answer_shape(raw, known, field_used=used), label="llm extract",
        density=DENSITY,
    )  # fmt: skip
    if parts is None:
        return res
    for part in parts:  # 1 part, or one per chunk after a split (#773, #826)
        for raw_item in part.parsed:
            cand, why = normalize_item(raw_item)
            if cand is None:
                res.dropped[why] += 1
            else:
                cand["page_number"] = page_number
                cand["answer_source"] = part.answer_source
                res.candidates.append(cand)
    xcall.finish_candidates(res)
    if res.answer:
        res.answer = {k: v for k, v in res.answer.items() if k != "masked"}  # shape only
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
    cfg: xcall.CallSettings | None = None,
    document_category: str | None = None,
) -> dict:
    """Blocking. Extract + verify every page; one model call at a time (one
    LM Studio slot). Returns verified fact dicts (``facts_rules`` shape) and
    counts; nothing is stored. ``document_category`` goes to the verifier
    (a report date on a report is no draw date, #933)."""
    from oncofiles.curated import verifier

    cfg = cfg or xcall.settings()
    known = known_words(vocab)
    results: list[PageResult] = []
    pages = strip_charts_pages(pages)  # #609: chart blocks are never data
    text_of = dict(pages)
    facts: list[dict] = []
    dropped: Counter = Counter()
    errors = truncated = 0
    seconds = 0.0
    not_assessed: list[int] = []
    date_shapes: Counter = Counter()
    finish: dict[str, str | None] = {}
    page_diag: dict[str, dict] = {}
    for number, text in pages:
        if stop and stop():
            not_assessed.append(number)
            continue
        pr = extract_page(text, page_number=number, chat=chat, known=known, cfg=cfg)
        results.append(pr)
        page_diag[str(number)] = pr.diagnostics()
        seconds += pr.seconds
        dropped.update(pr.dropped)
        finish[str(number)] = pr.finish_reason
        truncated += pr.truncated
        if not pr.assessed:
            errors += pr.error is not None
            not_assessed.append(number)
            continue
        for cand in pr.candidates:
            fact = verifier.verify(
                cand, page_text=text_of[number], vocab=vocab, report_date=report_date,
                birth_dates=birth_dates, source_reason=source_reason,
                document_category=document_category,
            )  # fmt: skip
            facts.append(fact)
            if DATE_SHAPE_REASONS.intersection(fact["metadata"].get("unverified_reasons") or ()):
                date_shapes[date_shape(cand.get("date_as_printed"))] += 1
    dob_known = bool(birth_dates)
    return {
        "facts": facts, "method": METHOD, "pages": len(pages),
        "pages_used": len(pages) - len(not_assessed), "pages_not_assessed": not_assessed,
        "pages_truncated": truncated, "page_errors": errors, "finish_reasons": finish,
        "page_diagnostics": page_diag, "warnings": xcall.warnings(results),
        **xcall.date_null_totals(results),
        "dob_known": dob_known,
        "birth_date_guard": "active" if dob_known else "inactive_no_dob",
        # dated facts whose date could not be checked against a birth date
        "dates_unchecked_no_dob": 0 if dob_known else sum(1 for f in facts if f["occurred_at"]),
        "facts_from_content": sum(1 for f in facts if f["metadata"]["answer_source"] == "content"),
        "facts_from_reasoning": sum(
            1 for f in facts if f["metadata"]["answer_source"] == "reasoning"
        ),
        # masked shapes (9 / a) of the dates the verifier could not parse or
        # find on the page: top shapes with counts, never a date (#826)
        "date_shapes_unverified": dict(date_shapes.most_common(DATE_SHAPE_TOP)),
        "dropped": dict(dropped), "seconds": round(seconds, 1),
    }  # fmt: skip


def _key(code, day, value) -> tuple:
    from oncofiles.curated.reread_verify import value_hash

    return (code, (day or "")[:10], value_hash(float(value)))


# Comparison buckets (#826). Every LLM fact and every compared rule fact of a
# document lands in EXACTLY ONE bucket; a pair of facts counts once on each
# side, so the paired buckets hold the same number on both sides.
PAIRED_BUCKETS = ("agree", "parser_crosscheck", "value_mismatch", "date_mismatch")
RULE_BUCKETS = (*PAIRED_BUCKETS, "rule_only")
LLM_BUCKETS = (*PAIRED_BUCKETS, "llm_only", "llm_no_value", "llm_uncoded")
# Every bucket of either side. Every sum over the buckets
# (``tools.curated._LAB_SUM_KEYS``, ``scorecard._LAB_COUNTS``) must list all
# of them (lock test, #933).
BUCKETS = tuple(dict.fromkeys((*RULE_BUCKETS, *LLM_BUCKETS)))


def _page(row: dict) -> int:
    return int(row.get("page_number") or 1)


def _comparable(row: dict) -> bool:
    return bool(row.get("code")) and row.get("value_canonical") is not None


def _row_key(row: dict) -> tuple:
    return _key(row["code"], row.get("occurred_at"), row["value_canonical"])


def _pair(
    rule: list[tuple],
    mine: list[tuple],
    part: Callable[[tuple], tuple],
    bucket: str,
    out_rule: list[tuple],
    out_mine: list[tuple],
) -> tuple[list[tuple], list[tuple]]:
    """Pair the facts of both sides that share ``part(key)``, one to one;
    paired facts go to ``bucket``, the rest of each side is returned."""
    take = Counter(part(k) for k, _ in rule) & Counter(part(k) for k, _ in mine)
    rest: list[list[tuple]] = []
    for side, out in ((rule, out_rule), (mine, out_mine)):
        budget, kept = Counter(take), []
        for k, row in side:
            if budget[part(k)] > 0:
                budget[part(k)] -= 1
                out.append((bucket, row))
            else:
                kept.append((k, row))
        rest.append(kept)
    return rest[0], rest[1]


def bucket_facts(
    stored: list[dict], llm_facts: list[dict], *, not_assessed_pages=(), crosscheck_pages=()
) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    """(rule side, LLM side) as ``(bucket, fact)`` lists: the ONE partition
    behind ``shadow_compare`` and ``agreement_by_code``.

    Compared are the facts on pages the model assessed: on the rule side the
    stored facts with a code and a canonical value (``rule_facts``), on the
    LLM side every fact (``llm_facts``). A key is (code, day, value hash).
    Buckets, decided in this order:

    * ``parser_crosscheck`` — the same key on both sides, on a transcription
      page (the same text read twice, not a verification);
    * ``agree`` — the same key on both sides, elsewhere;
    * ``value_mismatch`` — same code and day, another value;
    * ``date_mismatch`` — same code and value hash, another day or no day on
      one side (e.g. the model's date could not be parsed);
    * ``rule_only`` / ``llm_only`` — a comparable fact with no counterpart;
    * ``llm_no_value`` — an LLM fact with a code but no canonical value (text
      result, unit not convertible): it has no value hash to compare;
    * ``llm_uncoded`` — an LLM fact whose label maps to no code.

    The first four are pairs (one rule fact with one LLM fact), so each holds
    the same count on both sides. Stored facts without a code or a value are
    not compared (``rule_facts_no_value`` reports them)."""
    skip = {int(p) for p in not_assessed_pages}
    cross = {int(p) for p in crosscheck_pages}
    out_rule: list[tuple[str, dict]] = []
    out_mine: list[tuple[str, dict]] = []
    rule = [(_row_key(r), r) for r in stored if _comparable(r) and _page(r) not in skip]
    mine = []
    for f in llm_facts:
        if _page(f) in skip:
            continue
        if not f.get("code"):
            out_mine.append(("llm_uncoded", f))
        elif f.get("value_canonical") is None:
            out_mine.append(("llm_no_value", f))
        else:
            mine.append((_row_key(f), f))
    # the same key on both sides: crosscheck pairs first (both on such a page)
    n_cross = Counter(k for k, r in rule if _page(r) in cross) & Counter(
        k for k, f in mine if _page(f) in cross
    )
    n_same = Counter(k for k, _ in rule) & Counter(k for k, _ in mine)
    rest: list[list[tuple]] = []
    for side, out in ((rule, out_rule), (mine, out_mine)):
        x, same, kept = Counter(n_cross), n_same - n_cross, []
        for k, row in sorted(side, key=lambda kr: _page(kr[1]) not in cross):  # stable
            if x[k] > 0 and _page(row) in cross:
                x[k] -= 1
                out.append(("parser_crosscheck", row))
            elif same[k] > 0:
                same[k] -= 1
                out.append(("agree", row))
            else:
                kept.append((k, row))
        rest.append(kept)
    rule, mine = rest
    rule, mine = _pair(rule, mine, lambda k: k[:2], "value_mismatch", out_rule, out_mine)
    rule, mine = _pair(rule, mine, lambda k: (k[0], k[2]), "date_mismatch", out_rule, out_mine)
    out_rule += [("rule_only", r) for _, r in rule]
    out_mine += [("llm_only", f) for _, f in mine]
    return out_rule, out_mine


def _codes(side: list[tuple[str, dict]], bucket: str) -> list[str]:
    return sorted({row["code"] for b, row in side if b == bucket and row.get("code")})


def shadow_compare(
    stored: list[dict], llm_facts: list[dict], *, not_assessed_pages=(), crosscheck_pages=()
) -> dict:
    """Stored (rule) facts vs LLM facts of ONE document, as counts and codes
    only — never values, labels or evidence. A key is (code, day, value hash).

    Pages the model did not assess (truncated, empty or unparseable answer)
    are left out on BOTH sides: such a page is unknown, never "LLM found 0".

    On ``crosscheck_pages`` (transcription text: Cowork-era, uncertain or
    handwritten, see ``verifier.transcription_pages``) the LLM read the same
    text the rule parser read, so agreement there is reported only as
    ``parser_crosscheck``, never as ``agree`` (verification).

    Accounting (#826, buckets defined in ``bucket_facts``): every compared
    fact is in exactly one bucket, so

        rule_facts == agree + parser_crosscheck + value_mismatch
                      + date_mismatch + rule_only
        llm_facts  == agree + parser_crosscheck + value_mismatch
                      + date_mismatch + llm_only + llm_no_value + llm_uncoded

    and each ``*_codes`` list names the codes of the facts in that bucket
    (non-empty whenever the count is)."""
    skip = {int(p) for p in not_assessed_pages}
    cross = {int(p) for p in crosscheck_pages}
    all_stored = [r for r in stored if r.get("code")]
    stored_info = {
        # every stored fact of the document, whatever its verification (#714
        # prod run: rule_facts=0 hid 8 unverified facts on an unassessed page)
        "rule_facts_stored": len(all_stored),
        "rule_facts_by_verification": dict(
            sorted(Counter(r.get("verification") or "unknown" for r in all_stored).items())
        ),
        "rule_facts_no_value": sum(1 for r in all_stored if r.get("value_canonical") is None),
        "rule_facts_on_unassessed_pages": sum(1 for r in all_stored if _page(r) in skip),
    }
    rule_side, llm_side = bucket_facts(
        stored, llm_facts, not_assessed_pages=skip, crosscheck_pages=cross
    )
    stored = [r for r in stored if _page(r) not in skip]
    llm_facts = [f for f in llm_facts if _page(f) not in skip]
    rule = Counter(_row_key(r) for r in stored if _comparable(r))
    keyed = [f for f in llm_facts if _comparable(f)]
    mine = Counter(_row_key(f) for f in keyed)
    machine = Counter(_row_key(f) for f in keyed if f["verification"] == "machine")
    crosscheck = Counter(_row_key(r) for r in stored if _comparable(r) and _page(r) in cross)
    crosscheck &= Counter(_row_key(f) for f in keyed if _page(f) in cross)
    reasons: Counter = Counter()
    for f in llm_facts:
        reasons.update((f.get("metadata") or {}).get("unverified_reasons") or [])
    unverified_rule = Counter(
        _row_key(r) for r in stored if _comparable(r) and r.get("verification") == "unverified"
    )
    n_rule = Counter(b for b, _ in rule_side)
    n_llm = Counter(b for b, _ in llm_side)
    return {
        **stored_info,
        # compared = stored facts with a code and a value on assessed pages
        "rule_facts": sum(rule.values()),
        "rule_facts_compared_unverified": sum(unverified_rule.values()),
        "agree_with_unverified_rule": sum(((unverified_rule & mine) - crosscheck).values()),
        "llm_facts": len(llm_facts),
        "llm_machine": sum(1 for f in llm_facts if f["verification"] == "machine"),
        "llm_unverified": sum(1 for f in llm_facts if f["verification"] != "machine"),
        "agree": n_rule["agree"],
        "agree_machine": sum(((rule & machine) - crosscheck).values()),
        # agreement on transcription pages: the same text read twice, not a verification
        "parser_crosscheck": n_rule["parser_crosscheck"],
        "crosscheck_pages": sorted(cross - skip),
        # pairs: one rule fact with one LLM fact (the same count on both sides)
        "value_mismatch": n_rule["value_mismatch"],  # same code and day, another value
        "date_mismatch": n_rule["date_mismatch"],  # same code and value, another / no day
        "rule_only": n_rule["rule_only"],
        "llm_only": n_llm["llm_only"],
        "llm_no_value": n_llm["llm_no_value"],  # coded, nothing to hash: not comparable
        "llm_uncoded": n_llm["llm_uncoded"],  # no code mapped: not comparable
        "value_mismatch_codes": _codes(rule_side, "value_mismatch"),
        "date_mismatch_codes": _codes(rule_side, "date_mismatch"),
        "rule_only_codes": _codes(rule_side, "rule_only"),
        "llm_only_codes": _codes(llm_side, "llm_only"),
        "llm_no_value_codes": _codes(llm_side, "llm_no_value"),
        "unverified_reasons": dict(sorted(reasons.items())),
    }


def agreement_by_code(
    stored: list[dict], llm_facts: list[dict], *, not_assessed_pages=(), crosscheck_pages=()
) -> dict[str, dict[str, int]]:
    """The ``shadow_compare`` buckets per code (#714 lab write gate):
    ``{code: {agree, parser_crosscheck, value_mismatch, date_mismatch,
    rule_only, llm_only}}`` on the pages the model assessed — the same
    partition (``bucket_facts``), so the per-code counts add up to the
    document's. Counts and codes only, never values. Agreement on
    transcription pages is ``parser_crosscheck``, never ``agree``. LLM facts
    without a canonical value or a code are not listed here."""
    rule_side, llm_side = bucket_facts(
        stored, llm_facts, not_assessed_pages=not_assessed_pages, crosscheck_pages=crosscheck_pages
    )
    out: dict[str, Counter] = {}
    for bucket, row in rule_side:
        out.setdefault(row["code"], Counter())[bucket] += 1
    for bucket, row in llm_side:
        if bucket == "llm_only":  # the paired buckets are counted on the rule side
            out.setdefault(row["code"], Counter())[bucket] += 1
    order = (*PAIRED_BUCKETS, "rule_only", "llm_only")
    return {c: {b: out[c][b] for b in order if out[c][b]} for c in sorted(out)}
