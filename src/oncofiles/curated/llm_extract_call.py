"""One call + parse path for the local-LLM fact extractors (#767).

Both extractors — lab values (``curated.llm_extract``, #714) and the clinical
kinds (``curated.llm_extract_clinical``, #726) — send one page of sidecar text
to the LOCAL model and parse a JSON answer. Everything about *how* the model
is called and how its answer channels are read lives here, once:

* call settings: thinking on/off, sampling profile, answer budget, timeout,
  prefilled assistant turn (env defaults, per-job overrides for the dry-run
  A/B only);
* an optional ``response_format`` with the HTTP-400 retry without it;
* ``finish_reason=length`` → the page is ``truncated`` (never "found nothing");
* the strict reasoning-channel fallback (the caller's validator decides, and
  every item from it stays unverified in the verifier);
* error kind / class and per-page counts (tokens, seconds) — never text.

Each extractor keeps its own prompt, schema and kind-specific parsing.
"""

from __future__ import annotations

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
# Qwen3 model-card recommendations: greedy decoding makes Qwen3-family models
# repeat endlessly, especially in non-thinking mode (#767).
QWEN_NON_THINKING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.0}
QWEN_THINKING = {"temperature": 0.6, "top_p": 0.95, "top_k": 20}
GREEDY = {"temperature": 0.0}
PREFILL_MODES = ("on", "off")
# #770: an already-closed empty think block as the final assistant turn, so a
# model that ignores thinking-off continues AFTER the reasoning.
THINK_PREFILL = "<think>\n\n</think>\n\n"
# #770: reasoning-off marker in the system message (llm.REASONING_OFF_MARKER),
# for the LM Studio Prompt Template rule; env LLM_REASONING_MARKER.
REASONING_MARKER_MODES = ("on", "off")
TIMEOUT_MIN_S, TIMEOUT_MAX_S = 30, 900

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
    prefill: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
) -> None:
    """Per-job overrides must be one of the known modes, and ``timeout_s`` an
    int of seconds in [30, 900] (ValueError otherwise)."""
    if reasoning_marker is not None and reasoning_marker not in REASONING_MARKER_MODES:
        raise ValueError(
            f"reasoning_marker must be one of {REASONING_MARKER_MODES} or null, "
            f"got {reasoning_marker!r}"
        )
    if thinking is not None and thinking not in THINKING_MODES:
        raise ValueError(f"thinking must be one of {THINKING_MODES} or null, got {thinking!r}")
    if sampling is not None and sampling not in SAMPLING_MODES:
        raise ValueError(f"sampling must be one of {SAMPLING_MODES} or null, got {sampling!r}")
    if prefill is not None and prefill not in PREFILL_MODES:
        raise ValueError(f"prefill must be one of {PREFILL_MODES} or null, got {prefill!r}")
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
    prefill: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
) -> None:
    """Overrides are for the dry-run A/B only: a write path never takes them."""
    validate_overrides(thinking, sampling, prefill, timeout_s, reasoning_marker)
    if write and any(
        v is not None for v in (thinking, sampling, prefill, timeout_s, reasoning_marker)
    ):
        raise ValueError(
            "thinking/sampling/prefill/timeout_s/reasoning_marker overrides are allowed only "
            "on a dry run; a write always uses the configured LLM_EXTRACT_THINKING / "
            "LLM_EXTRACT_SAMPLING / LLM_EXTRACT_PREFILL / LLM_EXTRACT_TIMEOUT_S / "
            "LLM_REASONING_MARKER"
        )


@dataclass(frozen=True)
class CallSettings:
    thinking: bool
    sampling: str
    max_tokens: int
    timeout_s: float
    prefill: bool = False  # effective: never with thinking on (#770)
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
                "prefill": "on" if self.prefill else "off",
                "reasoning_marker": "on" if self.reasoning_marker else "off",
                **self.params}  # fmt: skip


def settings(
    thinking: str | None = None,
    sampling: str | None = None,
    prefill: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
) -> CallSettings:
    """Effective call settings: per-job overrides over the env defaults.

    Thinking ON keeps the full ``llm.REASONING_BUDGET`` (LM Studio counts the
    reasoning into ``max_tokens``, #689); thinking OFF gets the bounded answer
    budget ``LLM_EXTRACT_ANSWER_BUDGET`` so a repetition loop ends early.
    The prefilled closed think block (#770) is used only with thinking OFF and
    ``LLM_EXTRACT_PREFILL`` (or the ``prefill="on"`` override); ``timeout_s``
    overrides ``LLM_EXTRACT_TIMEOUT_S``; ``reasoning_marker`` overrides
    ``LLM_REASONING_MARKER`` (#770; it only takes effect with thinking OFF)."""
    from oncofiles import config

    validate_overrides(thinking, sampling, prefill, timeout_s, reasoning_marker)
    on = (thinking or config.LLM_EXTRACT_THINKING) == "on"
    mode = sampling or config.LLM_EXTRACT_SAMPLING
    if mode not in SAMPLING_MODES:
        mode = "greedy"  # an unknown env value never changes behaviour
    budget = llm.REASONING_BUDGET if on else config.LLM_EXTRACT_ANSWER_BUDGET
    want_prefill = prefill == "on" if prefill is not None else bool(config.LLM_EXTRACT_PREFILL)
    timeout = timeout_s if timeout_s is not None else config.LLM_EXTRACT_TIMEOUT_S
    marker = (
        reasoning_marker == "on"
        if reasoning_marker is not None
        else bool(config.LLM_REASONING_MARKER)
    )
    return CallSettings(
        on, mode, int(budget), float(timeout), prefill=want_prefill and not on,
        reasoning_marker=marker,
    )  # fmt: skip


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
    prefill: bool | None = None  # closed think block prefilled (#770, effective)
    reasoning_marker: bool | None = None  # marker setting of the call (#770)

    def diagnostics(self) -> dict:
        """Counts and codes only — never page or answer text."""
        out = {"finish_reason": self.finish_reason, "structured_output": self.structured_output,
               "answer_source": self.answer_source, "seconds": round(self.seconds, 1),
               "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
               "reasoning_tokens": self.reasoning_tokens, "thinking": self.thinking,
               "sampling": self.sampling, "prefill": self.prefill,
               "reasoning_marker": self.reasoning_marker}  # fmt: skip
        if self.error:
            out.update(error_kind=self.error_kind, error_class=self.error_class)
        if self.answer:
            out["answer"] = self.answer
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
    res.prefill = cfg.prefill
    res.reasoning_marker = cfg.reasoning_marker
    # reasoning_marker is always explicit here (#770): this job's setting, never
    # read from shared state by the concurrent classify / re-OCR calls.
    kwargs = {"max_tokens": cfg.max_tokens, "thinking": cfg.thinking,
              "timeout": cfg.timeout_s, "reasoning_marker": cfg.reasoning_marker,
              **cfg.params}  # fmt: skip
    if cfg.prefill:  # only then: other chat signatures stay untouched (#770)
        kwargs["prefill_assistant"] = THINK_PREFILL
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
    }
    return [{"warning": k, "pages": n} for k, n in counts.items() if n]


def merge_warnings(reports: list[dict]) -> list[dict]:
    """Sum the per-document ``warnings`` of a job report."""
    total: Counter = Counter()
    for r in reports:
        for w in r.get("warnings") or []:
            total[w["warning"]] += int(w.get("pages") or 0)
    return [{"warning": k, "pages": n} for k, n in total.items() if n]
