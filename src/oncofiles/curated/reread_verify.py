"""Verify-only re-reads (#678): a local re-read confirms Cowork-era values.

The old sidecar text and its dates stay; nothing is written to Drive. The
source is read again locally (two vision passes, #660) and parsed with the
same rules. A Cowork-era fact loses its ``cowork_ocr`` reason only when

* the re-read has the same code and value on the same page, at least as many
  times as the old text has it (a repeated trend value never confirms several
  facts with one match), on a line both passes agree on (no ``[?]``), and
* the fact's date is printed on that page of the re-read too. Without the
  date, ``cowork_ocr`` becomes ``date_unconfirmed`` and the fact stays
  unverified: the Cowork date came from the same unreliable OCR.

Results are stored as keys only — (code, value hash, day, page) — never as
text, and are valid only for the sidecar sha and source md5 they were made on.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

CONFIRMABLE = "cowork_ocr"


def value_hash(value) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def fact_key(code: str, value, day: str, page) -> tuple:
    return (code, value_hash(value), (day or "")[:10], int(page or 0))


def date_printed(day: str | None, text: str) -> bool:
    """Is ``day`` printed in ``text``? With its year in any form; a bare
    day.month ("3.8.") counts only when the page shows that day and month with
    no other year, so a page quoting two years never confirms the wrong one."""
    if not day or not re.match(r"\d{4}-\d{2}-\d{2}", day):
        return False
    y, m, d = int(day[:4]), int(day[5:7]), int(day[8:10])
    dm = rf"(?<!\d)0?{d}\.\s?0?{m}\."
    if re.search(dm + rf"\s?(?:{y}|{y % 100:02d})(?!\d)", text):
        return True
    if re.search(rf"(?<!\d){y}-{m:02d}-{d:02d}(?!\d)", text):
        return True
    other_year = re.search(dm + r"\s?\d{2}(?:\d{2})?(?!\d)", text)
    return not other_year and bool(re.search(dm + r"(?!\s?\d)", text))


def _dates_of(fact: dict) -> list[str]:
    """The printed date(s) that dated this fact: a range needs both ends."""
    meta = fact.get("meta") or {}
    if meta.get("date_range_start") and meta.get("date_range_end"):
        return [meta["date_range_start"], meta["date_range_end"]]
    return [(fact.get("occurred_at") or "")[:10]]


ASSESS_VERSION = 2
_PRINTED = ("printed", "printed_range")


def _usable(f: dict) -> bool:
    return (
        bool(f.get("code"))
        and f.get("value_canonical") is not None
        and ("[?]" not in (f.get("evidence") or ""))
    )


def assess(
    old_facts: list[dict], new_facts: list[dict], new_pages: list[str],
    *, old_page_count: int | None = None,
) -> dict:  # fmt: skip
    """Which old Cowork-era facts the re-read confirms.

    ``old_facts``: id, code, value_canonical, occurred_at, page_number, meta.
    ``new_facts``: facts parsed from the re-read (same parser), with
    page_number, code, value_canonical, occurred_at, evidence, metadata.
    Returns {"confirmed": [ids], "date_unconfirmed": [ids], "unconfirmed": [ids]}.

    A paged old sidecar is matched page by page. A Cowork-era sidecar is often
    unpaged (no page markers, so every old fact sits on "page 1") while the
    re-read is paged (``old_page_count``: pages of the old sidecar, i.e.
    its page markers); then the match is on the re-read's own dated facts:
    the same code, value AND date, where the re-read printed that date itself
    (never the report-date fallback), as a document-level multiset, and the
    old date must be printed on the page of the matching new fact (#678 v2).
    """
    candidates = [
        f
        for f in old_facts
        if CONFIRMABLE in ((f.get("meta") or {}).get("unverified_reasons") or [])
        and f.get("value_canonical") is not None
    ]
    if old_page_count is None:  # callers without the sidecar: infer from the facts
        old_page_count = max([int(f.get("page_number") or 1) for f in old_facts] or [1])
    unpaged = len(new_pages) > 1 and old_page_count <= 1
    if unpaged:
        return _assess_unpaged(candidates, new_facts, new_pages)
    new_count: Counter = Counter(
        (f.get("page_number"), f.get("code"), f.get("value_canonical"))
        for f in new_facts
        if _usable(f)
    )
    old_count: Counter = Counter(
        (f.get("page_number"), f.get("code"), f.get("value_canonical")) for f in candidates
    )
    out: dict[str, list[int]] = {"confirmed": [], "date_unconfirmed": [], "unconfirmed": []}
    for f in candidates:
        key = (f.get("page_number"), f.get("code"), f.get("value_canonical"))
        if new_count[key] < old_count[key]:
            out["unconfirmed"].append(f["id"])
            continue
        page = int(f.get("page_number") or 0)
        text = new_pages[page - 1] if 0 < page <= len(new_pages) else ""
        if all(date_printed(d, text) for d in _dates_of(f)):
            out["confirmed"].append(f["id"])
        else:
            out["date_unconfirmed"].append(f["id"])
    return out


def _assess_unpaged(candidates: list[dict], new_facts: list[dict], new_pages: list[str]) -> dict:
    pages_of: dict[tuple, list[int]] = {}
    for f in new_facts:
        meta = f.get("metadata") or f.get("meta") or {}
        if not _usable(f) or meta.get("date_source") not in _PRINTED:
            continue
        key = (f["code"], f["value_canonical"], (f.get("occurred_at") or "")[:10])
        pages_of.setdefault(key, []).append(int(f.get("page_number") or 1))
    out: dict[str, list[int]] = {"confirmed": [], "date_unconfirmed": [], "unconfirmed": []}
    groups: dict[tuple, list[dict]] = {}
    for f in candidates:
        key = (f["code"], f["value_canonical"], (f.get("occurred_at") or "")[:10])
        groups.setdefault(key, []).append(f)
    for key, olds in groups.items():
        pages = pages_of.get(key, [])
        if len(pages) < len(olds):  # every old fact needs its own new match
            out["unconfirmed"].extend(f["id"] for f in olds)
            continue
        for f, page in zip(olds, pages, strict=False):
            text = new_pages[page - 1] if 0 < page <= len(new_pages) else ""
            if all(date_printed(d, text) for d in _dates_of(f)):
                out["confirmed"].append(f["id"])
            else:
                out["unconfirmed"].append(f["id"])
    return out


def diagnostics(old_facts: list[dict], new_facts: list[dict], *, old_page_count: int) -> dict:
    """Counts only (never values) to explain a low confirmation rate: how many
    facts each read has, how many code+value pairs and codes they share, and
    how the re-read dated its facts."""
    from collections import Counter

    def pairs(fs):
        return Counter((f.get("code"), f.get("value_canonical")) for f in fs
                       if f.get("code") and f.get("value_canonical") is not None)  # fmt: skip

    old_p, new_p = pairs(old_facts), pairs(new_facts)
    sources = Counter(((f.get("metadata") or f.get("meta") or {}).get("date_source") or "none")
                      for f in new_facts if f.get("code"))  # fmt: skip
    return {
        "old_facts": sum(old_p.values()),
        "new_facts": sum(new_p.values()),
        "code_value_shared": sum((old_p & new_p).values()),
        "codes_shared": len({c for c, _ in old_p} & {c for c, _ in new_p}),
        "codes_only_old": sorted({c for c, _ in old_p} - {c for c, _ in new_p}),
        "new_date_sources": dict(sources),
        "old_pages": old_page_count,
    }


def result_keys(assessment: dict, old_by_id: dict[int, dict]) -> dict:
    """The stored form: keys only, no ids (ids change on every re-extraction)."""

    def keys(ids):
        return sorted(
            list(fact_key(old_by_id[i]["code"], old_by_id[i]["value_canonical"],
                          old_by_id[i]["occurred_at"], old_by_id[i]["page_number"]))
            for i in ids
        )  # fmt: skip

    return {"assess_version": ASSESS_VERSION, "confirmed": keys(assessment["confirmed"]),
            "date_unconfirmed": keys(assessment["date_unconfirmed"])}  # fmt: skip


def apply_plan(result: dict, facts: list[dict]) -> list[tuple[int, str, dict]]:
    """(fact id, new verification, new metadata) for current facts of one
    document, from a stored result. Only ``cowork_ocr`` is ever removed;
    every other reason stays, and only unverified rows are considered."""
    confirmed = {tuple(k) for k in result.get("confirmed", [])}
    no_date = {tuple(k) for k in result.get("date_unconfirmed", [])}
    plan = []
    for f in facts:
        if f.get("verification") != "unverified":
            continue
        meta = dict(f.get("meta") or {})
        reasons = list(meta.get("unverified_reasons") or [])
        if CONFIRMABLE not in reasons:
            continue
        key = fact_key(f["code"], f["value_canonical"], f["occurred_at"], f["page_number"])
        if key in confirmed:
            rest = [r for r in reasons if r != CONFIRMABLE]
        elif key in no_date:
            rest = sorted({*(r for r in reasons if r != CONFIRMABLE), "date_unconfirmed"})
        else:
            continue
        meta["reread_checked"] = True
        if key in confirmed:
            meta["verified_by_reread"] = True  # only when the re-read confirmed it
        if rest:
            meta["unverified_reasons"] = rest
        else:
            meta.pop("unverified_reasons", None)
        plan.append((f["id"], "unverified" if rest else "machine", meta))
    return plan


def loads(text: str | None) -> dict:
    try:
        return json.loads(text or "{}")
    except ValueError:
        return {}
