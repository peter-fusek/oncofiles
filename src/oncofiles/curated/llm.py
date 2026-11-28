"""Minimal client for the local OpenAI-compatible LLM (LM Studio) (#565).

Decision 2026-11-07: OCR — and, by extension, classification — of curated
folders runs on a local vision model on the self-hosted server, so medical
document content never leaves it. Plain ``httpx`` against
``/chat/completions``; no SDK dependency.

The model "thinks" by default; ``/no_think`` is sent in the system prompt
(and, with ``thinking=False``, in the last user message, #689) and any
``<think>`` block or echoed ``/no_think`` is stripped from the answer.
With ``LLM_REASONING_MARKER`` (#770) no ``/no_think`` is sent; thinking-off
calls end the system message with ``REASONING_OFF_MARKER`` instead. An
explicit ``thinking=True`` call (#926, classification) never carries
``/no_think`` nor the off-marker: its reasoning must not hang on a template
that might one day honour the soft switch.
"""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import json
import logging
import re
import time
from urllib.parse import urlsplit

import httpx

from oncofiles import config
from oncofiles.curated import llm_lanes

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

# What ``chat`` sends as ``reasoning_effort`` unless a caller passes one. Only
# the dry-run scorecards override it, per job, next to an optional ``model``.
DEFAULT_REASONING_EFFORT = "low"
REASONING_EFFORTS = ("none", "low", "medium", "high")
MODEL_ID_MAX = 200
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9._:/@-]+")
MODELS_TIMEOUT_S = 10.0

# Named sampling profiles (#767, #926). The Qwen3.8 model card: thinking
# T 1.0 / top_p 0.95 / top_k 20, non-thinking T 0.7 / top_p 0.8 / top_k 20 /
# presence_penalty 1.5; greedy decoding (T 0) makes Qwen3-family models loop,
# worst with thinking off. ``greedy`` is the request every caller sent before.
SAMPLING_PROFILES: dict[str, dict] = {
    "greedy": {"temperature": 0.0},
    "qwen_thinking": {"temperature": 1.0, "top_p": 0.95, "top_k": 20},
    "qwen_non_thinking": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5},
}


def sampling_profile(name: str) -> dict:
    """A copy of one named profile's request parameters (``chat`` kwargs)."""
    return dict(SAMPLING_PROFILES[name])


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


# What kind of failure a ``LocalLLMError`` is, for callers that must tell a
# down model (stop) from one bad answer (this document only). Decided where
# the error is raised, never by reading its message.
TRANSPORT = "transport"  # unreachable, timeout, reset, non-200, no usable payload
ANSWER = "answer"  # the model answered: truncated, not JSON, wrong shape
UNKNOWN = "unknown"  # a plain LocalLLMError: callers treat it as transport


class LocalLLMError(RuntimeError):
    """The local model is unreachable, misconfigured, or answered nonsense.

    ``failure`` is ``TRANSPORT``, ``ANSWER`` or ``UNKNOWN``; raise one of the
    two subclasses so callers do not have to fail closed."""

    failure: str = UNKNOWN


class LocalLLMTransportError(LocalLLMError):
    """No usable answer came back: the model or the server is down or erroring."""

    failure = TRANSPORT


class LocalLLMAnswerError(LocalLLMError):
    """The model answered, but not usably for this request (a per-document problem)."""

    failure = ANSWER


class LocalLLMHostRefusedError(LocalLLMError):
    """LOCAL_LLM_BASE_URL points at a host that is not private (#925); nothing was sent."""


# #925: document text and page images go to LOCAL_LLM_BASE_URL, so it must be
# this server or its private network, never a hosted API. Names that resolve
# only on a private network (or nowhere: RFC 2606/6761 test names) pass; any
# other host needs an explicit LOCAL_LLM_ALLOWED_HOSTS entry.
PRIVATE_HOST_NAMES = frozenset({"localhost", "host.lima.internal", "host.docker.internal"})
PRIVATE_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".localhost",
    ".test",
    ".invalid",
)
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)
_refusals_logged: set[str] = set()


def _allowed_hosts() -> set[str]:
    raw = config.LOCAL_LLM_ALLOWED_HOSTS or ""
    return {h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()}


def base_url_refusal(base_url: str | None = None) -> str | None:
    """Why ``LOCAL_LLM_BASE_URL`` may not be used, or None when its host is private.

    Allowed: loopback, RFC 1918 / ULA addresses, ``host.lima.internal``,
    ``host.docker.internal``, ``localhost``, a name under ``.local``,
    ``.internal``, ``.lan``, ``.home.arpa``, ``.localhost``, ``.test`` or
    ``.invalid``, and any host listed in ``LOCAL_LLM_ALLOWED_HOSTS``. The
    reason names the host only (never the path, query or credentials).
    """
    url = config.LOCAL_LLM_BASE_URL if base_url is None else base_url
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return "LOCAL_LLM_BASE_URL is not a valid URL"
    if parts.scheme not in ("http", "https"):
        return "LOCAL_LLM_BASE_URL must be an http(s) URL"
    if not host:
        return "LOCAL_LLM_BASE_URL has no host"
    if host in _allowed_hosts() or host in PRIVATE_HOST_NAMES:
        return None
    if any(host.endswith(sfx) for sfx in PRIVATE_HOST_SUFFIXES):
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback or any(ip in net for net in _PRIVATE_NETWORKS):
            return None
    shown = host[:64]
    return (
        f"LOCAL_LLM_BASE_URL host {shown!r} is not loopback, private or allowlisted; "
        "document content may only go to a model on this server or its private "
        "network (set LOCAL_LLM_ALLOWED_HOSTS to allow a host)"
    )


def require_private_base_url() -> None:
    """Raise ``LocalLLMHostRefusedError`` (logged once per process and host) unless private."""
    reason = base_url_refusal()
    if reason is None:
        return
    if reason not in _refusals_logged:
        _refusals_logged.add(reason)
        logger.error("local LLM refused (#925): %s", reason)
    raise LocalLLMHostRefusedError(reason)


def configured() -> bool:
    return bool(config.LOCAL_LLM_BASE_URL and config.LOCAL_LLM_MODEL)


def model_id() -> str:
    return config.LOCAL_LLM_MODEL or "unconfigured"


def model_override_error(model, reasoning_effort) -> str | None:
    """Why a per-job ``model`` / ``reasoning_effort`` override is invalid, or
    None. ``model``: a non-empty string of at most ``MODEL_ID_MAX`` characters
    from ``[A-Za-z0-9._:/@-]``; ``reasoning_effort``: one of
    ``REASONING_EFFORTS``. ``None`` = no override for either."""
    if model is not None and (
        not isinstance(model, str)
        or not model
        or len(model) > MODEL_ID_MAX
        or not _MODEL_ID_RE.fullmatch(model)
    ):
        return (
            f"model must be 1-{MODEL_ID_MAX} characters of letters, digits or . _ : / @ - "
            "(an LM Studio model id), or null"
        )
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        return f"reasoning_effort must be one of {list(REASONING_EFFORTS)} or null"
    return None


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if config.LOCAL_LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LOCAL_LLM_API_KEY}"
    return headers


def native_models_url(base_url: str | None = None) -> str:
    """LM Studio's native model list (``/api/v0/models``), derived from
    ``LOCAL_LLM_BASE_URL`` with a trailing ``/v1`` stripped."""
    base = (config.LOCAL_LLM_BASE_URL if base_url is None else base_url).rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}/api/v0/models"


def model_states(*, client: httpx.Client | None = None) -> dict[str, str]:
    """``{model id: state}`` from LM Studio's native ``GET /api/v0/models``
    (same host, key and #925 check as ``chat``; blocking, read-only: it never
    loads or downloads a model). ``state`` is LM Studio's (``loaded`` /
    ``not-loaded``). Raises ``LocalLLMTransportError`` when the endpoint is
    unavailable (unreachable, 404 on an older LM Studio, any non-200, an
    unexpected body). The OpenAI ``/v1/models`` is never used instead: with
    just-in-time loading it lists models that are not loaded."""
    if not config.LOCAL_LLM_BASE_URL:
        raise LocalLLMTransportError("LOCAL_LLM_BASE_URL not set")
    require_private_base_url()  # #925
    own = client is None
    http = client or httpx.Client(timeout=MODELS_TIMEOUT_S)
    try:
        resp = http.get(native_models_url(), headers=_headers())
    except httpx.HTTPError as exc:
        raise LocalLLMTransportError(f"local LLM unreachable: {type(exc).__name__}") from exc
    finally:
        if own:
            http.close()
    if resp.status_code != 200:
        raise LocalLLMTransportError(f"local LLM HTTP {resp.status_code}")
    try:
        data = resp.json().get("data")
        if not isinstance(data, list):
            raise TypeError("data is not a list")
        return {m["id"]: m.get("state") if isinstance(m.get("state"), str) else ""
                for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)}  # fmt: skip
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        raise LocalLLMTransportError("local LLM returned an unexpected model list") from exc


def model_refusal(model: str, *, client: httpx.Client | None = None) -> str | None:
    """None only when the configured LM Studio reports ``model`` with
    ``state == "loaded"`` at its native ``/api/v0/models``, so a scorecard
    override can never make LM Studio load (JIT) another large model next to
    the production one. Else ``model_not_loaded`` (unlisted or not loaded) or
    ``model_state_unknown`` when the native list cannot be read (fail closed,
    no fallback to ``/v1/models``)."""
    try:
        states = model_states(client=client)
    except LocalLLMError:
        return "model_state_unknown"
    return None if states.get(model) == "loaded" else "model_not_loaded"


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
    """System message text. ``thinking=True`` (#926): always bare. Marker off
    (default): ``/no_think`` appended, as before #770. Marker on: the marker
    only with ``thinking=False``, else bare."""
    if thinking is True:
        return system
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
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    model: str | None = None,
    thinking: bool | None = None,
    response_format: dict | None = None,
    info: dict | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    presence_penalty: float | None = None,
    reasoning_marker: bool | None = None,
    lane: str = "background",
) -> str:
    """One blocking chat completion; returns the answer text.

    ``lane`` (#1021): ``"background"`` (default, every existing caller) or
    ``"interactive"`` (a person waiting in a chat). The request holds one
    slot of the process-wide arbiter (:mod:`llm_lanes`) while it is sent;
    interactive calls are admitted first. The request body never changes.

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
    ``reasoning_marker`` (``None`` = env ``LLM_REASONING_MARKER``; #770): when
    on, no ``/no_think`` is sent anywhere; with ``thinking=False`` the system
    message ends with ``REASONING_OFF_MARKER``, otherwise it is sent bare.
    Only the extractor call path passes it, explicitly per call (never global
    state); every other caller follows the env. Off = byte-identical request.
    ``model`` (``None`` = ``LOCAL_LLM_MODEL``): another LM Studio model id for
    this call only. Only the dry-run scorecards pass it (validated with
    ``model_override_error`` and ``model_refusal`` first); every production
    caller leaves it unset, so its request body is unchanged."""
    if not configured():
        raise LocalLLMTransportError("LOCAL_LLM_BASE_URL / LOCAL_LLM_MODEL not set")
    require_private_base_url()  # #925: before anything is built or sent
    if lane not in llm_lanes.LANES:
        raise ValueError(f"unknown local-model lane: {lane!r}")
    est = estimate_prompt_tokens(system, user_content)
    max_tokens = clamp_budget(max_tokens, est)
    if max_tokens < MIN_ANSWER_TOKENS:
        # A per-document problem (worded "truncated" so the run is not aborted
        # as "model down"): the document goes to review, it never loops.
        raise LocalLLMAnswerError(
            f"local LLM answer truncated: prompt ~{est} tokens leaves no room "
            f"in the {config.LOCAL_LLM_CONTEXT_WINDOW}-token context"
        )
    marker = config.LLM_REASONING_MARKER if reasoning_marker is None else bool(reasoning_marker)
    payload = {
        "model": model or config.LOCAL_LLM_MODEL,
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
    headers = _headers()
    own = client is None
    http = client or httpx.Client(timeout=timeout or config.LOCAL_LLM_TIMEOUT_S)
    # A caller's client keeps its own timeout unless this call sets one (#767).
    per_call = {"timeout": timeout} if timeout else {}
    t0 = time.monotonic()
    try:
        # #1021: one arbiter slot for the request only (never while building
        # or parsing); an in-flight request is never interrupted.
        # Only the production model's instance is budgeted: a per-job ``model``
        # override (scorecards, benches) runs on another LM Studio instance
        # with its own parallel slots and must not throttle production.
        guard = (
            llm_lanes.slot(lane)
            if payload["model"] == config.LOCAL_LLM_MODEL
            else contextlib.nullcontext()
        )
        with guard:
            resp = http.post(
                f"{config.LOCAL_LLM_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
                **per_call,
            )
    except httpx.HTTPError as exc:
        raise LocalLLMTransportError(f"local LLM unreachable: {type(exc).__name__}") from exc
    finally:
        if own:
            http.close()
    if resp.status_code == 400 and _is_overflow(resp):
        # Our estimate was low: same per-document path as a truncation
        # (review after limited retries), never a run abort.
        raise LocalLLMAnswerError("local LLM answer truncated: context overflow (HTTP 400)")
    if resp.status_code != 200:
        raise LocalLLMTransportError(f"local LLM HTTP {resp.status_code}")
    try:
        choice = resp.json()["choices"][0]
        content = choice["message"].get("content")
    except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
        raise LocalLLMTransportError("local LLM returned an unexpected payload") from exc
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
        raise LocalLLMAnswerError(
            f"local LLM answer truncated: reasoning used the whole budget ({max_tokens} tokens)"
        )
    return strip_think(content if isinstance(content, str) else json.dumps(content))


def _is_overflow(resp) -> bool:
    """A JSON error from the model server saying the prompt does not fit. An HTML
    or other non-JSON body (a proxy error page) is never one: that is transport."""
    try:
        resp.json()
    except ValueError:
        return False
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
        raise LocalLLMAnswerError("local LLM answer is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise LocalLLMAnswerError("local LLM answer is not a JSON object")
    return obj
