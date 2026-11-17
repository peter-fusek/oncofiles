"""Dry re-OCR compare (#883): read a source again, compare with TODAY's sidecar.

Audit of sidecars a re-OCR rewrote while its two-pass guard was void for
image files (both passes saw byte-identical pixels). The source is read again
with the same ``reread`` as a re-OCR (pass 1 on the whole page, pass 2 as
independent halves, ``reconcile``), and that reconciled reading is compared
with the sidecar as it is on Drive today, page by page.

Never writes: no Drive call besides two downloads, no DB write, no backup,
no candidate, no index. The report holds counts, reason names and fact codes
only, never a value, a date or a line of text.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from oncofiles.curated import reocr
from oncofiles.curated.indexer import parse_sidecar

COMPARE_MAX_DOCS = 10


def _split_uncertain(page: str) -> tuple[str, str]:
    """A reconciled page as (lines both passes agree on, lines marked ``[?]``)."""
    sure, unsure = [], []
    for line in (page or "").splitlines():
        (unsure if "[?]" in line else sure).append(line)
    return "\n".join(sure), "\n".join(unsure)


def compare_page(today: str, new: str) -> dict:
    """Counts of one page: numbers and dates of today's sidecar page against
    the new reconciled reading of that page. Number and date tokens are the
    re-OCR loss check's own (``reocr._tokens``)."""
    old_nums, old_dates = reocr._tokens(today or "")
    sure, unsure = _split_uncertain(new)
    sure_nums, sure_dates = reocr._tokens(sure)
    unsure_nums, unsure_dates = reocr._tokens(unsure)
    new_nums, new_dates = sure_nums | unsure_nums, sure_dates | unsure_dates
    confirmed = old_nums & sure_nums
    return {
        "numbers_today": len(old_nums),
        "numbers_confirmed": len(confirmed),
        "numbers_uncertain": len((old_nums & unsure_nums) - confirmed),
        "numbers_not_found": len(old_nums - new_nums),
        "numbers_only_new": len(new_nums - old_nums),
        "dates_today": len(old_dates),
        "dates_same": len(old_dates & new_dates),
        "dates_not_found": len(old_dates - new_dates),
        "dates_only_new": len(new_dates - old_dates),
        "uncertain_lines_today": sum("[?]" in ln for ln in (today or "").splitlines()),
        "uncertain_lines_new": sum("[?]" in ln for ln in (new or "").splitlines()),
    }


PAGE_KEYS = tuple(compare_page("", ""))


def compare_pages(today_pages: list[str], new_pages: list[str]) -> list[dict]:
    """``compare_page`` for every page either side has (a page one side lacks
    compares with an empty page)."""
    n = max(len(today_pages), len(new_pages))
    return [
        {"page": i + 1, **compare_page(
            today_pages[i] if i < len(today_pages) else "",
            new_pages[i] if i < len(new_pages) else "",
        )}
        for i in range(n)
    ]  # fmt: skip


def _by_code_day(facts: set[tuple]) -> dict[tuple, tuple]:
    out: dict[tuple, list] = {}
    for code, value, day in facts:
        out.setdefault((code, day), []).append(value)
    return {k: tuple(sorted(v, key=str)) for k, v in out.items()}


def _tally(keys) -> dict[str, int]:
    out: dict[str, int] = {}
    for code, _day in keys:
        out[code] = out.get(code, 0) + 1
    return dict(sorted(out.items()))


def compare_facts(today_text: str, new_text: str, vocab) -> dict:
    """Lab facts (``extract_lab_facts`` via ``reocr._facts_of``, the re-OCR
    guard's own parse) of today's sidecar against the new reading, per code
    and day: agree / value differs / missing on either side. Counts and codes
    only; the values and days never leave this function."""
    today = _by_code_day(reocr._facts_of(today_text, vocab))
    new = _by_code_day(reocr._facts_of(new_text, vocab))
    agree = [k for k in today if k in new and today[k] == new[k]]
    differs = [k for k in today if k in new and today[k] != new[k]]
    missing_new = [k for k in today if k not in new]
    missing_today = [k for k in new if k not in today]
    return {
        "agree": len(agree),
        "value_differs": len(differs),
        "missing_in_new": len(missing_new),
        "missing_in_today": len(missing_today),
        "codes": {
            "value_differs": _tally(differs),
            "missing_in_new": _tally(missing_new),
            "missing_in_today": _tally(missing_today),
        },
    }


def _totals(pages: list[dict]) -> dict:
    return {k: sum(p[k] for p in pages) for k in PAGE_KEYS}


def compare_one(drive, d: dict, vocab, *, reader=None, parallel: int = 1) -> dict:
    """Blocking. Read one curated document again and compare (see module doc).

    ``d``: id, filename, gdrive_id, mime_type, structured_metadata (a
    ``curated_reocr_candidates`` row). ``drive`` is only ever asked to
    ``download``. ``reader`` defaults to ``reocr.reread_isolated`` (looked
    up at call time) with re-OCR settings: two passes, ``purpose="reocr"``."""
    reader = reader or reocr.reread_isolated
    cur = (d.get("structured_metadata") or {}).get("curated") or {}
    res: dict = {"document_id": d["id"], "result": "compared", "error": None}
    raw_side = drive.download(cur.get("sidecar_id") or "")
    source = drive.download(d["gdrive_id"])
    sha = hashlib.sha256(raw_side).hexdigest()
    md5 = hashlib.md5(source).hexdigest()  # noqa: S324 (Drive's own checksum)
    res["sidecar_matches_index"] = bool(cur.get("sidecar_sha")) and sha == cur.get("sidecar_sha")
    res["source_matches_index"] = (md5 == cur["source_md5"]) if cur.get("source_md5") else None
    today = raw_side.decode("utf-8", errors="replace")
    rr = reader(d["filename"], source, d.get("mime_type") or "", passes=2, doc_ref=d["id"],
                parallel=parallel, purpose="reocr")  # fmt: skip
    summary, warnings = reocr.report_usage(d["id"], getattr(rr, "llm_passes", None) or [])
    res.update(summary, warnings=warnings)  # #884: per-pass records (counts, sizes, sha)
    if rr.error or not any((p or "").strip() for p in rr.pages):
        return {**res, "result": "failed", "error": rr.error or "no text"}
    _, today_pages = parse_sidecar(today)
    pages = compare_pages(today_pages, list(rr.pages))
    # the new reading as a sidecar (same front matter: doc date and type), in
    # memory only, so both sides are parsed exactly alike
    new_text = reocr.rewrite_sidecar(today, rr, previous_engine="", now=datetime.now(UTC))
    res.update(
        method=rr.method, engine=rr.engine, dpi=list(rr.dpi), seconds=rr.seconds,
        pages_today=len(today_pages), pages_new=len(rr.pages),
        flagged_lines=rr.flagged_lines, handwritten=bool(rr.handwritten),
        totals=_totals(pages), pages=pages, facts=compare_facts(today, new_text, vocab),
    )  # fmt: skip
    return res
