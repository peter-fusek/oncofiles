"""Second-pass local re-OCR of uncertain transcriptions (#660, benchmark #658).

Documents whose sidecar text did not come from our local pipeline (Cowork-era
tesseract English-only, cloud vision) or that carry ``[?]`` markers are read
again from the **source file**, on the **local** model only:

* a PDF with a real text layer → that text, page by page (exact, no model);
* otherwise two vision passes with high reasoning effort at two resolutions
  (default 150 / 220 dpi; REOCR_DPI / REOCR_SECOND_PASS_DPI for this rewrite
  path, REREAD_DPI / REREAD_SECOND_PASS_DPI for the verify-only re-read).
  A line of the first pass whose numbers are not all found on the same page of
  the second pass is marked ``[?]``. Digits are accepted only where both
  passes agree; the parser keeps ``[?]`` lines unverified.

Guard: the old and the new text are both parsed; when the new one loses a
code with its value, or dates it differently, the sidecar is NOT rewritten
(result ``review``, the lost values listed).

Writing (never in a dry run): the old sidecar is first copied to the logs
folder (``_reocr-backup-<date>-<time>-<sidecar>``), then the sidecar is updated in
place (same file id, read-back verified by ``drive_ops``). Nothing is deleted;
the front matter is kept, only the extraction keys change.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Collection
from dataclasses import asdict, dataclass, field
from datetime import datetime

from oncofiles import config
from oncofiles.curated import extract as extract_mod
from oncofiles.curated import llm

logger = logging.getLogger(__name__)

LOCAL_ENGINE_KEYS = ("qwen", "local", "google-export", "utf-8", "pymupdf")
REOCR_BUDGET = 16_000
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_FRONT = re.compile(r"^---\n(.*?)\n---\n?", re.S)

REOCR_SYSTEM_PROMPT = extract_mod.OCR_SYSTEM_PROMPT + (
    "\n\nThis is a careful second reading. Take your time on every digit, decimal "
    "comma and unit. Keep each laboratory result on ONE line: test name, value, "
    "unit, reference range, flag. Examples:\n"
    "Leukocyty 6,37 G/l 4,0 - 11\n"
    "CEA 12,6 ug/l < 5,0 H\n"
    "Odber: 14.03.2027 07:40\n"
    "If a digit is not clearly legible, write the value followed by [?]."
)


def is_local_engine(engine: str | None) -> bool:
    return any(k in (engine or "").lower() for k in LOCAL_ENGINE_KEYS)


def needs_reocr(meta: dict, pages: list[str]) -> list[str]:
    """Why a document should be read again (empty list: it should not)."""
    reasons = []
    engine = str(meta.get("ocr_engine") or meta.get("extraction") or "")
    if not is_local_engine(engine):
        reasons.append("cowork_ocr")
    if any("[?]" in (p or "") for p in pages):
        reasons.append("ocr_uncertain")
    return reasons


def reconcile(first: list[str], second: list[str | None]) -> tuple[list[str], int]:
    """Mark first-pass lines whose numbers the second pass does not confirm.

    Comparison is per page and by number tokens (order-independent), so a
    re-wrapped line still matches; a digit read differently does not.
    ``None`` for a page = its second pass was skipped on purpose (#593
    adaptive); its lines are not compared and gain no marker."""
    out, flagged = [], 0
    for i, page in enumerate(first):
        other = second[i] if i < len(second) else ""
        if other is None:
            out.append("\n".join(page.splitlines()))
            continue
        pool = _NUM.findall(other)
        lines = []
        for line in page.splitlines():
            nums = _NUM.findall(line)
            missing = [n for n in nums if n not in pool]
            if missing and "[?]" not in line:
                line, flagged = f"{line} [?]", flagged + 1
            lines.append(line)
        out.append("\n".join(lines))
    return out, flagged


def _page_count(content: bytes, ext: str) -> int:
    import pymupdf

    with pymupdf.open(stream=content, filetype=ext) as doc:
        return doc.page_count if ext == "pdf" else 1


def born_digital(page, *, image_cover: float = 0.8) -> bool:
    """Is this PDF page's text its own (born-digital), not OCR laid over a scan?
    False when any text is invisible (render mode 3 or opacity 0, how OCR layers are added)
    or a raster image covers most of the page."""
    try:
        if any(span.get("type") == 3 or span.get("opacity", 1) == 0
               for span in page.get_texttrace()):  # fmt: skip
            return False
        area = abs(page.rect.width * page.rect.height) or 1.0
        for info in page.get_image_info():
            x0, y0, x1, y1 = info.get("bbox") or (0, 0, 0, 0)
            if abs((x1 - x0) * (y1 - y0)) / area >= image_cover:
                return False
    except Exception:  # unreadable structure: do not trust the layer
        return False
    return True


def _pass_record(page: int, k: int, dpi: int, info: dict, seconds: float, error=None) -> dict:
    """Usage of one vision pass (#800): counts only. ``info`` also holds the raw
    answer channels; they are never copied here."""

    def count(key):
        value = info.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    finish = info.get("finish_reason")
    rec = {
        "page": page, "pass": k + 1, "dpi": int(dpi),
        "finish": finish if isinstance(finish, str) else None,
        "prompt_tokens": count("prompt_tokens"),
        "completion_tokens": count("completion_tokens"),
        "reasoning_tokens": count("reasoning_tokens"),
        "seconds": round(seconds, 1),
    }  # fmt: skip
    if error is not None:
        rec["error"] = type(error).__name__  # the class only, never the message
    return rec


def _by_page_and_pass(records: list[dict]) -> list[dict]:
    return sorted(records, key=lambda r: (r["page"], r["pass"]))


def is_truncated(rec: dict) -> bool:
    """A pass the model cut off at its token budget (``finish_reason=length``;
    ``llm.chat`` raises on the same finish when the answer is empty)."""
    return rec.get("finish") == "length"


def usage_summary(records: list[dict]) -> dict:
    """Per-document LLM usage of a re-read (#800): the per-pass records, the
    maxima, the budget and the truncated passes. Counts only."""

    def peak(key):
        values = [r[key] for r in records if isinstance(r.get(key), int)]
        return max(values) if values else None

    return {
        "llm_passes": [dict(r) for r in records],
        "reasoning_tokens_max": peak("reasoning_tokens"),
        "completion_tokens_max": peak("completion_tokens"),
        "budget": REOCR_BUDGET,
        "truncated_passes": [{"page": r["page"], "pass": r["pass"]}
                             for r in records if is_truncated(r)],
    }  # fmt: skip


def truncation_warnings(document_id, truncated: list[dict]) -> list[dict]:
    """One report warning per truncated pass: never a silent pass (#800)."""
    return [{"warning": "reocr_pass_truncated", "document_id": document_id,
             "page": t["page"], "pass": t["pass"]} for t in truncated]  # fmt: skip


def log_passes(doc_ref, records: list[dict]) -> None:
    """One INFO line per vision pass, metadata only (ids, counts, finish).
    Called in the server process: the re-OCR child's logging goes nowhere."""
    for r in records:
        logger.info(
            "re-OCR llm pass: doc=%s page=%s pass=%s dpi=%s finish=%s prompt_tokens=%s "
            "completion_tokens=%s reasoning_tokens=%s budget=%s seconds=%s error=%s",
            doc_ref, r.get("page"), r.get("pass"), r.get("dpi"), r.get("finish"),
            r.get("prompt_tokens"), r.get("completion_tokens"), r.get("reasoning_tokens"),
            REOCR_BUDGET, r.get("seconds"), r.get("error"),
        )  # fmt: skip


def _vision_passes(
    content: bytes, ext: str, dpis: list[int], client, *, parallel: int = 1,
    second_pass_pages: Collection[int] | None = None, adaptive: bool = False,
    usage: list[dict] | None = None,
) -> list[list[str | None]]:  # fmt: skip
    """One answer list per DPI. Memory-bounded (#667): one page is rendered,
    sent and freed at a time. With ``parallel`` ≥ 2 (#593) the passes of that
    one page go to the local model concurrently, each as its own independent
    request (same prompt and settings, no shared state); at most one page's
    PNGs are alive at once. The caller caps ``parallel`` (REOCR_PARALLEL_PASSES
    and the number of running re-read jobs) so re-reads never hold more than
    3 LM Studio slots together.

    Skipping the second pass (#593 adaptive; ``None`` in that page's slot of
    every later pass) happens only where it provably cannot change a
    confirmation (verification never loosens):

    * ``adaptive`` and sequential (``parallel`` 1): a page whose first pass has
      no digit at all. ``reconcile`` compares number tokens only, so a second
      pass cannot mark any line of such a page: the output is the same text.
      Not done in parallel mode, where pass 2 runs alongside pass 1 and
      deciding first would serialise every page.
    * pages outside ``second_pass_pages`` (1-based; ``None`` = every page):
      the verify-only re-read passes the pages a confirmable value can be on
      (``reread_verify.pages_with_candidates``); never a re-read whose text is
      written back. Such a page's own lines confirm nothing, but the fact
      parser couples pages (the inline parser drops a value already seen on
      an earlier page), so its second pass is only *deferred*: when the page
      shares any number with a page that was read twice, it gets its second
      pass after all. See ``_shares_numbers``.

    ``usage`` (optional list) receives one ``_pass_record`` per model call
    (#800), also for a call that raised. Passes may run in the thread pool, so
    records are appended under a lock and sorted by page and pass at the end.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import pymupdf

    lock = threading.Lock()

    def ask(part: dict, i: int, total: int, k: int = 0) -> str:
        info: dict = {}
        t0 = time.monotonic()
        error = None
        try:
            return llm.chat(
                REOCR_SYSTEM_PROMPT,
                [{"type": "text", "text": f"Page {i + 1} of {total}."}, part],
                max_tokens=REOCR_BUDGET,
                reasoning_effort="high",
                thinking=False if config.REOCR_THINKING == "off" else None,
                client=client,
                info=info,
            )
        except Exception as exc:
            error = exc
            raise
        finally:
            if usage is not None:
                rec = _pass_record(i + 1, k, dpis[k], info, time.monotonic() - t0, error)
                with lock:
                    usage.append(rec)
            info.clear()  # the raw answer channels never outlive the call

    def render(page, dpi: int) -> dict:
        png = extract_mod._pixmap_png(page, dpi)
        part = llm.image_part(png)
        del png
        return part

    workers = max(1, min(int(parallel), len(dpis)))
    passes: list[list[str | None]] = [[] for _ in dpis]
    deferred: list[int] = []
    with (
        pymupdf.open(stream=content, filetype=ext) as doc,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        total = doc.page_count if ext == "pdf" else 1
        for i in range(total):
            page = doc[i]
            wanted = second_pass_pages is None or (i + 1) in second_pass_pages
            if len(dpis) > 1 and not wanted:
                answers = [ask(render(page, dpis[0]), i, total)]
                deferred.append(i)
            elif adaptive and workers == 1 and len(dpis) > 1:
                answers = [ask(render(page, dpis[0]), i, total)]
                if _NUM.search(answers[0]):  # digits: the second pass decides them
                    answers += [ask(render(page, dpi), i, total, k)
                                for k, dpi in enumerate(dpis[1:], start=1)]  # fmt: skip
            else:
                parts = [render(page, dpi) for dpi in dpis]
                if workers > 1:
                    n = len(parts)
                    answers = list(pool.map(ask, parts, [i] * n, [total] * n, range(n)))
                else:
                    answers = [ask(part, i, total, k) for k, part in enumerate(parts)]
                del parts
            for k, out in enumerate(passes):
                out.append(answers[k] if k < len(answers) else None)
            del page
        twice = [a for i, a in enumerate(passes[0]) if passes[-1][i] is not None]
        for i in deferred:
            if _shares_numbers(passes[0][i] or "", twice):
                page = doc[i]
                for k, dpi in enumerate(dpis[1:], start=1):
                    passes[k][i] = ask(render(page, dpi), i, total, k)
                del page
    if usage is not None:
        usage[:] = _by_page_and_pass(usage)
    return passes


def _numbers(text: str) -> set[float]:
    return {float(t.replace(",", ".")) for t in _NUM.findall(text)}


def _shares_numbers(once: str, twice: list[str]) -> bool:
    """Exactness guard of a deferred second pass (#593 adaptive).

    A page read once differs from its 2-pass text only by ``[?]`` markers on
    lines with digits, which a parser may read differently. Facts of another
    page depend on it only through (a) the parser chosen for the document and
    (b) the inline parser's "same value already seen" drop. (a) cannot
    loosen: a table row's existence is the same with or without the marker
    (Bory and OKB rows; ``test_markers_never_create_table_rows``) or only
    shrinks with it (markdown rows), so a page read once can only push the
    choice to a parser that yields nothing on the confirmable pages. (b)
    needs the same value on both pages: a value is a number printed on its
    page. So when this page shares no number with any page read twice, its
    second pass cannot change what is confirmed; otherwise it is read twice."""
    if not _NUM.search(once):
        return False  # no digit: no marker and no value either way
    seen: set[float] = set()
    for text in twice:
        seen |= _numbers(text or "")
    return bool(_numbers(once) & seen)


def _strip_handwritten(answers: list[str]) -> tuple[list[str], bool]:
    pages, handwritten = [], False
    for answer in answers:
        if answer.lower().startswith("[handwritten]"):
            handwritten = True
            answer = answer.split("\n", 1)[1] if "\n" in answer else ""
        pages.append(answer.strip())
    return pages, handwritten


@dataclass
class Reread:
    pages: list[str]
    method: str  # text_layer (born-digital only) | vision2 | plain
    engine: str
    handwritten: bool = False
    flagged_lines: int = 0
    error: str | None = None
    seconds: float = 0.0  # vision time, for #593 measurements
    parallel: int = 1
    # #593 A/B: render dpi per pass, vision passes per page, pages read once
    dpi: list[int] = field(default_factory=list)
    passes_per_page: list[int] = field(default_factory=list)
    skipped_second_pass_pages: list[int] = field(default_factory=list)
    # #800: one record per vision pass (page, pass, dpi, finish, token counts,
    # seconds); counts only, never the answer
    llm_passes: list[dict] = field(default_factory=list)


def reocr_dpis(passes: int, purpose: str = "reocr") -> list[int]:
    """Render dpi of each vision pass. ``purpose`` "reocr" (the rewrite path,
    REOCR_DPI / REOCR_SECOND_PASS_DPI) or "reread" (verify-only, REREAD_DPI /
    REREAD_SECOND_PASS_DPI); the two never affect each other."""
    if purpose == "reread":
        first, second = config.REREAD_DPI, config.REREAD_SECOND_PASS_DPI
    elif purpose == "reocr":
        first, second = config.REOCR_DPI, config.REOCR_SECOND_PASS_DPI
    else:
        raise ValueError(f"unknown re-read purpose {purpose!r}")
    return [int(first)] + ([int(second)] if passes >= 2 else [])


def reread(
    name: str, content: bytes, mime: str = "", *, passes: int = 2, doc_ref: object = None,
    parallel: int = 1, second_pass_pages: Collection[int] | None = None,
    purpose: str = "reocr",
) -> Reread:  # fmt: skip
    """Read a source again, locally. Never raises. ``doc_ref`` (a document id)
    is what logs show: filenames can carry names or diagnoses.
    ``second_pass_pages`` (1-based, ``None`` = all): see ``_vision_passes``;
    only the verify-only re-read passes it, and only in adaptive mode.
    ``purpose``: "reocr" (default, text written back) or "reread" (verify-only);
    it picks the dpi settings (``reocr_dpis``)."""
    ext = extract_mod._ext(name)
    usage: list[dict] = []  # per-pass LLM usage (#800), kept also when a pass fails
    try:
        if ext in extract_mod.TEXT_EXTS:
            return Reread([content.decode("utf-8", errors="replace")], "plain", "utf-8")
        if ext == "pdf":
            import pymupdf

            with pymupdf.open(stream=content, filetype="pdf") as doc:
                layer = [p.get_text() for p in doc]
                digital = all(born_digital(p) for p in doc)
            # Only a born-digital text layer is the document's own text. An OCR'd
            # layer (invisible text over a scan) is OCR of unknown quality, maybe
            # the very text the old transcription copied: read the image instead.
            if digital and len("".join(layer).strip()) >= 100:
                return Reread([t.strip() for t in layer], "text_layer", "pymupdf")
        elif ext not in extract_mod.IMAGE_EXTS:
            return Reread([], "none", "-", error=f"unsupported file type .{ext}")
        if _page_count(content, ext) > extract_mod.MAX_VISION_PAGES:
            return Reread([], "none", "-", error="too many pages for vision")
        import httpx

        dpis = reocr_dpis(passes, purpose)
        adaptive = config.REOCR_PASSES_MODE == "adaptive"
        t0 = time.monotonic()
        with httpx.Client(timeout=config.LOCAL_LLM_TIMEOUT_S) as client:  # one per document
            answers = _vision_passes(
                content, ext, dpis, client, parallel=parallel, adaptive=adaptive,
                second_pass_pages=second_pass_pages if adaptive else None, usage=usage,
            )  # fmt: skip
        seconds = round(time.monotonic() - t0, 1)
        first, hand = _strip_handwritten(answers[0])
        flagged, per_page, skipped = 0, [1] * len(first), []
        if passes >= 2:
            stripped, _ = _strip_handwritten([a or "" for a in answers[1]])
            second = [None if a is None else s for a, s in zip(answers[1], stripped, strict=True)]
            first, flagged = reconcile(first, second)
            per_page = [1 if s is None else 2 for s in second]
            skipped = [i for i, s in enumerate(second, start=1) if s is None]
        return Reread(first, "vision2" if passes >= 2 else "vision", llm.model_id(), hand, flagged,
                      seconds=seconds, parallel=parallel, dpi=dpis, passes_per_page=per_page,
                      skipped_second_pass_pages=skipped, llm_passes=usage)  # fmt: skip
    except llm.LocalLLMError as exc:
        return Reread([], "none", llm.model_id(), error=str(exc),
                      llm_passes=_by_page_and_pass(usage))  # fmt: skip
    except Exception as exc:  # corrupt file, codec, …
        logger.warning("re-OCR failed for doc %s", doc_ref, exc_info=True)
        return Reread([], "none", "-", error=type(exc).__name__,
                      llm_passes=_by_page_and_pass(usage))  # fmt: skip


def reread_isolated(
    name: str, content: bytes, mime: str = "", *, passes: int = 2, doc_ref: object = None,
    parallel: int = 1, second_pass_pages: Collection[int] | None = None,
    purpose: str = "reocr",
) -> Reread:  # fmt: skip
    """``reread`` in a short-lived child process (#667).

    PyMuPDF buffers, PNGs and allocator fragmentation die with the process, so
    the server's RSS returns to its baseline after every document. The child
    (``reocr_child``) gets only an allowlisted environment, never a secret, and
    reads inside ``local_only``. Never raises: a failed child is a failed
    document, not a failed job."""
    import json as _json
    import subprocess
    import sys

    from oncofiles.curated import reocr_child

    header = (
        _json.dumps(
            {
                "name": name,
                "mime": mime,
                "passes": passes,
                "doc_ref": doc_ref,
                "parallel": parallel,
                "purpose": purpose,
                "second_pass_pages": (
                    None if second_pass_pages is None else sorted(second_pass_pages)
                ),
            },
            default=str,  # fmt: skip
        ).encode()
        + b"\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "oncofiles.curated.reocr_child"],
            input=header + content,
            capture_output=True,
            env=reocr_child.child_env(),
            check=False,
        )
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:]
            logger.warning("re-OCR child failed for doc %s: %s", doc_ref, tail)
            return Reread([], "none", "-", error=f"worker: exit {proc.returncode}")
        out = _json.loads(proc.stdout)
    except Exception as exc:  # spawn failure, bad JSON, …
        logger.warning("re-OCR child failed for doc %s", doc_ref, exc_info=True)
        return Reread([], "none", "-", error=f"worker: {type(exc).__name__}")
    peak = out.pop("peak_rss_mb", None)
    logger.info("re-OCR child for doc %s: peak RSS %s MB", doc_ref, peak)  # sizing #667
    return Reread(**out)


def rewrite_sidecar(old_text: str, rr: Reread, *, previous_engine: str, now: datetime) -> str:
    """Same front matter with updated extraction keys; the body is the new text."""
    m = _FRONT.match(old_text)
    lines = m.group(1).splitlines() if m else []
    updates = {
        "extraction": rr.method,
        "ocr_engine": rr.engine,
        "handwritten": "true" if rr.handwritten else "false",
        "extracted_at": now.date().isoformat(),
        "reocr_at": now.date().isoformat(),
        "reocr_previous_engine": previous_engine or "unknown",
        "reocr_uncertain_lines": str(rr.flagged_lines),
    }
    out, seen = [], set()
    for line in lines:
        key = line.split(":", 1)[0].strip() if ":" in line and not line.startswith(" ") else None
        if key in updates:
            out.append(f"{key}: {updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}: {v}" for k, v in updates.items() if k not in seen]
    body = "\n\n".join(f"<!-- page {i} -->\n{p}" for i, p in enumerate(rr.pages, start=1))
    return "---\n" + "\n".join(out) + "\n---\n\n" + body.strip() + "\n"


@dataclass
class DocResult:
    document_id: int
    filename: str
    reasons: list[str]
    result: str = "pending"  # previewed | rewritten | review | failed | skipped
    method: str | None = None
    engine: str | None = None
    pages: int = 0
    uncertain_before: int = 0
    uncertain_after: int = 0
    facts_machine: int = 0
    facts_unverified: int = 0
    backup: str | None = None
    lost: list[str] = field(default_factory=list)  # "CODE value date" the old text had
    error: str | None = None
    dpi: list[int] = field(default_factory=list)  # #593 A/B
    passes_per_page: list[int] = field(default_factory=list)
    skipped_second_pass_pages: list[int] = field(default_factory=list)
    # #800: per-pass LLM usage (``usage_summary``); counts only
    llm_passes: list[dict] = field(default_factory=list)
    reasoning_tokens_max: int | None = None
    completion_tokens_max: int | None = None
    budget: int = REOCR_BUDGET
    truncated_passes: list[dict] = field(default_factory=list)  # {"page", "pass"}


@dataclass
class ReocrReport:
    dry_run: bool
    documents: list[DocResult] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)  # #800: reocr_pass_truncated

    def to_dict(self) -> dict:
        return {"dry_run": self.dry_run, "documents": [asdict(d) for d in self.documents],
                "warnings": [dict(w) for w in self.warnings]}  # fmt: skip


from oncofiles.curated.facts_rules import INLINE_CATEGORIES as _INLINE_CATEGORIES  # noqa: E402


def _facts_of(text: str, vocab) -> set[tuple]:
    """(code, value, day) of every coded fact a sidecar text yields."""
    from oncofiles.curated.facts_rules import extract_lab_facts
    from oncofiles.curated.indexer import category_for, parse_sidecar

    front, pages = parse_sidecar(text)
    day = str(front.get("doc_date") or "")[:10] or None
    category = category_for(str(front.get("doc_type") or "other")).value
    facts, _ = extract_lab_facts(pages, vocab, report_date=day,
                                 inline=category in _INLINE_CATEGORIES)  # fmt: skip
    return {
        (f["code"], f["value_num"] if f["value_num"] is not None else f["value_text"],
         (f["occurred_at"] or day or "")[:10])
        for f in facts if f.get("code")
    }  # fmt: skip


def lost_values(old_text: str, new_text: str, vocab) -> list[str]:
    """What the new transcription would silently drop (#660 guard): a code with
    its value the old text had, missing from the new one, or present with a
    different date. A re-OCR that loses anything is not written."""
    old, new = _facts_of(old_text, vocab), _facts_of(new_text, vocab)
    new_cv = {(c, v): d for c, v, d in new}
    out = []
    for c, v, d in sorted(old, key=str):
        if (c, v) not in new_cv:
            out.append(f"{c} {v} missing")
        elif new_cv[(c, v)] != d:
            out.append(f"{c} {v} re-dated {d}->{new_cv[(c, v)]}")
    return out


def handled_before(log_names: list[str], filename: str) -> bool:
    """Does the logs folder show this document was re-OCR'd before (a backup)
    or deliberately restored (the kept re-OCR text)? (#674)"""
    from oncofiles.curated.runner import nfc

    name = re.escape(nfc(filename))
    exact = re.compile(rf"_reocr-(?:backup|text)-\d{{8}}(?:-\d{{6}})?-{name}\.md")
    return any(exact.fullmatch(nfc(n)) for n in log_names)


def run_reocr(
    drive,
    root_id: str,
    docs: list[dict],
    vocab,
    *,
    dry_run: bool = True,
    passes: int = 2,
    now: datetime,
    stop=lambda: False,
    reader=None,
    rss_limit_mb: float | None = None,
    rss_mb=None,
) -> ReocrReport:
    """Blocking. ``docs``: document_id, filename, gdrive_id, sidecar_id, mime,
    engine, reasons. Writes only when ``dry_run`` is False (caller holds
    ``contract_writes()``); one document at a time, backup before update.
    ``reader`` reads a source (``reread_isolated`` in production, #667). With
    ``rss_limit_mb``, no document is started while RSS is above it."""
    import gc

    reader = reader or reread  # looked up at call time (tests patch reread)
    if rss_mb is None:
        from oncofiles.memory import get_rss_mb as rss_mb
    from oncofiles.curated.contract import parse_contract
    from oncofiles.curated.facts_rules import extract_lab_facts
    from oncofiles.curated.runner import nfc, walk

    report = ReocrReport(dry_run=dry_run)
    tree = walk(drive, root_id)
    readme = next((i for i in tree.root_files if nfc(i.name) == "README.md"), None)
    if readme is None:
        raise RuntimeError("not a curated folder (no README.md)")
    contract = parse_contract(drive.download(readme.id).decode("utf-8"))
    logs = tree.folders.get(contract.logs)
    if logs is None:
        raise RuntimeError("the contract's logs folder is missing")
    logs_id = logs.id
    log_names = [nfc(i.name) for i in drive.list_children(logs_id)]
    for d in docs:
        res = DocResult(d["document_id"], d["filename"], list(d.get("reasons") or []))
        report.documents.append(res)
        if stop():
            res.result, res.error = "skipped", "stopping"
            continue
        if not d.get("explicit") and handled_before(log_names, d["filename"]):
            # #674: re-OCR'd before, or its old sidecar was restored on purpose
            res.result, res.error = "skipped", "re-OCR'd or restored before; list it explicitly"
            continue
        if rss_limit_mb and (now_rss := rss_mb()) > rss_limit_mb:
            res.result, res.error = "skipped", f"rss {now_rss:.0f} MB > {rss_limit_mb:.0f} MB"
            continue
        try:
            old = drive.download(d["sidecar_id"]).decode("utf-8", errors="replace")
            res.uncertain_before = old.count("[?]")
            rr = reader(d["filename"], drive.download(d["gdrive_id"]), d.get("mime") or "",
                        passes=passes, doc_ref=d["document_id"])  # fmt: skip
            # #800: usage is reported also for a failed read; report only, the
            # rewrite/review decision below is unchanged
            log_passes(d["document_id"], rr.llm_passes)
            for key, value in usage_summary(rr.llm_passes).items():
                setattr(res, key, value)
            report.warnings += truncation_warnings(d["document_id"], res.truncated_passes)
            if rr.error or not any(p.strip() for p in rr.pages):
                res.result, res.error = "failed", rr.error or "no text"
                continue
            res.method, res.engine, res.pages = rr.method, rr.engine, len(rr.pages)
            res.dpi, res.passes_per_page = rr.dpi, rr.passes_per_page
            res.skipped_second_pass_pages = rr.skipped_second_pass_pages
            new = rewrite_sidecar(old, rr, previous_engine=d.get("engine") or "", now=now)
            res.uncertain_after = new.count("[?]")
            res.lost = lost_values(old, new, vocab)
            facts, _ = extract_lab_facts(rr.pages, vocab, unreliable=rr.handwritten, inline=True)
            coded = [f for f in facts if f.get("code")]
            res.facts_machine = sum(1 for f in coded if f["verification"] == "machine")
            res.facts_unverified = len(coded) - res.facts_machine
            if dry_run:
                res.result = "previewed"
                continue
            if res.lost:  # never shrink the record silently: a person decides
                res.result = "review"
                continue
            # time to the second (#674): a same-day retry gets its own backup;
            # create_text still refuses an existing name, it never overwrites
            name = f"_reocr-backup-{now:%Y%m%d-%H%M%S}-{d['filename']}.md"
            res.backup = drive.create_text(logs_id, name, old)  # before any change
            drive.update_text(d["sidecar_id"], new)
            res.result = "rewritten"
        except Exception as exc:
            logger.exception("re-OCR of doc %s failed", d.get("document_id"))
            res.result, res.error = "failed", type(exc).__name__
        finally:
            gc.collect()
    return report
