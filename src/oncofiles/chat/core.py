"""The chat core (#1020): answer one question about ONE patient's records.

``answer(db, patient_id=…, principal=…, messages=…, lang=…)`` streams
``ChatEvent`` s: ``status`` (queued | routing | tool:<name> | writing),
``delta`` (text), ``citation`` (document_id, page, label, date, verification),
``done`` (counts, seconds, model, route, grounding) or ``error`` (code + SK/EN
message). ``answer_text`` collects them into one dict.

Contract with the caller (the adapter: dashboard route #1023, WhatsApp, …):
``patient_id`` is already resolved and ACL-checked (caregiver of that
patient; a system admin who is not the caregiver gets no chat, maintainer
decision 2026-11-27, enforced by the adapter); ``principal`` is the opaque
caller key (the session e-mail on the dashboard) used for the one-answer
lock and hashed for the audit row; ``messages`` are the recent turns
(bounded, validated here; v1 keeps no server-side history, #1022).

How a turn runs (inside its own task, so the scope never leaks to the
caller): ``chat_scope(patient_id)`` fixes the patient (no admin bypass, no
slug can widen it) and ``cloud_guard.local_only("chat")`` covers every step.

1. The deterministic person check (``router.other_person``) refuses a
   question about another person BEFORE any model call.
2. The intent router (``router.route``) picks tool calls for a recognised
   intent and the model only writes the answer (``ROUTER_ANSWER_BUDGET``);
   otherwise the strict JSON next-action loop (``{"action": "tool"|"answer",
   …}``, at most ``MAX_STEPS`` model calls, ``JSON_STEP_BUDGET`` each, the
   last one told to answer; each tool result shown to the model capped at
   ``TOOL_RESULT_MODEL_BYTES``, all of them at ``TOOL_CONTEXT_BYTES`` with the
   oldest summarised to their sources; ``EMPTY_RESULTS_STOP`` tool calls in a
   row without new data end the loop with the not-found answer). Thinking
   off, no native tool_calls. A cut answer (``finish_reason=length``) is
   never shown: it takes the safe path like an empty one.
3. Guards: an empty answer becomes the deterministic not-found text with the
   tools and documents searched; every number and date must be grounded in
   this turn's tool results or the question (``grounding``), otherwise one
   regeneration naming the offending tokens, then the safe not-found text.
4. Citations only from tool results, unverified values marked, one fixed
   SK/EN disclaimer line on every answer. An ``answered`` result always
   carries at least one citation (a ``timeline`` citation without a document
   id when a planned row names none); an answer no tool result supports is
   replaced by the not-found text (``uncited_replaced``).

The model is only ``oncofiles.curated.llm.chat`` (the #925 host check
applies). There is NO cloud fallback: a busy or unavailable model is an
``error`` event (``model_busy`` / ``model_unavailable``).

Logs and the one ``activity_log`` row per turn carry counts, codes, seconds,
route and model only, never question or answer text, names or values.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from oncofiles import cloud_guard
from oncofiles.chat import grounding, prompts, router
from oncofiles.chat import tools as chat_tools
from oncofiles.chat.scope import chat_scope
from oncofiles.curated import llm

logger = logging.getLogger(__name__)

CHAT_JOB = "chat"
MAX_MESSAGES = 12  # turns the caller may send (user + assistant)
MAX_MESSAGE_BYTES = 4_000
MAX_TOTAL_BYTES = 16_000
MAX_STEPS = 3  # model calls of the JSON loop (the regeneration is extra)
# #1020 tuning after the first #1025 scorecard: the model writes ~17 tokens/s,
# so the answer length is the speed; the prompt asks for ~120 words.
ROUTER_ANSWER_BUDGET = 400  # max_tokens of the router's answer call, thinking off
JSON_STEP_BUDGET = 500  # max_tokens of one JSON-loop reply (an action or the answer)
TOOL_RESULT_MODEL_BYTES = 2_500  # one tool result as the JSON loop shows it
TOOL_CONTEXT_BYTES = 6_000  # all tool results of one JSON-loop prompt
EMPTY_RESULTS_STOP = 2  # tool calls in a row without new data end the JSON loop
# A person waits for an interactive turn; a background caller (the scorecard,
# ``llm.job_call_overrides()``) may wait longer for a slot before model_busy.
CALL_TIMEOUT_S = 90.0  # per model call, interactive
BACKGROUND_CALL_TIMEOUT_S = 300.0
INTERACTIVE_LANE_WAIT_S = 60.0  # longest wait for an arbiter slot, interactive
BACKGROUND_LANE_WAIT_S = 300.0
MAX_CITATIONS = 8
# In memory per process and UTC day. TODO(#1022): persist the per-patient
# daily turn count with the chat history.
DAILY_TURNS_PER_PATIENT = 200

_running: set[str] = set()  # principals with an answer in progress
_turns: dict[tuple[str, str], int] = {}  # (patient_id, UTC day) → accepted turns


@dataclass(frozen=True)
class ChatEvent:
    type: str  # status | delta | citation | done | error
    data: dict


class ModelUnavailableError(Exception):
    """The local model cannot answer now; ``code`` is model_busy | model_unavailable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Stats:
    route: str = "json"
    model_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    invalid_actions: int = 0
    answer_errors: int = 0
    max_steps_hit: bool = False
    early_stop: bool = False  # EMPTY_RESULTS_STOP tool calls without new data
    truncated: int = 0  # answers cut at the budget (finish=length), never shown
    tool_context_summarised: int = 0  # tool results shown to the model as sources only
    prompt_tokens: int = 0
    completion_tokens: int = 0
    lane_wait_seconds: float = 0.0  # summed waits for an arbiter slot
    lane_timeouts: int = 0
    grounding: dict = field(
        default_factory=lambda: {
            "checked": 0,
            "ungrounded": 0,
            "computed_allowed": 0,
            "regenerated": 0,
            "replaced": 0,
        }  # fmt: skip
    )
    result: str = "answered"  # answered | not_found | refused | grounding_replaced
    uncited_replaced: bool = False  # an answer without any citation became not-found


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _utc_day() -> str:
    return datetime.now(UTC).date().isoformat()


def is_busy(principal: str) -> bool:
    """True while this principal has an answer in progress (adapters pre-check it)."""
    return principal in _running


def reset_state() -> None:
    """Forget locks and counters (tests)."""
    _running.clear()
    _turns.clear()


# ── request validation ──


def validate(messages, lang) -> list[dict] | None:
    """Clean copies of the bounded recent turns, or None when anything is off."""
    if lang not in prompts.LANGS or not isinstance(messages, list):
        return None
    if not 1 <= len(messages) <= MAX_MESSAGES:
        return None
    out, total = [], 0
    for msg in messages:
        if not isinstance(msg, dict) or set(msg) != {"role", "content"}:
            return None
        role, content = msg["role"], msg["content"]
        if role not in ("user", "assistant") or not isinstance(content, str):
            return None
        content = content.strip()
        size = len(content.encode())
        if not content or size > MAX_MESSAGE_BYTES:
            return None
        total += size
        out.append({"role": role, "content": content})
    if total > MAX_TOTAL_BYTES or out[-1]["role"] != "user":
        return None
    return out


def _error(code: str, lang: str) -> ChatEvent:
    texts = prompts.ERRORS.get(code, prompts.ERRORS["internal_error"])
    return ChatEvent("error", {"code": code, "message": texts.get(lang, texts["en"])})


# ── public API ──


async def answer(
    db, *, patient_id: str, principal: str, messages: list[dict], lang: str
) -> AsyncIterator[ChatEvent]:
    """Stream the answer to the last user message (see the module docstring)."""
    lang_ok = lang if lang in prompts.LANGS else "en"
    clean = validate(messages, lang)
    if (
        clean is None
        or not isinstance(patient_id, str)
        or not patient_id
        or not isinstance(principal, str)
        or not principal
    ):
        yield _error("invalid_request", lang_ok)
        return
    if principal in _running:
        yield _error("busy", lang)
        return
    day_key = (patient_id, _utc_day())
    if _turns.get(day_key, 0) >= DAILY_TURNS_PER_PATIENT:
        yield _error("daily_limit", lang)
        return
    _turns[day_key] = _turns.get(day_key, 0) + 1
    _running.add(principal)
    queue: asyncio.Queue[ChatEvent | None] = asyncio.Queue()
    task = asyncio.create_task(_turn(db, patient_id, principal, clean, lang, queue.put_nowait))
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
        await task
    finally:
        if not task.done():
            task.cancel()
        _running.discard(principal)


async def answer_text(
    db, *, patient_id: str, principal: str, messages: list[dict], lang: str
) -> dict:
    """The whole turn as one dict: text, citations, statuses, done, error."""
    out: dict = {"text": "", "citations": [], "statuses": [], "done": None, "error": None}
    async for ev in answer(
        db, patient_id=patient_id, principal=principal, messages=messages, lang=lang
    ):
        if ev.type == "delta":
            out["text"] += ev.data["text"]
        elif ev.type == "citation":
            out["citations"].append(ev.data)
        elif ev.type == "status":
            out["statuses"].append(ev.data["status"])
        else:
            out[ev.type] = ev.data
    return out


# ── one turn ──


@dataclass
class Frame:
    patient_name: str
    vocab: router.Vocab


async def _frame(db, pid: str) -> Frame:
    """This patient's name and routing vocabulary (sequential reads, one connection)."""
    from oncofiles import patient_context

    patient = await db.get_patient(pid)
    try:
        await patient_context.ensure_loaded(db, pid)
        ctx = patient_context.get_context(pid)
    except Exception:  # noqa: BLE001 - names are a refusal aid, not required
        ctx = {}
    display = patient.display_name if patient else ""
    caregivers = [
        e.split("@", 1)[0] for e in str(getattr(patient, "caregiver_email", "") or "").split(",")
    ]
    people = await db.people_and_places_rows(patient_id=pid)
    institutions = await db.document_institution_counts(patient_id=pid)
    catalog = await db.fact_catalog_rows(patient_id=pid)
    vocabulary = await db.fact_code_vocabulary()
    org_names: dict[str, list[str]] = {}
    for org in people.get("organizations", []):
        org_names.setdefault(org.get("code") or "", []).append(org.get("name") or "")
    vocab = router.Vocab(
        codes=router.build_codes(catalog, vocabulary),
        institutions={
            value: router.institution_terms(value, org_names.get(value)) for value in institutions
        },
    )
    allowed = router.name_tokens(
        display,
        str(ctx.get("name") or ""),
        str(ctx.get("medical_record_name") or ""),
        *caregivers,
        *(p.get("display_name") or "" for p in people.get("practitioners", [])),
        *(p.get("surname_token") or "" for p in people.get("practitioners", [])),
        *(f"{o.get('code') or ''} {o.get('name') or ''}" for o in people.get("organizations", [])),
        *institutions,
    )
    for terms in [*vocab.institutions.values(), *vocab.codes.values()]:
        allowed |= router.name_tokens(*terms)
    vocab.allowed_names = allowed
    return Frame(patient_name=display, vocab=vocab)


def _background() -> bool:
    """True inside a background job's context (the chat scorecard, #1025)."""
    job = llm.job_call_overrides()
    return bool(job) and job.get("lane") == "background"


def lane_limits() -> tuple[float, float]:
    """(longest slot wait, per-call timeout) for this context: a background
    caller waits longer before ``model_busy``; a person keeps the short one."""
    if _background():
        return BACKGROUND_LANE_WAIT_S, BACKGROUND_CALL_TIMEOUT_S
    return INTERACTIVE_LANE_WAIT_S, CALL_TIMEOUT_S


def _call_llm(system: str, user: str, info: dict, max_tokens: int = ROUTER_ANSWER_BUDGET) -> str:
    """The one place the chat hands content to the local model (thinking off).

    Content = this patient's tool results (read tools, raw genotype excluded
    by ``chat.tools``) and the caller's question; no page text is read."""
    slot_wait, call_timeout = lane_limits()
    return llm.chat(
        system,
        user,
        max_tokens=max_tokens,
        timeout=call_timeout,
        thinking=False,
        info=info,
        lane="interactive",  # #1021/#1023: a person is waiting for this answer
        slot_timeout=slot_wait,
        **llm.sampling_profile("qwen_non_thinking"),
    )


def _unavailable_code(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, llm.LocalLLMHostRefusedError):
        return "model_unavailable"
    if isinstance(exc, llm.LocalLLMLaneBusyError):
        return "model_busy"
    if re.search(r"HTTP (429|503)|Timeout", text):
        return "model_busy"
    return "model_unavailable"


def _lane_counts(stats: Stats, info: dict) -> None:
    wait = info.get("lane_wait_s")
    if isinstance(wait, int | float) and not isinstance(wait, bool):
        stats.lane_wait_seconds = round(stats.lane_wait_seconds + float(wait), 3)
    if info.get("lane_timeout"):
        stats.lane_timeouts += 1


async def _model(system: str, user: str, stats: Stats, max_tokens: int) -> str:
    stats.model_calls += 1
    info: dict = {}
    try:
        text = await asyncio.to_thread(_call_llm, system, user, info, max_tokens)
    except llm.LocalLLMAnswerError:
        _lane_counts(stats, info)
        stats.answer_errors += 1
        return ""
    except llm.LocalLLMError as exc:
        _lane_counts(stats, info)
        raise ModelUnavailableError(_unavailable_code(exc)) from exc
    _lane_counts(stats, info)
    stats.prompt_tokens += int(info.get("prompt_tokens") or 0)
    stats.completion_tokens += int(info.get("completion_tokens") or 0)
    if info.get("finish_reason") == "length":
        stats.truncated += 1  # never a silent cut: the safe path takes over
        return ""
    return (text or "").strip()


async def _tool(db, name: str, args, stats: Stats, emit) -> chat_tools.ToolResult:
    emit(ChatEvent("status", {"status": f"tool:{name if name in chat_tools.SPECS else 'unknown'}"}))
    stats.tool_calls += 1
    result = await chat_tools.run_tool(db, name, args)
    if not result.ok:
        stats.tool_errors += 1
    return result


def _newest_document_id(result: chat_tools.ToolResult) -> int | None:
    docs = result.data.get("documents") if result.ok and isinstance(result.data, dict) else None
    rows = [d for d in docs or [] if isinstance(d, dict) and isinstance(d.get("id"), int)]
    rows = [d for d in rows if not d.get("excluded")]
    if not rows:
        return None
    return max(rows, key=lambda d: (str(d.get("document_date") or ""), d["id"]))["id"]


async def _router_path(db, frame, plan, question, history, today, stats, emit):
    results: list[chat_tools.ToolResult] = []
    for name, args in plan.calls:
        result = await _tool(db, name, args, stats, emit)
        results.append(result)
        if plan.detail_newest and (doc_id := _newest_document_id(result)) is not None:
            results.append(await _tool(db, "document_detail", {"document_id": doc_id}, stats, emit))
    blocks = "\n\n".join(prompts.data_block(r.name, r.args, r.text) for r in results)
    user = (
        prompts.history_block(history)
        + f"Question: {question}\n\nTool results:\n{blocks or '(none)'}"
    )
    system = prompts.router_system(frame.patient_name, today)
    emit(ChatEvent("status", {"status": "writing"}))
    text = await _model(system, user, stats, ROUTER_ANSWER_BUDGET)

    async def regenerate(bad: list[str]) -> str:
        emit(ChatEvent("status", {"status": "writing"}))
        again = user + "\n\n" + prompts.regenerate_note(bad)
        return await _model(system, again, stats, ROUTER_ANSWER_BUDGET)

    return text, results, regenerate


def _parse_action(raw: str) -> dict | None:
    try:
        obj = llm.parse_json_object(raw)
    except llm.LocalLLMAnswerError:
        return None
    action = obj.get("action")
    if action == "answer" and isinstance(obj.get("text"), str):
        return {"action": "answer", "text": obj["text"]}
    if action == "tool":
        name = obj.get("name") or obj.get("tool")
        if isinstance(name, str):
            args = obj.get("args")
            return {"action": "tool", "name": name, "args": {} if args is None else args}
    return None


_COUNT_KEYS = frozenset({"total", "count", "duplicates_removed"})


def _has_data(obj) -> bool:
    """True when a tool result holds anything: a non-empty list or a value
    other than a count (``{"total": 0}`` and ``{}`` are empty)."""
    if isinstance(obj, list):
        return any(_has_data(v) for v in obj) if obj else False
    if isinstance(obj, dict):
        return any(_has_data(v) for k, v in obj.items() if k not in _COUNT_KEYS)
    return obj not in (None, "", 0, False)


def _sources_of(result: chat_tools.ToolResult) -> list[str]:
    """A tool result reduced to its sources (document ids with dates, or
    timeline dates): what an older result keeps in the JSON loop's prompt."""
    out: list[str] = []
    for c in chat_tools.citation_candidates([result]):
        if c.kind == "timeline":
            entry = f"timeline {c.date[:10]}" if c.date else "timeline"
        else:
            entry = f"doc {c.document_id}" + (f" {c.date[:10]}" if c.date else "")
        if entry not in out:
            out.append(entry)
        if len(out) >= 8:
            break
    return out


@dataclass
class _Step:
    shown: str  # the action as JSON
    result: chat_tools.ToolResult
    note: str
    full: str = ""  # the result capped at TOOL_RESULT_MODEL_BYTES
    short: str = ""  # the result summarised to its sources


def _step_text(step: _Step, summarised: bool) -> str:
    return f"\nYour action: {step.shown}\n{step.short if summarised else step.full}\n{step.note}"


def _render(head: str, steps: list[_Step], tail: str, stats: Stats) -> str:
    """The JSON loop's prompt: every step's tool result in full (each capped)
    while all of them fit ``TOOL_CONTEXT_BYTES``; past it the oldest are
    summarised to their sources first."""
    summarised = [False] * len(steps)

    def size() -> int:
        return sum(
            len((s.short if summed else s.full).encode())
            for s, summed in zip(steps, summarised, strict=True)
        )

    for i in range(len(steps)):
        if size() <= TOOL_CONTEXT_BYTES:
            break
        summarised[i] = True
    stats.tool_context_summarised = max(stats.tool_context_summarised, sum(summarised))
    body = "".join(_step_text(s, summed) for s, summed in zip(steps, summarised, strict=True))
    return head + body + tail


def _model_view(result: chat_tools.ToolResult) -> str:
    """One tool result as the JSON loop shows it: at most
    ``TOOL_RESULT_MODEL_BYTES`` (``fit`` drops the middle of the longest list
    with a marker, or cuts the text with one). ``fit`` shortens in place, so
    it works on a copy: citations still see the whole result."""
    text = result.text
    if result.ok and len(text.encode()) > TOOL_RESULT_MODEL_BYTES:
        text = chat_tools.fit(copy.deepcopy(result.data), TOOL_RESULT_MODEL_BYTES)
    return prompts.data_block(result.name, result.args, text)


async def _json_path(db, frame, question, history, today, stats, emit):
    system = prompts.json_system(
        frame.patient_name, today, chat_tools.tools_text(), max_steps=MAX_STEPS
    )
    head = prompts.history_block(history) + f"Question: {question}\n"
    steps: list[_Step] = []
    notes = ""  # loop notes after the steps (invalid actions)
    results: list[chat_tools.ToolResult] = []
    seen: dict[str, chat_tools.ToolResult] = {}
    seen_texts: set[str] = set()
    empty_in_a_row = 0
    text = ""
    for step in range(MAX_STEPS):
        last = step == MAX_STEPS - 1
        tail = notes + ("\n" + prompts.LAST_STEP_NOTE + "\n" if last else "")
        emit(ChatEvent("status", {"status": "routing"}))
        raw = await _model(system, _render(head, steps, tail, stats), stats, JSON_STEP_BUDGET)
        action = _parse_action(raw)
        if action is None:
            stats.invalid_actions += 1
            notes += (
                "\n[note] That was not one valid JSON action. Reply with exactly one JSON "
                "object as specified.\n"
            )
            continue
        if action["action"] == "answer":
            text = action["text"].strip()
            break
        if last:  # no reply left to read a result: best effort below, never an error
            stats.max_steps_hit = True
            break
        key = json.dumps([action["name"], action["args"]], sort_keys=True, default=str)
        if key in seen:
            result = seen[key]
            note = "\n[note] You already called this tool with these arguments; answer now.\n"
            new_data = False
        else:
            result = await _tool(db, action["name"], action["args"], stats, emit)
            seen[key] = result
            results.append(result)
            note = ""
            new_data = result.ok and _has_data(result.data) and result.text not in seen_texts
            seen_texts.add(result.text)
        shown = json.dumps(
            {"action": "tool", "name": result.name, "args": result.args}, ensure_ascii=False
        )
        steps.append(
            _Step(
                shown,
                result,
                note,
                full=_model_view(result),
                short=prompts.summarised_block(result.name, result.args, _sources_of(result)),
            )
        )
        empty_in_a_row = 0 if new_data else empty_in_a_row + 1
        if empty_in_a_row >= EMPTY_RESULTS_STOP:
            stats.early_stop = True  # nothing new twice: the not-found answer, no more calls
            break
    else:
        stats.max_steps_hit = True
    transcript = _render(head, steps, notes, stats)

    async def regenerate(bad: list[str]) -> str:
        emit(ChatEvent("status", {"status": "writing"}))
        prior = json.dumps({"action": "answer", "text": text}, ensure_ascii=False)
        again = (
            transcript
            + f"\nYour action: {prior}\n[note] {prompts.regenerate_note(bad)} Reply with "
            '{"action": "answer", "text": "..."}.\n'
        )
        action = _parse_action(await _model(system, again, stats, JSON_STEP_BUDGET))
        return action["text"].strip() if action and action["action"] == "answer" else ""

    if text:
        emit(ChatEvent("status", {"status": "writing"}))
    return text, results, regenerate


def _not_found_text(results: list[chat_tools.ToolResult], lang: str) -> str:
    names = list(dict.fromkeys(r.name for r in results))
    searched = ", ".join(names) if names else prompts.NOTHING_SEARCHED[lang]
    lines = [prompts.NOT_FOUND[lang], f"{prompts.SEARCHED[lang]}: {searched}."]
    docs: list[str] = []
    for c in chat_tools.citation_candidates(results):
        if c.document_id is None:
            continue
        entry = f"[doc {c.document_id}" + (f", {c.date[:10]}" if c.date else "") + "]"
        if entry not in docs:
            docs.append(entry)
    if docs:
        lines.append(f"{prompts.DOCUMENTS_SEEN[lang].capitalize()}: {', '.join(docs[:5])}.")
    return "\n".join(lines)


_DOC_REF = re.compile(r"\bdoc\w*\s*#?\s*(\d+)")


def _cited(candidates) -> list[dict]:
    """Citation events (deduplicated, at most ``MAX_CITATIONS``). ``kind`` is
    ``document`` or ``timeline`` (a timeline row with no source document: a
    date and a label, ``document_id`` None)."""
    out, keys = [], set()
    for c in candidates:
        key = (c.kind, c.document_id, c.page, c.label, c.date)
        if key in keys:
            continue
        keys.add(key)
        out.append({
            "kind": c.kind, "document_id": c.document_id, "page": c.page, "label": c.label,
            "date": c.date, "evidence": c.evidence, "verification": c.verification,
            "verification_label": c.verification_label,
            "unverified": c.verification == "unverified",
        })  # fmt: skip
        if len(out) >= MAX_CITATIONS:
            break
    return out


def _citations(text: str, results: list[chat_tools.ToolResult]) -> list[dict]:
    answer_tokens = set(grounding.tokens(text))
    refs = {int(m) for m in _DOC_REF.findall(grounding.fold(text))}
    scored: list[tuple[int, int, chat_tools.Candidate]] = []
    for i, c in enumerate(chat_tools.citation_candidates(results)):
        value_hit = bool(set(grounding.tokens(" ".join(c.values[:1]))) & answer_tokens)
        date_hit = bool(set(grounding.tokens(c.date or "")) & answer_tokens)
        score = 2 * value_hit + date_hit + (c.document_id is not None and c.document_id in refs)
        if score:
            scored.append((-score, i, c))
    return _cited(c for _s, _i, c in sorted(scored, key=lambda t: (t[0], t[1])))


_LABEL_STOP = frozenset({"other", "labs", "report", "unknown", "document", "dokument", "sprava"})
FALLBACK_PER_RESULT = 3


def _label_words(label: str | None) -> set[str]:
    words = re.findall(r"[a-z]{5,}", grounding.fold(label or ""))
    return {w for w in words if w not in _LABEL_STOP}


def _fallback_citations(text: str, results: list[chat_tools.ToolResult], route: str) -> list[dict]:
    """Citations when no row matched the answer's values, dates or document
    ids: (1) the rows of every tool result that holds a number or date of the
    answer; (2) the rows whose label words the answer repeats; (3) on the
    planned route, the nearest planned row. Empty = nothing supports it."""
    answer_tokens = set(grounding.tokens(text))
    picked: list[chat_tools.Candidate] = []
    if answer_tokens:
        for r in results:
            if r.ok and answer_tokens & grounding.pool([r.text]):
                picked += chat_tools.citation_candidates([r])[:FALLBACK_PER_RESULT]
    if not picked:
        words = set(re.findall(r"[a-z]{5,}", grounding.fold(text)))
        picked = [
            c for c in chat_tools.citation_candidates(results) if _label_words(c.label) & words
        ]
    if not picked and route == "router:planned":
        rows = [
            c
            for r in results
            if r.name == "timeline"
            for c in chat_tools.citation_candidates([r])
            if c.date
        ]
        picked = sorted(rows, key=lambda c: c.date)[:1]
    return _cited(picked)


async def _guard(text, results, question, today, regenerate, stats, lang) -> str:
    """Empty-answer and grounding guards; returns the text to show."""
    if not text.strip():
        stats.result = "not_found"
        return _not_found_text(results, lang)
    sources = [r.text for r in results] + [question, today]
    check = grounding.check(text, sources)
    stats.grounding["checked"] += check.checked
    stats.grounding["computed_allowed"] += check.computed_allowed
    if check.ok:
        return text
    stats.grounding["ungrounded"] += len(check.ungrounded)
    stats.grounding["regenerated"] += 1
    text = await regenerate(check.ungrounded)
    if not text.strip():
        stats.result = "not_found"
        return _not_found_text(results, lang)
    check = grounding.check(text, sources)
    stats.grounding["checked"] += check.checked
    stats.grounding["computed_allowed"] += check.computed_allowed
    if check.ok:
        return text
    stats.grounding["ungrounded"] += len(check.ungrounded)
    stats.grounding["replaced"] += 1
    stats.result = "grounding_replaced"
    return _not_found_text(results, lang)


async def _turn(db, pid, principal, messages, lang, emit: Callable[[ChatEvent | None], None]):
    t0 = time.monotonic()
    stats = Stats()
    error_code: str | None = None
    try:
        with chat_scope(pid), cloud_guard.local_only(CHAT_JOB):
            emit(ChatEvent("status", {"status": "queued"}))
            frame = await _frame(db, pid)
            question, history = messages[-1]["content"], messages[:-1]
            today = _today()
            emit(ChatEvent("status", {"status": "routing"}))
            citations: list[dict] = []
            if router.other_person(question, frame.vocab.allowed_names):
                stats.route, stats.result = "refused", "refused"
                shown = prompts.REFUSE_OTHER_PERSON[lang]
            else:
                plan = router.route(question, frame.vocab, today)
                if plan is not None:
                    stats.route = f"router:{plan.intent}"
                    text, results, regen = await _router_path(
                        db, frame, plan, question, history, today, stats, emit
                    )
                else:
                    text, results, regen = await _json_path(
                        db, frame, question, history, today, stats, emit
                    )
                shown = await _guard(text, results, question, today, regen, stats, lang)
                if stats.result == "answered":
                    citations = _citations(shown, results) or _fallback_citations(
                        shown, results, stats.route
                    )
                    if not citations:  # invariant: an answer always names its source
                        stats.result, stats.uncited_replaced = "not_found", True
                        shown = _not_found_text(results, lang)
            emit(ChatEvent("delta", {"text": shown}))
            if any(c["unverified"] for c in citations):
                emit(ChatEvent("delta", {"text": "\n\n" + prompts.UNVERIFIED_NOTE[lang]}))
            for c in citations:
                emit(ChatEvent("citation", c))
            emit(ChatEvent("delta", {"text": "\n\n" + prompts.DISCLAIMER[lang]}))
            emit(ChatEvent("done", _done(stats, t0, len(citations), lang)))
    except ModelUnavailableError as exc:
        error_code = exc.code
        err = _error(exc.code, lang)
        # counts only: how long the turn waited for a model slot (scorecard #1025)
        err.data.update(lane_wait_seconds=stats.lane_wait_seconds,
                        lane_timeouts=stats.lane_timeouts)  # fmt: skip
        emit(err)
    except asyncio.CancelledError:
        error_code = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 - one failed turn never takes the caller down
        error_code = "internal_error"
        logger.warning("chat: turn failed (%s)", type(exc).__name__)
        emit(_error("internal_error", lang))
    finally:
        seconds = time.monotonic() - t0
        logger.info(
            "chat turn: route=%s result=%s model_calls=%d tool_calls=%d tool_errors=%d "
            "invalid_actions=%d grounding=%s seconds=%.1f model=%s error=%s",
            stats.route, stats.result, stats.model_calls, stats.tool_calls, stats.tool_errors,
            stats.invalid_actions, json.dumps(stats.grounding, sort_keys=True), seconds,
            llm.model_id(), error_code,
        )  # fmt: skip
        if error_code != "cancelled":
            await _audit(db, pid, principal, stats, seconds, error_code)
        emit(None)


def _done(stats: Stats, t0: float, n_citations: int, lang: str) -> dict:
    data = asdict(stats)
    data.update({
        "seconds": round(time.monotonic() - t0, 2),
        "model": llm.model_id(),
        "citations": n_citations,
        "disclaimer": prompts.DISCLAIMER[lang],
    })  # fmt: skip
    return data


async def _audit(db, pid, principal, stats: Stats, seconds: float, error_code) -> None:
    """One ``activity_log`` row per turn: counts and codes, the caller as a keyed hash."""
    from oncofiles.models import ActivityLogEntry

    try:
        from oncofiles.secrets_keys import caller_email_hash

        caller = caller_email_hash(principal)
    except Exception:  # noqa: BLE001 - no key configured: no hash
        caller = None
    summary = {
        "route": stats.route,
        "result": stats.result if error_code is None else "error",
        "error": error_code,
        "model_calls": stats.model_calls,
        "tool_calls": stats.tool_calls,
        "tool_errors": stats.tool_errors,
        "invalid_actions": stats.invalid_actions,
        "grounding": stats.grounding,
        "truncated": stats.truncated,
        "early_stop": stats.early_stop,
        "uncited_replaced": stats.uncited_replaced,
        "lane_wait_seconds": stats.lane_wait_seconds,
        "lane_timeouts": stats.lane_timeouts,
        "model": llm.model_id(),
    }
    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=pid,
                session_id="chat",
                agent_id="chat",
                tool_name="chat_turn",
                input_summary=json.dumps({"caller_email_hash": caller}),
                output_summary=json.dumps(summary, sort_keys=True),
                duration_ms=int(seconds * 1000),
                status="ok" if error_code is None else "error",
            )
        )
    except Exception as exc:  # noqa: BLE001 - the audit never changes the answer
        logger.warning("chat: audit row failed (%s)", type(exc).__name__)
