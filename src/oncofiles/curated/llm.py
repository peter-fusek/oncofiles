"""Minimal client for the local OpenAI-compatible LLM (LM Studio) (#565).

Decision 2026-11-07: OCR — and, by extension, classification — of curated
folders runs on a local vision model on the self-hosted server, so medical
document content never leaves it. Plain ``httpx`` against
``/chat/completions``; no SDK dependency.

The model "thinks" by default; ``/no_think`` is sent in the system prompt
(and, with ``thinking=False``, in the last user message, #689) and any
``<think>`` block or echoed ``/no_think`` is stripped from the answer.
With ``LLM_REASONING_MARKER`` (#770) no ``/no_think`` is sent; thinking-off
calls end the system message with ``REASONING_OFF_MARKER`` instead.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time

import httpx

from oncofiles import config

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
NO_THINK = "/no_think"
# A "/no_think" the model echoes back must never reach a transcript or sidecar.
_NO_THINK_ECHO_RE = re.compile(r"[ \t]*/no_think\b")
# #770: the Qwen3.8 MLX chat template has no /no_think soft switch. This marker
# at the end of the system message is mapped to enable_thinking=false by a
# Prompt Template rule in LM Studio (private RUNBOOK, AI zone, #770); without
# that rule it does nothing. Sent only behind LLM_REASONING_MARKER (or the
# extractors' explicit per-call dry-run override) and only with thinking=False.
REASONING_OFF_MARKER = "[[reasoning:off]]"

# Room for the model's reasoning *plus* the answer. Measured 2026-11-07 on
# qwen3.8-27b-mlx: ~2400 reasoning tokens for one classification.
REASONING_BUDGET = 12_000
# LM Studio serves the model with a fixed context (macmini invariant, 32K;
# env LOCAL_LLM_CONTEXT_WINDOW): prompt + max_tokens must fit, or the call
# fails or truncates mid-answer.
CONTEXT_MARGIN = 1_024  # chat template, tokeniser drift
CHARS_PER_TOKEN = 3.0  # Slovak/Czech medical text, measured ~3.0-3.5 (2026-11-09)
IMAGE_TOKENS = 3_000  # one page PNG at 150 dpi (vision models tile ~28 px patches)
MIN_ANSWER_TOKENS = 1_500  # below this the model cannot reason and answer


def estimate_prompt_tokens(system: str, user_content: list[dict] | str) -> int:
    """Conservative token estimate for the prompt (no tokenizer available locally)."""
    chars = len(system)
    images = 0
    if isinstance(user_content, str):
        chars += len(user_content)
    else:
        for part in user_content:
            if part.get("type") == "text":
                chars += len(part.get("text", ""))
            else:
                images += 1
    return int(chars / CHARS_PER_TOKEN) + images * IMAGE_TOKENS


def clamp_budget(max_tokens: int, prompt_tokens: int) -> int:
    """max_tokens that still fits the context window next to the prompt."""
    window = config.LOCAL_LLM_CONTEXT_WINDOW
    return max(0, min(max_tokens, window - prompt_tokens - CONTEXT_MARGIN))


class LocalLLMError(RuntimeError):
    """The local model is unreachable, misconfigured, or answered nonsense."""


def configured() -> bool:
    return bool(config.LOCAL_LLM_BASE_URL and config.LOCAL_LLM_MODEL)


def model_id() -> str:
    return config.LOCAL_LLM_MODEL or "unconfigured"


def image_part(png_bytes: bytes, mime: str = "image/png") -> dict:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def strip_think(text: str) -> str:
    """Remove reasoning from a model answer: ``<think>…</think>`` blocks
    (including the empty one Qwen emits under ``/no_think``), anything before
    a stray ``</think>`` whose opening tag the template ate, and a literal
    ``/no_think`` echoed by the model (#689)."""
    text = _THINK_RE.sub("", text or "")
    if "</think>" in text:  # opening tag eaten by the template
        text = text.split("</think>", 1)[1]
    text = _NO_THINK_ECHO_RE.sub("", text)
    return text.strip()


def _with_no_think(user_content: list[dict] | str) -> list[dict] | str:
    """User content carrying the Qwen ``/no_think`` soft switch exactly once (#689).

    LM Studio ignores ``chat_template_kwargs.enable_thinking=false`` for the
    Qwen3.x MLX model; the soft switch in the last user turn is what the
    model itself honours. Text content gets it appended. Multimodal content
    gets a NEW text part after the image part(s), so the instruction text and
    part order are unchanged (infra review). The caller's list is not mutated."""
    if isinstance(user_content, str):
        if NO_THINK in user_content:
            return user_content
        return user_content + " " + NO_THINK
    if any(
        part.get("type") == "text" and NO_THINK in (part.get("text") or "") for part in user_content
    ):
        return user_content
    return [*user_content, {"type": "text", "text": " " + NO_THINK}]


def _system_content(system: str, thinking: bool | None, marker: bool) -> str:
    """System message text. Marker off (default): ``/no_think`` appended, as
    before #770. Marker on: the marker only with ``thinking=False``, else bare."""
    if not marker:
        return system + "\n" + NO_THINK
    if thinking is False:
        return system + "\n" + REASONING_OFF_MARKER
    return system


def chat(
    system: str,
    user_content: list[dict] | str,
    *,
    max_tokens: int = REASONING_BUDGET,
    temperature: float = 0.0,
    client: httpx.Client | None = None,
    timeout: float | None = None,
    reasoning_effort: str = "low",
    thinking: bool | None = None,
    response_format: dict | None = None,
    info: dict | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    presence_penalty: float | None = None,
    prefill_assistant: str | None = None,
    reasoning_marker: bool | None = None,
) -> str:
    """One blocking chat completion; returns the answer text.

    ``reasoning_effort`` is sent but LM Studio ignores it for the current
    Qwen3.8 MLX model (it logs "cannot be converted to any custom KVs"), so
    the model always thinks. ``thinking=False`` asks the chat template to skip
    the reasoning (``chat_template_kwargs.enable_thinking=false``) and, unless
    ``LLM_NO_THINK_SOFT_SWITCH=false``, also adds the Qwen ``/no_think`` soft
    switch to the last user message, since LM Studio ignores the kwarg for
    Qwen3.x MLX (#689); ``None`` leaves the model's default. Pass a matching
    ``max_tokens`` budget.
    ``response_format`` (OpenAI shape, e.g. ``{"type": "json_schema", ...}``)
    asks LM Studio for structured output; callers still parse defensively.
    ``top_p`` / ``top_k`` / ``presence_penalty`` are sent only when set
    (sampling profiles of the extractors, #767); every other caller keeps
    the greedy ``temperature=0`` request unchanged.
    ``timeout`` (seconds) overrides ``LOCAL_LLM_TIMEOUT_S`` for this call,
    also on a caller-supplied ``client``.
    ``info`` (optional dict) receives ``finish_reason`` and the token counts
    (``prompt_tokens``, ``completion_tokens``, ``reasoning_tokens``) of the
    completion, plus the raw answer channels (in memory only).
    ``prefill_assistant`` (optional) appends a final assistant message with
    that content, so the model continues it (#770: an already-closed empty
    think block for a model that ignores thinking-off). LM Studio may return
    only the continuation or the prefill plus continuation; ``strip_think``
    handles both. Only the extractors pass it, behind ``LLM_EXTRACT_PREFILL``.
    ``reasoning_marker`` (``None`` = env ``LLM_REASONING_MARKER``; #770): when
    on, no ``/no_think`` is sent anywhere; with ``thinking=False`` the system
    message ends with ``REASONING_OFF_MARKER``, otherwise it is sent bare.
    Only the extractor call path passes it, explicitly per call (never global
    state); every other caller follows the env. Off = byte-identical request."""
    if not configured():
        raise LocalLLMError("LOCAL_LLM_BASE_URL / LOCAL_LLM_MODEL not set")
    est = estimate_prompt_tokens(system, user_content)
    max_tokens = clamp_budget(max_tokens, est)
    if max_tokens < MIN_ANSWER_TOKENS:
        # A per-document problem (worded "truncated" so the run is not aborted
        # as "model down"): the document goes to review, it never loops.
        raise LocalLLMError(
            f"local LLM answer truncated: prompt ~{est} tokens leaves no room "
            f"in the {config.LOCAL_LLM_CONTEXT_WINDOW}-token context"
        )
    marker = config.LLM_REASONING_MARKER if reasoning_marker is None else bool(reasoning_marker)
    payload = {
        "model": config.LOCAL_LLM_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Qwen3.x reasons before answering and ignores "/no_think" in the SYSTEM
        # prompt in LM Studio; the reasoning counts against max_tokens, so callers
        # pass a large budget. thinking=False also puts it in the user turn (#689).
        "reasoning_effort": reasoning_effort,
        "messages": [
            {"role": "system", "content": _system_content(system, thinking, marker)},
            {"role": "user", "content": user_content},
        ],
    }
    sampling = {"top_p": top_p, "top_k": top_k, "presence_penalty": presence_penalty}
    payload.update({k: v for k, v in sampling.items() if v is not None})
    if response_format is not None:
        payload["response_format"] = response_format
    if thinking is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
    if thinking is False and config.LLM_NO_THINK_SOFT_SWITCH and not marker:
        # The kwarg alone is ignored by LM Studio for Qwen3.x MLX (#689).
        # With the #770 marker the template rule switches thinking off instead.
        payload["messages"][-1]["content"] = _with_no_think(user_content)
    if prefill_assistant is not None:
        # Must stay the LAST message: the model continues this turn (#770).
        payload["messages"].append({"role": "assistant", "content": prefill_assistant})
    headers = {"Content-Type": "application/json"}
    if config.LOCAL_LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LOCAL_LLM_API_KEY}"
    own = client is None
    http = client or httpx.Client(timeout=timeout or config.LOCAL_LLM_TIMEOUT_S)
    # A caller's client keeps its own timeout unless this call sets one (#767).
    per_call = {"timeout": timeout} if timeout else {}
    t0 = time.monotonic()
    try:
        resp = http.post(
            f"{config.LOCAL_LLM_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
            **per_call,
        )
    except httpx.HTTPError as exc:
        raise LocalLLMError(f"local LLM unreachable: {type(exc).__name__}") from exc
    finally:
        if own:
            http.close()
    if resp.status_code == 400 and _is_overflow(resp):
        # Our estimate was low: same per-document path as a truncation
        # (review after limited retries), never a run abort.
        raise LocalLLMError("local LLM answer truncated: context overflow (HTTP 400)")
    if resp.status_code != 200:
        raise LocalLLMError(f"local LLM HTTP {resp.status_code}")
    try:
        choice = resp.json()["choices"][0]
        content = choice["message"].get("content")
    except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
        raise LocalLLMError("local LLM returned an unexpected payload") from exc
    if info is not None:
        # Diagnostics for the caller only (in memory, never logged or stored).
        # LM Studio may leave ``content`` empty and put the text in
        # ``reasoning_content`` (reasoning parser + structured output); only
        # callers that opt in read it (#714 shadow extractor).
        message = choice.get("message") or {}
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        info["finish_reason"] = choice.get("finish_reason")
        info["raw_content"] = (
            content
            if isinstance(content, str)
            else ("" if content is None else json.dumps(content))
        )
        info["reasoning_content"] = reasoning if isinstance(reasoning, str) else ""
    effective_timeout = timeout or (config.LOCAL_LLM_TIMEOUT_S if own else "client")
    usage = _log_usage(resp, choice, payload, est, time.monotonic() - t0, timeout=effective_timeout)
    if info is not None:
        info.update(usage)  # counts only
    if not (content or "").strip() and choice.get("finish_reason") == "length":
        # The whole budget went to reasoning (2026-11-07: 1199/1200 tokens).
        raise LocalLLMError(
            f"local LLM answer truncated: reasoning used the whole budget ({max_tokens} tokens)"
        )
    return strip_think(content if isinstance(content, str) else json.dumps(content))


def _is_overflow(resp) -> bool:
    text = (resp.text or "").lower()
    return any(k in text for k in ("context", "n_ctx", "too many tokens", "maximum length"))


def _log_usage(
    resp, choice: dict, payload: dict, est: int, seconds: float, *, timeout: float | str | None
) -> dict:
    """Token counts and the effective request parameters per call — numbers
    only, never prompt or answer text. Returns the token counts."""
    try:
        usage = resp.json().get("usage") or {}
    except (ValueError, AttributeError):
        usage = {}
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("completion_tokens_details") or {}
    counts = {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens") if isinstance(details, dict) else None,
    }
    thinking = (payload.get("chat_template_kwargs") or {}).get("enable_thinking")
    logger.info(
        "local llm call: prompt_tokens=%s completion_tokens=%s reasoning_tokens=%s "
        "max_tokens=%s prompt_estimate=%s finish=%s seconds=%.1f "
        "temperature=%s top_p=%s top_k=%s presence_penalty=%s thinking=%s timeout=%s",
        counts["prompt_tokens"],
        counts["completion_tokens"],
        counts["reasoning_tokens"],
        payload.get("max_tokens"),
        est,
        choice.get("finish_reason"),
        seconds,
        payload.get("temperature"),
        payload.get("top_p"),
        payload.get("top_k"),
        payload.get("presence_penalty"),
        "default" if thinking is None else ("on" if thinking else "off"),
        timeout,
    )
    return counts


def parse_json_object(text: str) -> dict:
    """Extract the first JSON object from a model answer (tolerates ```json fences)."""
    text = strip_think(text)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    try:
        obj = json.loads(candidate)
    except ValueError as exc:
        raise LocalLLMError("local LLM answer is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise LocalLLMError("local LLM answer is not a JSON object")
    return obj
