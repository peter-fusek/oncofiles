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
(result ``review``, the codes of the lost values listed, never the values,
#805). A second, generic guard (``token_loss`` / ``near_empty_pages``)
compares every printed number and date, not only lab-vocabulary values: a
lost date, more than ``REVIEW_MISSING_NUMBERS`` lost numbers, or a near-empty page together with
any lost number or a body shrunk below ``NEAR_EMPTY_CHARS_RATIO`` also means
``review`` (reason codes in ``review_reasons``; counts only, never tokens).
#806 adds, stricter only: a body below ``TEXT_SHRUNK_RATIO`` of the old one
(``text_shrunk``) and a share of the old distinct words found in the new text
below ``MIN_WORD_OVERLAP`` (``low_word_overlap``, reported as ``word_overlap``).

Near-empty pages (#806): a vision pass that returns less than
``NEAR_EMPTY_CHARS`` while more than ``RETRY_REASONING_RATIO`` of its
completion tokens were reasoning is retried ONCE for that page with thinking
off (the per-call reasoning marker, same dpi and prompt); the retry's text is
kept only when longer, both attempts are in ``llm_passes`` and the page in
``retried_pages``. The model's reasoning channel is never read as a
transcription (infra decision on #806). Every rendered page gets a cheap ink
density (``page_ink``: share of non-near-white pixels at ``INK_DPI``) and,
for a PDF, whether it has a text layer. A near-empty page with ink below
``BLANK_INK_MAX`` and no text layer is not retried; it is a blank back side,
not counted by ``near_empty_with_loss``, only when the OLD sidecar has page
markers and its text of that same page was near-empty too (a blank never
loosens the guard). A retried pass's kept text goes through the same
reconcile and loss guard as any first pass.

Writing (never in a dry run): the old sidecar is first copied to the logs
folder (``_reocr-backup-<date>-<time>-<sidecar>``), then the sidecar is updated in
place (same file id, read-back verified by ``drive_ops``). Nothing is deleted;
the front matter is kept, only the extraction keys change.

``review`` in a real run (maintainer decision 2026-11-15): the sidecar stays
as it is and the new transcription is kept NEXT TO it, as
``_reocr-candidate-<date>-<time>-<source filename>.md`` in the logs folder
(``candidate_text``: a header block saying what it is, then the sidecar text
a rewrite would have written). No guard is loosened, nothing is re-indexed;
an admin promotes a candidate with ``restore_sidecar``.

Tesseract baseline profile (#831, maintainer decision 2026-11-16): when the
OLD sidecar's front matter says ``ocr_engine: tesseract…`` its text is not a
trustworthy baseline (it drops decimal commas and garbles labels), so
``lost_values``, ``missing_numbers`` and ``low_word_overlap`` are still
computed but only reported (``baseline_untrusted``), not blocking.
``text_shrunk``, ``missing_dates`` and the new ``uncertain_share_high``
(numbers on ``[?]`` lines above ``UNCERTAIN_SHARE_MAX``) still mean
``review``. A near-empty page is judged by its own ink there, not by numbers
the old text had: ink below ``BLANK_INK_MAX`` and no text layer = a blank
back side (no page markers in the old sidecar needed); otherwise
``near_empty_page_with_ink`` (or ``near_empty_unknown_ink`` when the ink was
not measured) means ``review``; ``near_empty_with_loss`` is only reported.
A lost code still stops the rewrite there when the document has an active
stored fact with that code that is ``machine``, ``verified`` or ``corrected``
(``lost_trusted_values``; ``lost_trusted_values_unknown`` when the caller
could not look the stored facts up): such a value is trusted evidence.
Any other engine keeps the rules above unchanged. Off-switch: ``REOCR_TESSERACT_PROFILE=false``.
``promote_candidates`` re-evaluates kept candidates of Tesseract-era sidecars
under the profile without reading the scan again.
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

# Generic loss guard (#660 follow-up): thresholds of the review decision
REVIEW_MISSING_NUMBERS = 2  # more than this many lost numbers → review
REVIEW_MISSING_DATES = 0  # more than this many lost dates → review
NEAR_EMPTY_CHARS = 50  # a page with fewer stripped characters is near-empty
NEAR_EMPTY_CHARS_RATIO = 0.8  # near-empty page + body below this share of the old → review
# #806 (thresholds may only become stricter)
TEXT_SHRUNK_RATIO = 0.6  # new body below this share of the old body → review (text_shrunk)
MIN_WORD_OVERLAP = 0.40  # share of old distinct words found in the new text below this → review
WORD_MIN_LEN = 3  # words for the overlap: lower-case, letters only, diacritics kept, ≥ this
RETRY_REASONING_RATIO = 0.8  # near-empty pass with reasoning/completion above this → 1 retry
INK_DPI = 36  # render dpi of the ink-density measure (cheap, never sent to the model)
INK_NEAR_WHITE = 230  # grey level (0-255) at or above which a pixel counts as paper
BLANK_INK_MAX = 0.005  # a page with less ink (share of pixels) and no text layer is blank
# #831: guard profile for a Tesseract-era OLD sidecar (its text is not a baseline)
TESSERACT_PROFILE = "tesseract_baseline"
# computed and reported under the profile, but they do not stop a rewrite
BASELINE_UNTRUSTED_REASONS = (
    "lost_values", "missing_numbers", "low_word_overlap",
    # leans on the old text's numbers; the profile judges a near-empty page by its ink
    "near_empty_with_loss",
)  # fmt: skip
# profile only: share of the new text's numeric tokens that sit on a ``[?]`` line
# above this → review (uncertain_share_high)
UNCERTAIN_SHARE_MAX = 0.15
REPLACED_ENGINE_MAX = 40  # characters of the old engine kept as ``replaced_engine``
PROMOTE_MAX_DOCS = 20  # candidates evaluated per promote_candidates call
_WORD = re.compile(r"[^\W\d_]+")
_DATE_TOKEN = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.\s?(?:\d{4}|\d{2})\b|\b\d{4}-\d{2}-\d{2}\b")
_NUM_TOKEN = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w])")
_PAGE_MARKER = re.compile(r"<!--\s*page\s+\d+\s*-->")

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


def _pass_record(
    page: int, k: int, dpi: int | None, info: dict, seconds: float, error=None
) -> dict:
    """Usage of one vision pass (#800): counts only. ``info`` also holds the raw
    answer channels; they are never copied here."""

    def count(key):
        value = info.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    finish = info.get("finish_reason")
    rec = {
        "page": page, "pass": k + 1, "dpi": dpi if isinstance(dpi, int) else None,
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
    return sorted(
        records, key=lambda r: (r.get("page") or 0, r.get("pass") or 0, bool(r.get("retry")))
    )


def page_ink(page, *, dpi: int = INK_DPI) -> float:
    """Share of non-near-white pixels of a page rendered in grey at ``dpi``
    (#806). A number only: nothing of the page leaves this function."""
    import pymupdf

    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    samples = pix.samples
    total = pix.width * pix.height
    del pix
    if not total:
        return 0.0
    paper = bytes(range(INK_NEAR_WHITE, 256))
    return len(samples.translate(None, paper)) / total


def is_blank(ink: float | None, text_layer: bool | None) -> bool:
    """A likely blank back side: measured ink below ``BLANK_INK_MAX`` and no
    text layer. Unknown ink (not measured) is never blank."""
    return isinstance(ink, int | float) and ink < BLANK_INK_MAX and not text_layer


def needs_retry(text: str | None, reasoning_tokens, completion_tokens) -> bool:
    """A near-empty answer from a pass that spent nearly all its tokens on
    reasoning (#806). Unknown counts never trigger a retry."""
    if len((text or "").strip()) >= NEAR_EMPTY_CHARS:
        return False
    if not all(isinstance(v, int) and not isinstance(v, bool)
               for v in (reasoning_tokens, completion_tokens)):  # fmt: skip
        return False
    return completion_tokens > 0 and reasoning_tokens / completion_tokens > RETRY_REASONING_RATIO


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
        if r.get("retry"):
            logger.info("re-OCR llm pass retried: doc=%s page=%s pass=%s thinking=%s",
                        doc_ref, r.get("page"), r.get("pass"), r.get("thinking"))  # fmt: skip


def report_usage(doc_ref, records: list[dict]) -> tuple[dict, list[dict]]:
    """Log the passes and build the per-document usage fields and warnings.
    Never raises: reporting must never change a re-OCR outcome. On a failure
    it logs the exception class only and returns empty usage."""
    try:
        log_passes(doc_ref, records)
        summary = usage_summary(records)
        return summary, truncation_warnings(doc_ref, summary["truncated_passes"])
    except Exception as exc:
        logger.warning("re-OCR usage report failed for doc %s: %s", doc_ref, type(exc).__name__)
        return {}, []


def _vision_passes(
    content: bytes, ext: str, dpis: list[int], client, *, parallel: int = 1,
    second_pass_pages: Collection[int] | None = None, adaptive: bool = False,
    usage: list[dict] | None = None, retry_thin: bool = False, diag: dict | None = None,
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

    #806: every page's ink density (and, for a PDF, whether it has a text
    layer) is measured before its passes and put in ``diag`` (``page_ink``,
    ``page_text_layer``; ``retried_pages``). With ``retry_thin`` (the re-OCR
    rewrite path) a pass whose answer is near-empty although nearly all of
    its completion was reasoning (``needs_retry``) is asked once more for that
    page with thinking off (``reasoning_marker=True``), same image and
    prompt; the longer answer stands. A blank page (``is_blank``) is never
    retried. Only ``llm.chat``'s return value (the ``content`` channel) is
    ever used as text; ``info`` is read for token counts only.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import pymupdf

    lock = threading.Lock()
    blank: dict[int, bool] = {}  # 0-based page -> likely blank back side (#806)
    retried: set[int] = set()  # 1-based pages with a thinking-off retry

    def call(part: dict, i: int, total: int, k: int = 0, retry: bool = False):
        """One model call: (answer text, (reasoning_tokens, completion_tokens))."""
        info: dict = {}
        t0 = time.monotonic()
        error = None
        if retry:  # #806: thinking off through the per-call marker, nothing global
            mode = {"thinking": False, "reasoning_marker": True}
        else:
            mode = {"thinking": False if config.REOCR_THINKING == "off" else None}
        try:
            text = llm.chat(
                REOCR_SYSTEM_PROMPT,
                [{"type": "text", "text": f"Page {i + 1} of {total}."}, part],
                max_tokens=REOCR_BUDGET,
                reasoning_effort="high",
                client=client,
                info=info,
                **mode,
            )
            return text, (info.get("reasoning_tokens"), info.get("completion_tokens"))
        except Exception as exc:
            error = exc
            raise
        finally:
            if usage is not None:
                # Reporting must never break the read: a recording failure is
                # logged (class name only) and the pass result stands.
                try:
                    dpi = dpis[k] if k < len(dpis) else None
                    rec = _pass_record(i + 1, k, dpi, info, time.monotonic() - t0, error)
                    if retry:
                        rec.update(retry=True, thinking="off")
                    with lock:
                        usage.append(rec)
                except Exception as rec_exc:
                    logger.warning("re-OCR pass usage not recorded: %s", type(rec_exc).__name__)
            info.clear()  # the raw answer channels never outlive the call

    def ask(part: dict, i: int, total: int, k: int = 0) -> str:
        text, tokens = call(part, i, total, k)
        if not retry_thin or blank.get(i) or not needs_retry(text, *tokens):
            return text
        with lock:
            retried.add(i + 1)
        try:
            again, _ = call(part, i, total, k, retry=True)
        except Exception as exc:  # the retry is a bonus: its failure keeps the first answer
            logger.warning("re-OCR retry failed: page=%s pass=%s %s", i + 1, k + 1,
                           type(exc).__name__)  # fmt: skip
            return text
        return again if len((again or "").strip()) > len((text or "").strip()) else text

    def render(page, dpi: int) -> dict:
        png = extract_mod._pixmap_png(page, dpi)
        part = llm.image_part(png)
        del png
        return part

    workers = max(1, min(int(parallel), len(dpis)))
    passes: list[list[str | None]] = [[] for _ in dpis]
    deferred: list[int] = []
    inks: list[float | None] = []
    layers: list[bool | None] = []
    with (
        pymupdf.open(stream=content, filetype=ext) as doc,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        total = doc.page_count if ext == "pdf" else 1
        for i in range(total):
            page = doc[i]
            try:  # a measurement only: a failure leaves the page "unknown", never blank
                ink: float | None = round(page_ink(page), 4)
            except Exception as exc:
                logger.warning("re-OCR ink measure failed: page=%s %s", i + 1, type(exc).__name__)
                ink = None
            layer = bool(page.get_text().strip()) if ext == "pdf" else None
            inks.append(ink)
            layers.append(layer)
            blank[i] = is_blank(ink, layer)
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
    if diag is not None:
        diag.update(page_ink=inks, page_text_layer=layers, retried_pages=sorted(retried))
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
    # #806: per rendered page; empty when no page was rendered (text layer, plain)
    page_ink: list[float | None] = field(default_factory=list)
    page_text_layer: list[bool | None] = field(default_factory=list)
    retried_pages: list[int] = field(default_factory=list)  # 1-based, thinking-off retry


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
        diag: dict = {}
        with httpx.Client(timeout=config.LOCAL_LLM_TIMEOUT_S) as client:  # one per document
            answers = _vision_passes(
                content, ext, dpis, client, parallel=parallel, adaptive=adaptive,
                second_pass_pages=second_pass_pages if adaptive else None, usage=usage,
                retry_thin=purpose == "reocr", diag=diag,
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
                      skipped_second_pass_pages=skipped, llm_passes=usage,
                      page_ink=diag.get("page_ink", []),
                      page_text_layer=diag.get("page_text_layer", []),
                      retried_pages=diag.get("retried_pages", []))  # fmt: skip
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


def rewrite_sidecar(
    old_text: str, rr: Reread, *, previous_engine: str, now: datetime,
    replaced_engine: str | None = None,
) -> str:  # fmt: skip
    """Same front matter with updated extraction keys; the body is the new text.
    ``replaced_engine`` (#831, Tesseract profile only): the old sidecar's engine,
    recorded short."""
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
    if replaced_engine:
        updates["replaced_engine"] = short_engine(replaced_engine)
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


def _body(text: str) -> str:
    """Sidecar text without its front matter and page markers."""
    return _PAGE_MARKER.sub(" ", _FRONT.sub("", text or "", count=1))


def _date_key(token: str) -> str:
    """One key per printed date: ``5.3.2027``, ``05. 03. 2027`` and
    ``2027-03-05`` are the same day; a 2-digit year stays as printed."""
    t = re.sub(r"\s", "", token)
    if "-" in t:
        y, m, d = t.split("-")
    else:
        d, m, y = t.split(".")
    return f"{int(d)}.{int(m)}.{y}"


def _tokens(text: str) -> tuple[set[str], set[str]]:
    body = _body(text)
    dates = {_date_key(m.group(0)) for m in _DATE_TOKEN.finditer(body)}
    rest = _DATE_TOKEN.sub(" ", body)  # a date never also counts as numbers
    nums = {m.group(0).replace(",", ".") for m in _NUM_TOKEN.finditer(rest)}
    return nums, dates


def _chars(text: str) -> int:
    """Non-whitespace characters of the body (page joins do not count)."""
    return len(re.sub(r"\s", "", _body(text)))


def _words(text: str) -> set[str]:
    """Distinct words of the body: NFC, lower-case, letters only (diacritics
    kept), at least ``WORD_MIN_LEN`` long."""
    import unicodedata

    body = unicodedata.normalize("NFC", _body(text)).lower()
    return {w for w in _WORD.findall(body) if len(w) >= WORD_MIN_LEN}


def word_overlap(old_text: str, new_text: str) -> float | None:
    """Share of the old text's distinct words that the new text still has
    (#806), rounded; ``None`` when the old text has no such word."""
    old = _words(old_text)
    if not old:
        return None
    return round(len(old & _words(new_text)) / len(old), 3)


def token_loss(old_text: str, new_text: str) -> dict:
    """Generic loss check: how many printed numbers and dates of the old text
    the new one no longer has (sets, front matter and page markers ignored),
    the character counts and the word overlap (#806).
    Counts only, never the tokens themselves."""
    old_nums, old_dates = _tokens(old_text)
    new_nums, new_dates = _tokens(new_text)
    return {
        "missing_numbers": len(old_nums - new_nums),
        "missing_dates": len(old_dates - new_dates),
        "old_chars": _chars(old_text),
        "new_chars": _chars(new_text),
        "word_overlap": word_overlap(old_text, new_text),
    }


def near_empty_pages(pages: list[str]) -> list[int]:
    """1-based numbers of pages whose stripped text is shorter than
    ``NEAR_EMPTY_CHARS``."""
    return [i for i, p in enumerate(pages, start=1) if len((p or "").strip()) < NEAR_EMPTY_CHARS]


_PAGE_SPLIT = re.compile(r"<!--\s*page\s+(\d+)\s*-->")


def old_page_chars(old_text: str) -> dict[int, int] | None:
    """Stripped character count per page of the OLD sidecar, by its page
    markers; ``None`` when it has none (Cowork-era sidecars): the old text
    of a single page is then unknown."""
    body = _FRONT.sub("", old_text or "", count=1)
    parts = _PAGE_SPLIT.split(body)
    if len(parts) < 3:
        return None
    out: dict[int, int] = {}
    for num, text in zip(parts[1::2], parts[2::2], strict=True):
        out[int(num)] = out.get(int(num), 0) + len(text.strip())
    return out


def blank_pages(
    empty_pages: list[int], ink: list, text_layer: list, old_chars: dict[int, int] | None
) -> list[int]:
    """The near-empty pages (1-based) that are blank back sides (#806): low
    ink, no text layer AND the old sidecar's text of that same page was
    near-empty too. Without old page markers (``old_chars`` None) or without
    that page in them, no page is blank: a blank never loosens the guard."""
    if old_chars is None:
        return []

    def at(values: list, page: int):
        return values[page - 1] if 0 < page <= len(values) else None

    return [
        p for p in empty_pages
        if p in old_chars and old_chars[p] < NEAR_EMPTY_CHARS
        and is_blank(at(ink, p), at(text_layer, p))
    ]  # fmt: skip


# ── Tesseract baseline profile (#831) ───────────────────────────────────


def sidecar_engine(text: str) -> str:
    """``ocr_engine`` of a sidecar's front matter, unquoted; "" when the text
    has no front matter or no such key."""
    m = _FRONT.match(text or "")
    if not m:
        return ""
    for line in m.group(1).splitlines():
        if line.startswith("ocr_engine:"):
            return line.split(":", 1)[1].strip().strip("\"'").strip()
    return ""


def is_tesseract_engine(engine: str | None) -> bool:
    """Does the engine name start with ``tesseract`` (any case, quoted or not)?"""
    return str(engine or "").strip().strip("\"'").strip().lower().startswith("tesseract")


def short_engine(engine: str) -> str:
    """An engine name as a short, single-line front-matter value."""
    clean = re.sub(r"[^\w .()/+-]", " ", str(engine or ""))
    return re.sub(r"\s+", " ", clean).strip()[:REPLACED_ENGINE_MAX].strip() or "unknown"


def guard_profile(old_text: str) -> str | None:
    """The guard profile of a rewrite, decided by the OLD sidecar alone:
    ``tesseract_baseline`` when its ``ocr_engine`` starts with ``tesseract``
    and ``REOCR_TESSERACT_PROFILE`` is on; None = the standard rules."""
    if not config.REOCR_TESSERACT_PROFILE:
        return None
    return TESSERACT_PROFILE if is_tesseract_engine(sidecar_engine(old_text)) else None


def uncertain_share(text: str) -> float:
    """Share of the numeric tokens of a sidecar body (numbers and dates, every
    occurrence, a date counted once) that sit on a line carrying ``[?]``.
    0.0 for a text without any numeric token. A number only."""
    total = marked = 0
    for line in _body(text).splitlines():
        n = len(_DATE_TOKEN.findall(line)) + len(_NUM_TOKEN.findall(_DATE_TOKEN.sub(" ", line)))
        total += n
        if "[?]" in line:
            marked += n
    return round(marked / total, 3) if total else 0.0


def near_empty_by_ink(
    empty_pages: list[int], ink: Collection | None, text_layer: Collection | None
) -> tuple[list[int], list[str]]:
    """Near-empty pages judged by the page's own ink (#831, Tesseract profile):
    (blank pages, reason codes). A page with measured ink below
    ``BLANK_INK_MAX`` and no text layer is a blank back side (``is_blank``); a
    page with more ink, or with a text layer, visibly has content the model
    did not return (``near_empty_page_with_ink``); a page whose ink is unknown
    (not measured, or a candidate kept without it) is never blank
    (``near_empty_unknown_ink``)."""
    ink, text_layer = list(ink or []), list(text_layer or [])

    def at(values: list, page: int):
        return values[page - 1] if 0 < page <= len(values) else None

    blank, with_ink, unknown = [], False, False
    for page in empty_pages:
        value = at(ink, page)
        if not isinstance(value, int | float) or isinstance(value, bool):
            unknown = True
        elif is_blank(value, at(text_layer, page)):
            blank.append(page)
        else:
            with_ink = True
    reasons = ["near_empty_page_with_ink"] * with_ink + ["near_empty_unknown_ink"] * unknown
    return blank, reasons


def trusted_loss(
    profile: str | None, lost_code_counts: dict[str, int], trusted_codes: Collection | None
) -> tuple[dict[str, int], str | None]:
    """(lost_trusted_codes, blocking reason or None) under the Tesseract
    profile (#831, infra condition): a code the new text loses or re-dates
    (``lost_codes``) still blocks when the document has an active stored fact
    with that code that is machine-verified, verified or corrected
    (``trusted_codes``, loaded by the caller). ``None`` = the stored facts
    could not be looked up: with any lost code the decision fails closed
    (``lost_trusted_values_unknown``). Codes and counts only."""
    if profile != TESSERACT_PROFILE or not lost_code_counts:
        return {}, None
    if trusted_codes is None:
        return {}, "lost_trusted_values_unknown"
    trusted = {str(c) for c in trusted_codes}
    hit = {code: n for code, n in lost_code_counts.items() if code in trusted}
    return hit, ("lost_trusted_values" if hit else None)


def profile_reasons(
    profile: str | None, reasons: list[str], new_text: str, *,
    empty_pages: list[int] | None = None, ink: Collection | None = None,
    text_layer: Collection | None = None, trusted_reason: str | None = None,
) -> tuple[list[str], list[str], float | None]:  # fmt: skip
    """(blocking reasons, baseline_untrusted, uncertain_share) of a decision.
    Without a profile the reasons are returned as they are. Under
    ``tesseract_baseline`` the ``BASELINE_UNTRUSTED_REASONS`` move to the
    second list (reported, not blocking), the near-empty pages are judged by
    their ink (``near_empty_by_ink``) and ``uncertain_share_high`` is added
    when too many of the new text's numbers are uncertain."""
    if profile != TESSERACT_PROFILE:
        return list(reasons), [], None
    untrusted = [r for r in reasons if r in BASELINE_UNTRUSTED_REASONS]
    blocking = [trusted_reason] if trusted_reason else []  # from ``trusted_loss``
    blocking += [r for r in reasons if r not in BASELINE_UNTRUSTED_REASONS]
    blocking += near_empty_by_ink(empty_pages or [], ink, text_layer)[1]
    share = uncertain_share(new_text)
    if share > UNCERTAIN_SHARE_MAX:
        blocking.append("uncertain_share_high")
    return blocking, untrusted, share


def review_reasons(
    lost: list[str], loss: dict, empty_pages: list[int], blank: Collection[int] = ()
) -> list[str]:
    """Reason codes that stop a rewrite (empty list: it may be written).
    A near-empty page alone is not a stop; a blank page (``blank``, #806)
    never counts toward ``near_empty_with_loss``."""
    reasons = []
    if lost:
        reasons.append("lost_values")
    if loss["missing_dates"] > REVIEW_MISSING_DATES:
        reasons.append("missing_dates")
    if loss["missing_numbers"] > REVIEW_MISSING_NUMBERS:
        reasons.append("missing_numbers")
    missed = [p for p in empty_pages if p not in set(blank)]
    if missed and (
        loss["missing_numbers"] > 0
        or loss["new_chars"] < NEAR_EMPTY_CHARS_RATIO * loss["old_chars"]
    ):
        reasons.append("near_empty_with_loss")
    if loss["new_chars"] < TEXT_SHRUNK_RATIO * loss["old_chars"]:
        reasons.append("text_shrunk")
    overlap = loss.get("word_overlap")
    if overlap is not None and overlap < MIN_WORD_OVERLAP:
        reasons.append("low_word_overlap")
    return reasons


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
    # review in a real run: Drive id of the kept new transcription in logs
    # (``_reocr-candidate-…``), or the exception class name when writing it failed
    candidate: str | None = None
    candidate_error: str | None = None
    # #805: codes of values the old text had and the new one drops or re-dates,
    # with how often; never the value or its date (job reports carry codes only)
    lost_codes: dict[str, int] = field(default_factory=dict)
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
    # generic loss guard: counts and page numbers only, never tokens
    missing_numbers: int = 0
    missing_dates: int = 0
    near_empty_pages: list[int] = field(default_factory=list)
    old_chars: int = 0
    new_chars: int = 0
    review_reasons: list[str] = field(default_factory=list)
    decision: str | None = None  # rewrite | review (also on a dry run)
    # #806: rounded floats, page numbers and booleans only, never content
    word_overlap: float | None = None
    page_ink: list[float | None] = field(default_factory=list)
    page_text_layer: list[bool | None] = field(default_factory=list)
    blank_pages: list[int] = field(default_factory=list)
    retried_pages: list[int] = field(default_factory=list)
    # #831: guard profile of the decision (None = standard rules); reason codes
    # computed but not blocking under it; share of numbers on ``[?]`` lines
    profile: str | None = None
    baseline_untrusted: list[str] = field(default_factory=list)
    uncertain_share: float | None = None
    # #831: lost codes that have an active machine/verified/corrected stored fact
    lost_trusted_codes: dict[str, int] = field(default_factory=dict)


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


_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,31}")


def lost_codes(lost: list[str]) -> dict[str, int]:
    """``lost_values`` output reduced to {code: count} for reports (#805).
    Only the leading code token survives; the value and dates never do. A
    token that does not look like a code is counted as ``unknown``."""
    out: dict[str, int] = {}
    for item in lost or []:
        head = str(item).split(" ", 1)[0]
        code = head if _CODE.fullmatch(head) else "unknown"
        out[code] = out.get(code, 0) + 1
    return dict(sorted(out.items()))


def redact_report(report):
    """Read-side redaction of a persisted job report (#805): rows written
    before the fix carry ``lost`` strings ("CODE value missing"); each becomes
    ``lost_codes``. Nothing is migrated or deleted in the DB."""
    if not isinstance(report, dict) or not isinstance(report.get("documents"), list):
        return report
    docs = []
    for d in report["documents"]:
        if isinstance(d, dict) and "lost" in d:
            raw = d.get("lost")
            d = {k: v for k, v in d.items() if k != "lost"}
            d["lost_codes"] = lost_codes(raw if isinstance(raw, list) else [])
        docs.append(d)
    return {**report, "documents": docs}


CANDIDATE_PREFIX = "_reocr-candidate-"
CANDIDATE_KIND = "reocr_candidate"
_CANDIDATE_HEAD = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def candidate_text(new_sidecar: str, doc: dict, res: DocResult, *, passes: int, now) -> str:
    """A re-OCR candidate file: a header block that says what the file is
    (names, engine, dpi, reason CODES; never a value), then the complete
    sidecar text a rewrite would have written, verbatim."""
    import json

    head = [
        f"kind: {CANDIDATE_KIND}",
        f"source_file: {json.dumps(doc['filename'], ensure_ascii=False)}",
        f"document_id: {doc['document_id']}",
        f"method: {res.method or 'unknown'}",
        f"engine: {res.engine or 'unknown'}",
        f"dpi: [{', '.join(str(x) for x in res.dpi)}]",
        f"passes: {passes}",
        f"review_reasons: [{', '.join(res.review_reasons)}]",
        f"created: {now.isoformat()}",
    ]
    if res.page_ink:  # #831: lets a later promotion judge a near-empty page by its ink
        head[-1:-1] = [f"page_ink: {json.dumps(res.page_ink)}",
                       f"page_text_layer: {json.dumps(res.page_text_layer)}"]  # fmt: skip
    if res.profile:  # #831: only under a profile
        head[-1:-1] = [f"profile: {res.profile}",
                       f"baseline_untrusted: [{', '.join(res.baseline_untrusted)}]"]  # fmt: skip
    return "---\n" + "\n".join(head) + "\n---\n" + new_sidecar


def candidate_sidecar_text(text: str) -> str | None:
    """The sidecar text inside a candidate file (its header block removed);
    None when the file does not start with a ``kind: reocr_candidate`` header."""
    m = _CANDIDATE_HEAD.match(text or "")
    if not m or f"kind: {CANDIDATE_KIND}" not in m.group(1).splitlines():
        return None
    rest = text[m.end() :]
    return rest if rest.strip() else None


def candidate_page_ink(text: str) -> tuple[list | None, list | None]:
    """(page_ink, page_text_layer) recorded in a candidate's header block;
    (None, None) for a candidate written before the ink was recorded (#831)
    or with an unreadable value."""
    import json

    m = _CANDIDATE_HEAD.match(text or "")
    found: dict[str, list | None] = {"page_ink": None, "page_text_layer": None}
    for line in m.group(1).splitlines() if m else []:
        key, _, value = line.partition(":")
        if key in found:
            try:
                parsed = json.loads(value.strip())
            except ValueError:
                parsed = None
            found[key] = parsed if isinstance(parsed, list) else None
    if found["page_ink"] is None:
        return None, None
    return found["page_ink"], found["page_text_layer"]


def handled_before(log_names: list[str], filename: str) -> bool:
    """Does the logs folder show this document was re-OCR'd before (a backup),
    deliberately restored (the kept re-OCR text) or sent to review with its
    new text kept (a candidate)? (#674) Automatic selection skips it; an
    explicitly listed document is read again."""
    from oncofiles.curated.runner import nfc

    name = re.escape(nfc(filename))
    exact = re.compile(rf"_reocr-(?:backup|text|candidate)-\d{{8}}(?:-\d{{6}})?-{name}\.md")
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
    engine, reasons, and ``trusted_codes`` (codes of the document's active
    machine/verified/corrected stored facts; missing or None = unknown, which
    fails closed under the Tesseract profile, #831).
    Writes only when ``dry_run`` is False (caller holds
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
            # #674: re-OCR'd before, its old sidecar was restored on purpose, or a
            # review already left a candidate in logs
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
            summary, warns = report_usage(d["document_id"], getattr(rr, "llm_passes", None) or [])
            for key, value in summary.items():
                setattr(res, key, value)
            report.warnings += warns
            if rr.error or not any(p.strip() for p in rr.pages):
                res.result, res.error = "failed", rr.error or "no text"
                continue
            res.method, res.engine, res.pages = rr.method, rr.engine, len(rr.pages)
            res.dpi, res.passes_per_page = rr.dpi, rr.passes_per_page
            res.skipped_second_pass_pages = rr.skipped_second_pass_pages
            res.profile = guard_profile(old)  # #831: decided by the old sidecar alone
            new = rewrite_sidecar(
                old, rr, previous_engine=d.get("engine") or "", now=now,
                replaced_engine=sidecar_engine(old) if res.profile else None,
            )  # fmt: skip
            res.uncertain_after = new.count("[?]")
            lost = lost_values(old, new, vocab)  # internal only: carries values (#805)
            res.lost_codes = lost_codes(lost)
            loss = token_loss(old, new)
            res.missing_numbers, res.missing_dates = loss["missing_numbers"], loss["missing_dates"]
            res.old_chars, res.new_chars = loss["old_chars"], loss["new_chars"]
            res.word_overlap = loss["word_overlap"]
            res.page_ink = list(getattr(rr, "page_ink", None) or [])
            res.page_text_layer = list(getattr(rr, "page_text_layer", None) or [])
            res.retried_pages = list(getattr(rr, "retried_pages", None) or [])
            res.near_empty_pages = near_empty_pages(rr.pages)
            res.blank_pages = blank_pages(res.near_empty_pages, res.page_ink,
                                          res.page_text_layer, old_page_chars(old))  # fmt: skip
            res.lost_trusted_codes, trusted_reason = trusted_loss(
                res.profile, res.lost_codes, d.get("trusted_codes")
            )
            res.review_reasons, res.baseline_untrusted, res.uncertain_share = profile_reasons(
                res.profile,
                review_reasons(lost, loss, res.near_empty_pages, res.blank_pages),
                new, empty_pages=res.near_empty_pages, ink=res.page_ink,
                text_layer=res.page_text_layer, trusted_reason=trusted_reason,
            )  # fmt: skip
            if res.profile:  # #831: blank by the page's own ink, no old page markers needed
                res.blank_pages = near_empty_by_ink(res.near_empty_pages, res.page_ink,
                                                    res.page_text_layer)[0]  # fmt: skip
            res.decision = "review" if res.review_reasons else "rewrite"
            logger.info(
                "re-OCR loss check doc=%s missing_numbers=%d missing_dates=%d "
                "near_empty_pages=%s blank_pages=%s retried_pages=%s old_chars=%d "
                "new_chars=%d word_overlap=%s decision=%s reasons=%s profile=%s "
                "baseline_untrusted=%s uncertain_share=%s",
                d["document_id"], res.missing_numbers, res.missing_dates,
                res.near_empty_pages, res.blank_pages, res.retried_pages, res.old_chars,
                res.new_chars, res.word_overlap, res.decision,
                ",".join(res.review_reasons) or "-", res.profile or "-",
                ",".join(res.baseline_untrusted) or "-", res.uncertain_share,
            )  # fmt: skip
            facts, _ = extract_lab_facts(rr.pages, vocab, unreliable=rr.handwritten, inline=True)
            coded = [f for f in facts if f.get("code")]
            res.facts_machine = sum(1 for f in coded if f["verification"] == "machine")
            res.facts_unverified = len(coded) - res.facts_machine
            if dry_run:
                res.result = "previewed"
                continue
            if res.review_reasons:  # never shrink the record silently: a person decides
                res.result = "review"
                # the sidecar stays; the new text is kept next to it in logs.
                # One attempt, never retried; a failure does not stop the run.
                try:
                    res.candidate = drive.create_text(
                        logs_id,
                        f"{CANDIDATE_PREFIX}{now:%Y%m%d-%H%M%S}-{d['filename']}.md",
                        candidate_text(new, d, res, passes=passes, now=now),
                    )
                except Exception as exc:
                    res.candidate_error = type(exc).__name__
                    logger.warning("re-OCR candidate of doc %s not written: %s",
                                   d["document_id"], res.candidate_error)  # fmt: skip
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


# ── restore a re-OCR backup (#806) ──────────────────────────────────────


class RestoreRefusedError(ValueError):
    """The restore request is not allowed; the message is a reason code."""


def _logs_folder_id(drive, root_id: str) -> str:
    from oncofiles.curated.contract import parse_contract
    from oncofiles.curated.runner import nfc, walk

    tree = walk(drive, root_id)
    readme = next((i for i in tree.root_files if nfc(i.name) == "README.md"), None)
    if readme is None:
        raise RuntimeError("not a curated folder (no README.md)")
    contract = parse_contract(drive.download(readme.id).decode("utf-8"))
    logs = tree.folders.get(contract.logs)
    if logs is None:
        raise RuntimeError("the contract's logs folder is missing")
    return logs.id


def is_reocr_backup_of(name: str, filename: str) -> bool:
    """Is ``name`` a re-OCR backup of this document's sidecar
    (``_reocr-backup-<date>[-<time>]-<filename>.md``)?"""
    from oncofiles.curated.runner import nfc

    exact = re.compile(rf"_reocr-backup-\d{{8}}(?:-\d{{6}})?-{re.escape(nfc(filename))}\.md")
    return bool(exact.fullmatch(nfc(name or "")))


def is_reocr_candidate_of(name: str, filename: str) -> bool:
    """Is ``name`` a kept re-OCR candidate of this document
    (``_reocr-candidate-<date>-<time>-<filename>.md``)?"""
    from oncofiles.curated.runner import nfc

    exact = re.compile(
        rf"{re.escape(CANDIDATE_PREFIX)}\d{{8}}-\d{{6}}-{re.escape(nfc(filename))}\.md"
    )
    return bool(exact.fullmatch(nfc(name or "")))


def restore_counts(current: str, restored: str) -> dict:
    """What a restore would change: counts only, never a token (#806).
    ``old_*`` = the current sidecar, ``new_*`` = the backup's text;
    ``missing_*`` = tokens of the current text the backup does not have."""
    cur_nums, cur_dates = _tokens(current)
    new_nums, new_dates = _tokens(restored)
    return {
        "old_chars": _chars(current), "new_chars": _chars(restored),
        "old_numbers": len(cur_nums), "new_numbers": len(new_nums),
        "old_dates": len(cur_dates), "new_dates": len(new_dates),
        "missing_numbers": len(cur_nums - new_nums),
        "missing_dates": len(cur_dates - new_dates),
        "regained_numbers": len(new_nums - cur_nums),
        "regained_dates": len(new_dates - cur_dates),
    }  # fmt: skip


def with_front_keys(text: str, updates: dict[str, str]) -> str:
    """A sidecar text with these top-level front-matter keys set (replaced in
    place or appended). A text without front matter is returned unchanged."""
    m = _FRONT.match(text or "")
    if not m or not updates:
        return text
    out, seen = [], set()
    for line in m.group(1).splitlines():
        key = line.split(":", 1)[0].strip() if ":" in line and not line.startswith(" ") else None
        if key in updates:
            out.append(f"{key}: {updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}: {v}" for k, v in updates.items() if k not in seen]
    return "---\n" + "\n".join(out) + "\n---\n" + text[m.end() :]


def restore_sidecar(
    drive, root_id: str, doc: dict, backup_id: str, *, dry_run: bool = True, now: datetime,
    logs_id: str | None = None, candidate_front: dict[str, str] | None = None,
) -> dict:  # fmt: skip
    """Put a re-OCR backup back as the document's sidecar (#806), or promote
    a kept re-OCR candidate to it. Blocking.

    ``doc``: document_id, filename, sidecar_id. ``backup_id`` must be a file
    in the contract's logs folder named ``_reocr-backup-*-<filename>.md`` or
    ``_reocr-candidate-*-<filename>.md`` (then its header block is removed and
    the rest is the new sidecar text; a file without that header is refused,
    ``candidate_malformed``); anything else raises ``RestoreRefusedError``.
    ``backup_kind`` in the result says which. A dry run only reports counts.
    A real run (caller holds ``contract_writes()``) first copies the CURRENT
    sidecar to logs (``_restore-backup-<ts>-<filename>.md``; ``create_text``
    refuses an existing name), then updates the sidecar in place with the
    backup's text (read-back verified by ``drive_ops``). Nothing is deleted.

    ``logs_id``: the logs folder when the caller already resolved it.
    ``candidate_front`` (#831, ``promote_candidates`` only): front-matter keys
    set on a promoted CANDIDATE's sidecar text; a backup is always restored
    verbatim."""
    logs_id = logs_id or _logs_folder_id(drive, root_id)
    backup = next((i for i in drive.list_children(logs_id) if i.id == backup_id), None)
    if backup is None:
        raise RestoreRefusedError("backup_not_in_logs")
    if is_reocr_backup_of(backup.name, doc["filename"]):
        kind = "reocr_backup"
    elif is_reocr_candidate_of(backup.name, doc["filename"]):
        kind = CANDIDATE_KIND
    else:
        raise RestoreRefusedError("backup_not_of_this_document")
    try:
        current = drive.download(doc["sidecar_id"]).decode("utf-8")
        restored = drive.download(backup_id).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RestoreRefusedError("not_utf8") from exc
    if kind == CANDIDATE_KIND:
        restored = candidate_sidecar_text(restored)
        if restored is None:
            raise RestoreRefusedError("candidate_malformed")
        if candidate_front:
            restored = with_front_keys(restored, candidate_front)
    out = {"document_id": doc["document_id"], "dry_run": dry_run, "backup_kind": kind,
           **restore_counts(current, restored)}  # fmt: skip
    if current == restored:
        out["result"] = "unchanged"
        return out
    if dry_run:
        out["result"] = "previewed"
        return out
    name = f"_restore-backup-{now:%Y%m%d-%H%M%S}-{doc['filename']}.md"
    out["restore_backup_id"] = drive.create_text(logs_id, name, current)  # before any change
    drive.update_text(doc["sidecar_id"], restored)
    out["result"] = "restored"
    return out


# ── promote kept candidates of Tesseract-era sidecars (#831) ────────────


def evaluate_candidate(
    old_text: str, new_text: str, vocab, *, ink: list | None = None,
    text_layer: list | None = None, trusted_codes: Collection | None = None,
) -> dict:  # fmt: skip
    """The rewrite guards for a kept candidate (``new_text``: the sidecar text
    inside it) against the current sidecar, without any model call. Codes and
    counts only. ``ink`` / ``text_layer``: what the candidate's header
    recorded (``candidate_page_ink``); a candidate without it cannot show a
    near-empty page to be blank (``near_empty_unknown_ink``)."""
    from oncofiles.curated.indexer import parse_sidecar

    profile = guard_profile(old_text)
    lost = lost_values(old_text, new_text, vocab)  # internal: carries values (#805)
    loss = token_loss(old_text, new_text)
    empty = near_empty_pages(parse_sidecar(new_text)[1])
    codes = lost_codes(lost)
    lost_trusted, trusted_reason = trusted_loss(profile, codes, trusted_codes)
    blocking, untrusted, share = profile_reasons(
        profile, review_reasons(lost, loss, empty), new_text,
        empty_pages=empty, ink=ink, text_layer=text_layer, trusted_reason=trusted_reason,
    )  # fmt: skip
    return {
        "lost_trusted_codes": lost_trusted,
        "blank_pages": near_empty_by_ink(empty, ink, text_layer)[0] if profile else [],
        "profile": profile, "decision": "review" if blocking else "rewrite",
        "review_reasons": blocking, "baseline_untrusted": untrusted,
        "uncertain_share": share, "lost_codes": codes,
        "missing_numbers": loss["missing_numbers"], "missing_dates": loss["missing_dates"],
        "old_chars": loss["old_chars"], "new_chars": loss["new_chars"],
        "word_overlap": loss["word_overlap"], "near_empty_pages": empty,
    }  # fmt: skip


def promote_candidates(
    drive, root_id: str, docs: list[dict], vocab, *, dry_run: bool = True, now: datetime,
    explicit: bool = False, limit: int = PROMOTE_MAX_DOCS, stop=lambda: False,
) -> dict:  # fmt: skip
    """Promote the NEWEST kept re-OCR candidate of each Tesseract-era sidecar
    that passes the ``tesseract_baseline`` guards (#831). Blocking; the model
    is never called and the source file is never read.

    ``docs``: document_id, filename, sidecar_id, trusted_codes (as in
    ``run_reocr``; missing = unknown, fails closed). With ``explicit`` every
    listed document gets an entry (``refused`` with a reason code when it has
    no candidate or its sidecar is not Tesseract-era); otherwise documents
    without a candidate or with another engine are only counted and at most
    ``limit`` are evaluated (``remaining``: further documents with a
    candidate). A passing candidate goes through ``restore_sidecar`` (caller
    holds ``contract_writes()``): the current sidecar is copied to logs first,
    the update is read-back verified. The candidate file stays in logs;
    nothing is deleted. Codes, counts and ids only in the result."""
    from oncofiles.curated.runner import nfc

    logs_id = _logs_folder_id(drive, root_id)
    children = drive.list_children(logs_id)
    out: dict = {"dry_run": dry_run, "profile": TESSERACT_PROFILE, "documents": [],
                 "without_candidate": 0, "not_tesseract": 0, "remaining": 0}  # fmt: skip
    for d in docs:
        cands = sorted(
            (i for i in children if is_reocr_candidate_of(i.name, d["filename"])),
            key=lambda i: nfc(i.name),  # same prefix and filename: by timestamp
        )
        entry: dict = {"document_id": d["document_id"], "candidates": len(cands)}
        if not cands:
            if explicit:
                out["documents"].append({**entry, "result": "refused", "error": "no_candidate"})
            else:
                out["without_candidate"] += 1
            continue
        if not explicit and len(out["documents"]) >= limit:
            out["remaining"] += 1
            continue
        if stop():
            out["documents"].append({**entry, "result": "skipped", "error": "stopping"})
            continue
        entry["candidate"] = cands[-1].id
        try:
            try:
                old = drive.download(d["sidecar_id"]).decode("utf-8")
                if not is_tesseract_engine(sidecar_engine(old)):
                    if explicit:
                        raise RestoreRefusedError("not_tesseract_sidecar")
                    out["not_tesseract"] += 1
                    continue
                if guard_profile(old) is None:
                    raise RestoreRefusedError("tesseract_profile_disabled")
                raw = drive.download(cands[-1].id).decode("utf-8")
                new = candidate_sidecar_text(raw)
            except UnicodeDecodeError as exc:
                raise RestoreRefusedError("not_utf8") from exc
            if new is None:
                raise RestoreRefusedError("candidate_malformed")
            ink, layer = candidate_page_ink(raw)
            entry.update(evaluate_candidate(old, new, vocab, ink=ink, text_layer=layer,
                                            trusted_codes=d.get("trusted_codes")))  # fmt: skip
            logger.info(
                "re-OCR candidate check doc=%s decision=%s reasons=%s profile=%s "
                "baseline_untrusted=%s uncertain_share=%s dry_run=%s",
                d["document_id"], entry["decision"], ",".join(entry["review_reasons"]) or "-",
                entry["profile"], ",".join(entry["baseline_untrusted"]) or "-",
                entry["uncertain_share"], dry_run,
            )  # fmt: skip
            if entry["review_reasons"]:
                entry["result"] = "review"  # the candidate stays a candidate
            elif dry_run:
                entry["result"] = "previewed"
            else:
                done = restore_sidecar(
                    drive, root_id, d, cands[-1].id, dry_run=False, now=now, logs_id=logs_id,
                    candidate_front={"replaced_engine": short_engine(sidecar_engine(old))},
                )  # fmt: skip
                entry["restore_backup_id"] = done.get("restore_backup_id")
                entry["result"] = "promoted" if done["result"] == "restored" else done["result"]
        except RestoreRefusedError as exc:
            entry.update(result="refused", error=str(exc))
        except Exception as exc:
            logger.exception("promotion of a candidate of doc %s failed", d.get("document_id"))
            entry.update(result="failed", error=type(exc).__name__)
        out["documents"].append(entry)
    return out
