"""One call + parse path for the local-LLM fact extractors (#767).

Both extractors — lab values (``curated.llm_extract``, #714) and the clinical
kinds (``curated.llm_extract_clinical``, #726) — send one page of sidecar text
to the LOCAL model and parse a JSON answer. Everything about *how* the model
is called and how its answer channels are read lives here, once:

* call settings: thinking on/off, sampling profile, answer budget, timeout,
  reasoning-off marker (env defaults, per-job overrides for the dry-run A/B
  only);
* an optional ``response_format`` with the HTTP-400 retry without it;
* ``finish_reason=length`` → the page is ``truncated`` (never "found nothing");
* the strict reasoning-channel fallback (the caller's validator decides, and
  every item from it stays unverified in the verifier);
* error kind / class and per-page counts (tokens, seconds) — never text;
* the one-level page split (#773): a page whose answer came back truncated
  is retried ONCE as 2 line-boundary chunks (optionally split before the
  first call, ``LLM_EXTRACT_SPLIT_LINES``). Worst case per page: 3 calls,
  each bounded by the answer budget and the timeout.

Each extractor keeps its own prompt, schema and kind-specific parsing.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from oncofiles.curated import llm

logger = logging.getLogger(__name__)

THINKING_MODES = ("on", "off")
SAMPLING_MODES = ("greedy", "qwen")
# Qwen3.8 model-card recommendations (#770): greedy decoding makes Qwen3-family
# models repeat endlessly, especially in non-thinking mode (#767).
QWEN_NON_THINKING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5}
QWEN_THINKING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20}
GREEDY = {"temperature": 0.0}
# #770: reasoning-off marker in the system message (llm.REASONING_OFF_MARKER),
# for the LM Studio Prompt Template rule; env LLM_REASONING_MARKER.
REASONING_MARKER_MODES = ("on", "off")
TIMEOUT_MIN_S, TIMEOUT_MAX_S = 30, 900
PARALLEL_DOCS_MIN, PARALLEL_DOCS_MAX = 1, 3  # documents at once (#773)

ERROR_KINDS = ("http_status", "json_decode", "schema_shape", "empty", "timeout", "truncated",
               "reasoning_ambiguous", "no_json",
               "other")  # fmt: skip


class AnswerError(llm.LocalLLMError):
    """The model answered, but not with a usable facts JSON."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def error_kind(exc: BaseException) -> str:
    if isinstance(exc, AnswerError):
        return exc.kind
    msg = str(exc)
    if "truncated" in msg:
        return "truncated"
    if "unreachable" in msg and "Timeout" in msg:
        return "timeout"
    if "HTTP" in msg:
        return "http_status"
    return "other"


def sampling_params(sampling: str, thinking: bool) -> dict:
    """Request parameters of a sampling profile (``greedy`` sends only
    ``temperature=0``, as every call did before #767)."""
    if sampling == "qwen":
        return dict(QWEN_THINKING if thinking else QWEN_NON_THINKING)
    return dict(GREEDY)


def validate_overrides(
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
    parallel_docs: int | None = None,
) -> None:
    """Per-job overrides must be one of the known modes, ``timeout_s`` an
    int of seconds in [30, 900] and ``parallel_docs`` an int in [1, 3]
    (ValueError otherwise)."""
    if parallel_docs is not None and (
        isinstance(parallel_docs, bool)
        or not isinstance(parallel_docs, int)
        or not PARALLEL_DOCS_MIN <= parallel_docs <= PARALLEL_DOCS_MAX
    ):
        raise ValueError(
            f"parallel_docs must be an int, {PARALLEL_DOCS_MIN} <= parallel_docs <= "
            f"{PARALLEL_DOCS_MAX}, or null, got {parallel_docs!r}"
        )
    if reasoning_marker is not None and reasoning_marker not in REASONING_MARKER_MODES:
        raise ValueError(
            f"reasoning_marker must be one of {REASONING_MARKER_MODES} or null, "
            f"got {reasoning_marker!r}"
        )
    if thinking is not None and thinking not in THINKING_MODES:
        raise ValueError(f"thinking must be one of {THINKING_MODES} or null, got {thinking!r}")
    if sampling is not None and sampling not in SAMPLING_MODES:
        raise ValueError(f"sampling must be one of {SAMPLING_MODES} or null, got {sampling!r}")
    if timeout_s is not None and (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, int)
        or not TIMEOUT_MIN_S <= timeout_s <= TIMEOUT_MAX_S
    ):
        raise ValueError(
            f"timeout_s must be an int of seconds, {TIMEOUT_MIN_S} <= timeout_s <= "
            f"{TIMEOUT_MAX_S}, or null, got {timeout_s!r}"
        )


def refuse_overrides_on_write(
    *,
    write: bool,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
    parallel_docs: int | None = None,
) -> None:
    """Overrides are for the dry-run A/B only: a write path never takes them."""
    validate_overrides(thinking, sampling, timeout_s, reasoning_marker, parallel_docs)
    if write and any(
        v is not None for v in (thinking, sampling, timeout_s, reasoning_marker, parallel_docs)
    ):
        raise ValueError(
            "thinking/sampling/timeout_s/reasoning_marker/parallel_docs overrides are allowed "
            "only on a dry run; a write always uses the configured LLM_EXTRACT_THINKING / "
            "LLM_EXTRACT_SAMPLING / LLM_EXTRACT_TIMEOUT_S / "
            "LLM_REASONING_MARKER / LLM_EXTRACT_PARALLEL_DOCS"
        )


def parallel_docs(override: int | None = None) -> int:
    """Documents extracted at once (#773): the per-job override (dry run only)
    over ``LLM_EXTRACT_PARALLEL_DOCS``, always within [1, 3]. Each document
    holds one LM Studio slot while it runs."""
    from oncofiles import config

    validate_overrides(parallel_docs=override)
    n = override if override is not None else config.LLM_EXTRACT_PARALLEL_DOCS
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 1
    return max(PARALLEL_DOCS_MIN, min(PARALLEL_DOCS_MAX, n))


@dataclass(frozen=True)
class CallSettings:
    thinking: bool
    sampling: str
    max_tokens: int
    timeout_s: float
    # Passed explicitly to every llm.chat call of this job (#770): never env
    # or module state, so concurrent classify / re-OCR calls are unaffected.
    reasoning_marker: bool = False

    @property
    def params(self) -> dict:
        return sampling_params(self.sampling, self.thinking)

    def report(self) -> dict:
        """Effective settings for a job report (numbers and modes only)."""
        return {"thinking": "on" if self.thinking else "off", "sampling": self.sampling,
                "max_tokens": self.max_tokens, "timeout_s": self.timeout_s,
                "reasoning_marker": "on" if self.reasoning_marker else "off",
                **self.params}  # fmt: skip


def settings(
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
) -> CallSettings:
    """Effective call settings: per-job overrides over the env defaults.

    Thinking ON keeps the full ``llm.REASONING_BUDGET`` (LM Studio counts the
    reasoning into ``max_tokens``, #689); thinking OFF gets the bounded answer
    budget ``LLM_EXTRACT_ANSWER_BUDGET`` so a repetition loop ends early.
    ``timeout_s``
    overrides ``LLM_EXTRACT_TIMEOUT_S``; ``reasoning_marker`` overrides
    ``LLM_REASONING_MARKER`` (#770; it only takes effect with thinking OFF)."""
    from oncofiles import config

    validate_overrides(thinking, sampling, timeout_s, reasoning_marker)
    on = (thinking or config.LLM_EXTRACT_THINKING) == "on"
    mode = sampling or config.LLM_EXTRACT_SAMPLING
    if mode not in SAMPLING_MODES:
        mode = "greedy"  # an unknown env value never changes behaviour
    budget = llm.REASONING_BUDGET if on else config.LLM_EXTRACT_ANSWER_BUDGET
    timeout = timeout_s if timeout_s is not None else config.LLM_EXTRACT_TIMEOUT_S
    marker = (
        reasoning_marker == "on"
        if reasoning_marker is not None
        else bool(config.LLM_REASONING_MARKER)
    )
    return CallSettings(on, mode, int(budget), float(timeout), reasoning_marker=marker)


@dataclass
class PageResult:
    page_number: int
    candidates: list[dict] = field(default_factory=list)
    dropped: Counter = field(default_factory=Counter)  # reason -> count
    error: str | None = None
    seconds: float = 0.0
    finish_reason: str | None = None
    error_kind: str | None = None
    error_class: str | None = None
    structured_output: str | None = None  # accepted | rejected (HTTP 400) | off
    answer: dict | None = None  # answer_shape() of the lab extractor: masked, in memory only
    answer_source: str | None = None  # content | reasoning
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    thinking: str | None = None  # on | off (effective)
    sampling: str | None = None  # greedy | qwen (effective)
    reasoning_marker: bool | None = None  # marker setting of the call (#770)
    # page split (#773): counts only
    split: bool = False
    split_trigger: str | None = None  # truncated | proactive
    split_first_call: dict | None = None  # the truncated whole-page call
    split_chunks: list[dict] = field(default_factory=list)  # one per chunk CALLED
    split_duplicates_removed: int = 0
    # context-loss check of the split (#773): candidates and those without a date
    items: int = 0
    date_null_items: int = 0

    def diagnostics(self) -> dict:
        """Counts and codes only — never page or answer text."""
        out = {"finish_reason": self.finish_reason, "structured_output": self.structured_output,
               "answer_source": self.answer_source, "seconds": round(self.seconds, 1),
               "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
               "reasoning_tokens": self.reasoning_tokens, "thinking": self.thinking,
               "sampling": self.sampling,
               "reasoning_marker": self.reasoning_marker}  # fmt: skip
        if self.error:
            out.update(error_kind=self.error_kind, error_class=self.error_class)
        if self.answer:
            out["answer"] = self.answer
        out["split"] = self.split
        out.update(items=self.items, date_null_items=self.date_null_items)
        if self.split:
            out.update(split_trigger=self.split_trigger, chunk_count=SPLIT_CHUNKS,
                       chunks_called=len(self.split_chunks), first_call=self.split_first_call,
                       chunks=list(self.split_chunks),
                       duplicates_removed=self.split_duplicates_removed)  # fmt: skip
        return out

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length" or "truncated" in (self.error or "")

    @property
    def assessed(self) -> bool:
        """False for a truncated, empty or unparseable answer: the page was
        NOT read, which is never the same as "the model found nothing"."""
        return self.error is None and not self.truncated


def call_and_parse(
    res: PageResult,
    *,
    system: str,
    user: str,
    response_format: dict | None,
    parse_content: Callable[[str], Any],
    parse_reasoning: Callable[[str], Any],
    cfg: CallSettings,
    chat: Callable[..., str] | None = None,
    shape: Callable[[str, str], dict] | None = None,
    label: str = "llm extract",
) -> Any:
    """Blocking. One model call for one page; returns the parsed answer, or
    ``None`` with ``res.error`` / ``error_kind`` / ``error_class`` set (the
    page is then NOT assessed). Fills the per-page diagnostics of ``res``.

    ``parse_content`` reads the ``content`` channel; ``parse_reasoning`` is the
    caller's STRICT validator for an answer found only in ``reasoning_content``
    (``res.answer_source = "reasoning"``: never machine-trusted downstream).
    ``shape(raw, field)`` optionally records the masked answer shape."""
    call = chat or llm.chat
    res.thinking = "on" if cfg.thinking else "off"
    res.sampling = cfg.sampling
    res.reasoning_marker = cfg.reasoning_marker
    # reasoning_marker is always explicit here (#770): this job's setting, never
    # read from shared state by the concurrent classify / re-OCR calls.
    kwargs = {"max_tokens": cfg.max_tokens, "thinking": cfg.thinking,
              "timeout": cfg.timeout_s, "reasoning_marker": cfg.reasoning_marker,
              **cfg.params}  # fmt: skip
    info: dict = {}
    t0 = time.monotonic()
    try:
        try:
            if response_format is not None:
                answer = call(system, user, response_format=response_format, info=info, **kwargs)
                res.structured_output = "accepted"
            else:
                answer = call(system, user, info=info, **kwargs)
                res.structured_output = "off"
        except llm.LocalLLMError as exc:
            if response_format is None or "HTTP 400" not in str(exc) or "truncated" in str(exc):
                raise
            # this server/model rejects structured output: strict instructions only
            res.structured_output = "rejected"
            info.clear()
            answer = call(system, user, info=info, **kwargs)
        _counts(res, info)
        res.finish_reason = info.get("finish_reason")
        raw, field_used = info.get("raw_content", answer or ""), "content"
        if (
            not llm.strip_think(answer or "").strip()
            and (info.get("reasoning_content") or "").strip()
        ):
            # LM Studio may put the whole answer in reasoning_content (#714 prod run)
            raw, field_used = info["reasoning_content"], "reasoning_content"
        if shape is not None:
            res.answer = shape(raw or "", field_used)
        if res.finish_reason == "length":
            raise llm.LocalLLMError("local LLM answer truncated: finish_reason=length")
        if field_used == "reasoning_content":
            parsed = parse_reasoning(raw)
            res.answer_source = "reasoning"
        else:
            parsed = parse_content(answer)
            res.answer_source = "content"
    except Exception as exc:  # any failure leaves the page "not assessed"
        _counts(res, info)
        res.finish_reason = res.finish_reason or info.get("finish_reason")
        res.error = str(exc)[:200]
        res.error_kind = error_kind(exc)
        res.error_class = type(exc.__cause__ or exc).__name__
        if shape is not None and res.answer is None:
            raw = info.get("raw_content") or info.get("reasoning_content")
            if raw:
                res.answer = shape(raw, "content")
        res.seconds = time.monotonic() - t0
        # Kind/class/finish only — never the answer or page text.
        logger.warning(
            "%s: page %s not assessed (%s, %s, finish=%s)",
            label, res.page_number, res.error_kind, res.error_class, res.finish_reason,
        )  # fmt: skip
        return None
    res.seconds = time.monotonic() - t0
    return parsed


# ── page split (#773) ────────────────────────────────────────────────────

SPLIT_CHUNKS = 2  # one split level only: never recurse (bounded cost)
MAX_CALLS_PER_PAGE = 1 + SPLIT_CHUNKS  # whole page + 2 chunks, each <= the timeout
_HEADING_MAX_CHARS = 60
_PREFERRED_WINDOW = (0.3, 0.7)  # a preferred cut lies in this share of the text


def _is_heading(line: str) -> bool:
    """A generic section-heading line (no layout rules): a short line that is
    a markdown heading, ends with a colon, or is written in capitals."""
    s = line.strip()
    if not s or len(s) > _HEADING_MAX_CHARS:
        return False
    if s.startswith("#") or s.endswith(":"):
        return True
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


def split_page_text(text: str) -> tuple[str, str] | None:
    """Split a page into 2 chunks at a line boundary near the middle (by
    characters), never inside a line. Prefers a cut after a blank line or
    before a heading line within the middle 30-70 % of the text, else the
    line boundary closest to the middle. ``chunk1 + "\\n" + chunk2 == text``.
    ``None`` when the page has fewer than 2 lines with content."""
    text = text or ""
    lines = text.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    filled = [bool(line.strip()) for line in lines]
    cuts = [i for i in range(1, len(lines)) if any(filled[:i]) and any(filled[i:])]
    if not cuts:
        return None
    total = len(text)
    mid = total / 2
    lo, hi = _PREFERRED_WINDOW[0] * total, _PREFERRED_WINDOW[1] * total
    preferred = [
        i for i in cuts
        if lo <= starts[i] <= hi and (not filled[i - 1] or _is_heading(lines[i]))
    ]  # fmt: skip
    best = min(preferred or cuts, key=lambda i: (abs(starts[i] - mid), i))
    return "\n".join(lines[:best]), "\n".join(lines[best:])


@dataclass
class AnswerPart:
    """The parsed answer of one call, with the channel it came from (an item
    from the reasoning channel stays unverified, chunk by chunk)."""

    parsed: Any
    answer_source: str | None


def _call_diag(r: PageResult, lines: int | None = None) -> dict:
    out = {"finish_reason": r.finish_reason, "prompt_tokens": r.prompt_tokens,
           "completion_tokens": r.completion_tokens, "reasoning_tokens": r.reasoning_tokens,
           "seconds": round(r.seconds, 1)}  # fmt: skip
    if lines is not None:
        out.update(lines=lines, answer_source=r.answer_source)
    if r.error:
        out["error_kind"] = r.error_kind
    return out


def _sum(values) -> int | None:
    nums = [v for v in values if v is not None]
    return sum(nums) if nums else None


def call_page(
    res: PageResult,
    *,
    text: str,
    system: str,
    response_format: dict | None,
    parse_content: Callable[[str], Any],
    parse_reasoning: Callable[[str], Any],
    cfg: CallSettings,
    chat: Callable[..., str] | None = None,
    shape: Callable[[str, str], dict] | None = None,
    label: str = "llm extract",
    split_lines: int | None = None,
) -> list[AnswerPart] | None:
    """Blocking. The page's answer as a list of parts (1, or 2 after a split),
    or ``None`` when the page is NOT assessed (``res.error`` set).

    One call for the whole page. Only when that answer is truncated
    (``finish=length``) is the page text split ONCE into 2 line-boundary
    chunks (``split_page_text``), each chunk called once with the same
    settings. A chunk that is not assessed (truncated, empty, unparseable,
    timeout) leaves the whole page not assessed: there is no second split
    level, so a page costs at most ``MAX_CALLS_PER_PAGE`` calls of at most
    ``cfg.timeout_s`` each. With ``split_lines`` (default
    ``LLM_EXTRACT_SPLIT_LINES``, 0 = off) > 0, a page of more lines is split
    before the first call (2 calls). The caller verifies every item against
    the FULL page text and calls ``dedupe_split`` on its candidates."""
    from oncofiles import config

    limit = config.LLM_EXTRACT_SPLIT_LINES if split_lines is None else split_lines
    common = {"system": system, "response_format": response_format,
              "parse_content": parse_content, "parse_reasoning": parse_reasoning,
              "cfg": cfg, "chat": chat, "shape": shape}  # fmt: skip
    user = f"Page {res.page_number}:\n"
    if limit > 0 and len(text.split("\n")) > limit:
        chunks = split_page_text(text)
        if chunks is not None:
            return _call_chunks(res, chunks, "proactive", None, user, label, common)
    parsed = call_and_parse(res, user=user + text, label=label, **common)
    if parsed is not None:
        return [AnswerPart(parsed, res.answer_source)]
    if not res.truncated:
        return None  # timeout, HTTP, unparseable: a split would not help
    chunks = split_page_text(text)
    if chunks is None:
        return None  # a single line cannot be split: stays not assessed
    return _call_chunks(res, chunks, "truncated", _call_diag(res), user, label, common)


def _call_chunks(
    res: PageResult,
    chunks: tuple[str, str],
    trigger: str,
    first: dict | None,
    user: str,
    label: str,
    common: dict,
) -> list[AnswerPart] | None:
    cfg: CallSettings = common["cfg"]
    parts: list[AnswerPart] = []
    called: list[PageResult] = []
    failed: PageResult | None = None
    for i, chunk in enumerate(chunks, 1):
        cr = PageResult(page_number=res.page_number)
        parsed = call_and_parse(
            cr, user=user + chunk, label=f"{label} (chunk {i}/{len(chunks)})", **common
        )
        called.append(cr)
        res.split_chunks.append(_call_diag(cr, lines=len(chunk.split("\n"))))
        if parsed is None:
            failed = cr  # the page stays not assessed: the next chunk is not called
            break
        parts.append(AnswerPart(parsed, cr.answer_source))
    res.split, res.split_trigger, res.split_first_call = True, trigger, first
    res.thinking = "on" if cfg.thinking else "off"
    res.sampling, res.reasoning_marker = cfg.sampling, cfg.reasoning_marker
    calls = ([res] if first is not None else []) + called
    res.seconds = sum(r.seconds for r in calls)
    res.prompt_tokens = _sum(r.prompt_tokens for r in calls)
    res.completion_tokens = _sum(r.completion_tokens for r in calls)
    res.reasoning_tokens = _sum(r.reasoning_tokens for r in calls)
    res.structured_output = called[0].structured_output
    if failed is not None:
        res.finish_reason = failed.finish_reason
        res.error = f"split chunk {len(called)}/{len(chunks)} not assessed: {failed.error}"[:200]
        res.error_kind, res.error_class = failed.error_kind, failed.error_class
        res.answer, res.answer_source = failed.answer, None
        logger.warning(
            "%s: page %s split (%s), chunk %s/%s not assessed (%s, finish=%s)",
            label, res.page_number, trigger, len(called), len(chunks), failed.error_kind,
            failed.finish_reason,
        )  # fmt: skip
        return None
    res.finish_reason = called[-1].finish_reason
    res.error = res.error_kind = res.error_class = None
    res.answer = called[0].answer
    sources = {p.answer_source for p in parts}
    res.answer_source = "reasoning" if "reasoning" in sources else "content"
    logger.info(
        "%s: page %s assessed after split (%s, %s chunks)",
        label, res.page_number, trigger, len(chunks),
    )  # fmt: skip
    return parts


def _is_date_field(key: str) -> bool:
    return key == "date_as_printed" or key.endswith("_date_as_printed")


def date_is_null(cand: dict) -> bool:
    """True when none of the item's printed date fields holds a value (lab
    ``date_as_printed``; clinical ``date_as_printed`` / ``start_…`` /
    ``end_…``). An item without any date field counts as null."""
    return not any(v for k, v in cand.items() if _is_date_field(k))


def finish_candidates(res: PageResult) -> None:
    """After an extractor has collected a page's candidates: drop exact
    duplicates of a split page, then count items and items without a date
    (the split's context-loss check, #773). Counts only."""
    dedupe_split(res)
    res.items = len(res.candidates)
    res.date_null_items = sum(date_is_null(c) for c in res.candidates)


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


DATE_NULL_KEYS = tuple(
    f"{k}_{n}" for n in ("split", "unsplit") for k in ("items", "date_null_items", "date_null_rate")
)


def date_null_totals(page_results: list[PageResult]) -> dict:
    """Report totals: items without a date / items, for split vs unsplit
    pages (assessed pages only; a page not assessed has no items). Counts and
    rates only."""
    out: dict = {}
    for name, flag in (("split", True), ("unsplit", False)):
        pages = [p for p in page_results if p.assessed and p.split is flag]
        items = sum(p.items for p in pages)
        null = sum(p.date_null_items for p in pages)
        out.update({f"items_{name}": items, f"date_null_items_{name}": null,
                    f"date_null_rate_{name}": _rate(null, items)})  # fmt: skip
    return out


def dedupe_split(res: PageResult) -> None:
    """Drop exact duplicate candidates of a split page: the same item (same
    kind, same evidence, same fields) returned twice, e.g. by both chunks for
    a repeated line. Two different items quoting one printed line (two values
    on one row) are both kept. An unsplit page is left exactly as before."""
    if not res.split:
        return
    seen: set[str] = set()
    kept: list[dict] = []
    for cand in res.candidates:
        key = json.dumps(cand, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            res.split_duplicates_removed += 1
            continue
        seen.add(key)
        kept.append(cand)
    res.candidates = kept


def _counts(res: PageResult, info: dict) -> None:
    for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
        value = info.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            setattr(res, key, value)


def warnings(page_results: list[PageResult]) -> list[dict]:
    """Report warnings of a document: a fallback or a cut-off answer must be
    visible, never a silent pass. Counts of pages only."""
    counts = {
        "answer_from_reasoning_channel": sum(p.answer_source == "reasoning" for p in page_results),
        "truncated": sum(p.truncated for p in page_results),
        "page_errors": sum(p.error is not None and not p.truncated for p in page_results),
        # pages sent as 2 chunks (#773); each chunk call is in page_diagnostics
        "split_pages": sum(p.split for p in page_results),
    }
    return [{"warning": k, "pages": n} for k, n in counts.items() if n]


def merge_warnings(reports: list[dict]) -> list[dict]:
    """Sum the per-document ``warnings`` of a job report."""
    total: Counter = Counter()
    for r in reports:
        for w in r.get("warnings") or []:
            total[w["warning"]] += int(w.get("pages") or 0)
    return [{"warning": k, "pages": n} for k, n in total.items() if n]
