"""Minimal client for the local OpenAI-compatible LLM (LM Studio) (#565).

Decision 2026-11-07: OCR — and, by extension, classification — of curated
folders runs on a local vision model on the self-hosted server, so medical
document content never leaves it. Plain ``httpx`` against
``/chat/completions``; no SDK dependency.

The model "thinks" by default; ``/no_think`` is sent and any ``<think>`` block
is stripped from the answer regardless.
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

# Room for the model's reasoning *plus* the answer. Measured 2026-11-07 on
# qwen3.8-27b-mlx: ~2400 reasoning tokens for one classification.
REASONING_BUDGET = 12_000
# LM Studio serves the model with a fixed 32K context (macmini invariant):
# prompt + max_tokens must fit, or the call fails or truncates mid-answer.
CONTEXT_WINDOW = 32_768
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
    return max(0, min(max_tokens, CONTEXT_WINDOW - prompt_tokens - CONTEXT_MARGIN))


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
    text = _THINK_RE.sub("", text or "")
    if "</think>" in text:  # opening tag eaten by the template
        text = text.split("</think>", 1)[1]
    return text.strip()


def chat(
    system: str,
    user_content: list[dict] | str,
    *,
    max_tokens: int = REASONING_BUDGET,
    temperature: float = 0.0,
    client: httpx.Client | None = None,
    timeout: float | None = None,
) -> str:
    """One blocking chat completion; returns the answer text."""
    if not configured():
        raise LocalLLMError("LOCAL_LLM_BASE_URL / LOCAL_LLM_MODEL not set")
    est = estimate_prompt_tokens(system, user_content)
    max_tokens = clamp_budget(max_tokens, est)
    if max_tokens < MIN_ANSWER_TOKENS:
        # A per-document problem (worded "truncated" so the run is not aborted
        # as "model down"): the document goes to review, it never loops.
        raise LocalLLMError(
            f"local LLM answer truncated: prompt ~{est} tokens leaves no room "
            f"in the {CONTEXT_WINDOW}-token context"
        )
    payload = {
        "model": config.LOCAL_LLM_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Qwen3.x reasons before answering and ignores "/no_think" in LM Studio;
        # the reasoning counts against max_tokens, so callers pass a large budget.
        "reasoning_effort": "low",
        "messages": [
            {"role": "system", "content": system + "\n/no_think"},
            {"role": "user", "content": user_content},
        ],
    }
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
    if resp.status_code != 200:
        raise LocalLLMError(f"local LLM HTTP {resp.status_code}")
    try:
        choice = resp.json()["choices"][0]
        content = choice["message"].get("content")
    except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
        raise LocalLLMError("local LLM returned an unexpected payload") from exc
    _log_usage(resp, choice, max_tokens, est, time.monotonic() - t0)
    if not (content or "").strip() and choice.get("finish_reason") == "length":
        # The whole budget went to reasoning (2026-11-07: 1199/1200 tokens).
        raise LocalLLMError(
            f"local LLM answer truncated: reasoning used the whole budget ({max_tokens} tokens)"
        )
    return strip_think(content if isinstance(content, str) else json.dumps(content))


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
