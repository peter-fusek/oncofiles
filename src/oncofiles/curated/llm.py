"""Minimal client for the local OpenAI-compatible LLM (LM Studio) (#565).

Decision 2026-11-07: OCR — and, by extension, classification — of curated
folders runs on a local vision model on the self-hosted server, so medical
document content never leaves it. Plain ``httpx`` against
``/chat/completions``; no SDK dependency.

The model "thinks" by default; ``/no_think`` is sent in the system prompt
(and, with ``thinking=False``, in the last user message, #689) and any
``<think>`` block or echoed ``/no_think`` is stripped from the answer.
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
    ``info`` (optional dict) receives ``finish_reason`` of the completion."""
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
    payload = {
        "model": config.LOCAL_LLM_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Qwen3.x reasons before answering and ignores "/no_think" in the SYSTEM
        # prompt in LM Studio; the reasoning counts against max_tokens, so callers
        # pass a large budget. thinking=False also puts it in the user turn (#689).
        "reasoning_effort": reasoning_effort,
        "messages": [
            {"role": "system", "content": system + "\n" + NO_THINK},
            {"role": "user", "content": user_content},
        ],
    }
    if response_format is not None:
        payload["response_format"] = response_format
    if thinking is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
    if thinking is False and config.LLM_NO_THINK_SOFT_SWITCH:
        # The kwarg alone is ignored by LM Studio for Qwen3.x MLX (#689).
        payload["messages"][-1]["content"] = _with_no_think(user_content)
    headers = {"Content-Type": "application/json"}
    if config.LOCAL_LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LOCAL_LLM_API_KEY}"
    own = client is None
    http = client or httpx.Client(timeout=timeout or config.LOCAL_LLM_TIMEOUT_S)
    t0 = time.monotonic()
    try:
        resp = http.post(
            f"{config.LOCAL_LLM_BASE_URL}/chat/completions", json=payload, headers=headers
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
        info["finish_reason"] = choice.get("finish_reason")
    _log_usage(resp, choice, max_tokens, est, time.monotonic() - t0)
    if not (content or "").strip() and choice.get("finish_reason") == "length":
        # The whole budget went to reasoning (2026-11-07: 1199/1200 tokens).
        raise LocalLLMError(
            f"local LLM answer truncated: reasoning used the whole budget ({max_tokens} tokens)"
        )
    return strip_think(content if isinstance(content, str) else json.dumps(content))


def _is_overflow(resp) -> bool:
    text = (resp.text or "").lower()
    return any(k in text for k in ("context", "n_ctx", "too many tokens", "maximum length"))


def _log_usage(resp, choice: dict, max_tokens: int, est: int, seconds: float) -> None:
    """Token counts per call — numbers only, never prompt or answer text."""
    try:
        usage = resp.json().get("usage") or {}
    except ValueError:
        usage = {}
    details = usage.get("completion_tokens_details") or {}
    logger.info(
        "local llm call: prompt_tokens=%s completion_tokens=%s reasoning_tokens=%s "
        "max_tokens=%s prompt_estimate=%s finish=%s seconds=%.1f",
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        details.get("reasoning_tokens"),
        max_tokens,
        est,
        choice.get("finish_reason"),
        seconds,
    )


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
