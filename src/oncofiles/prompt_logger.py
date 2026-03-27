"""Fire-and-forget prompt logging for AI call observability."""

from __future__ import annotations

import asyncio
import json
import logging

from oncofiles.models import PromptCallType, PromptLogEntry

logger = logging.getLogger(__name__)

_MAX_PROMPT_LOG_TASKS = 100
_PROMPT_LOG_TIMEOUT = 5.0
_prompt_log_tasks: set[asyncio.Task] = set()


def _extract_result_summary(call_type: str, raw_response: str) -> str:
    """Extract a short human-readable summary from the raw AI response.

    No extra AI call — just parses the response and extracts the relevant field.
    """
    if not raw_response:
        return ""

    try:
        if call_type == PromptCallType.SUMMARY_TAGS:
            parsed = json.loads(raw_response)
            return str(parsed.get("summary", ""))[:200]
        elif call_type == PromptCallType.STRUCTURED_METADATA:
            parsed = json.loads(raw_response)
            return str(parsed.get("plain_summary", ""))[:200]
        elif call_type == PromptCallType.FILENAME_DESCRIPTION:
            return raw_response.strip()[:200]
        elif call_type == PromptCallType.OCR:
            # First 150 chars of extracted text, cleaned
            cleaned = " ".join(raw_response.split())
            return cleaned[:150]
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    # Fallback: first 200 chars of raw response
    return raw_response[:200]


async def _write_prompt_log(db, entry: PromptLogEntry) -> None:
    """Write prompt log entry to database with timeout."""
    try:
        await asyncio.wait_for(
            db.insert_prompt_log(entry),
            timeout=_PROMPT_LOG_TIMEOUT,
        )
    except TimeoutError:
        logger.debug("Prompt log write timed out for %s", entry.call_type)
    except Exception:
        logger.debug("Prompt log write failed for %s", entry.call_type, exc_info=True)


def log_ai_call(
    db,
    *,
    call_type: str,
    document_id: int | None = None,
    model: str,
    system_prompt: str,
    user_prompt: str,
    raw_response: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int,
    status: str = "ok",
    error_message: str | None = None,
) -> None:
    """Schedule a fire-and-forget prompt log write. Never raises, never blocks.

    Safe to call from synchronous code running on the async event loop.
    If no event loop is running (e.g. called from asyncio.to_thread), silently skips.
    """
    if db is None:
        return

    # Capture patient_id from context (set by PatientResolutionMiddleware)
    try:
        from oncofiles.patient_middleware import get_current_patient_id

        patient_id = get_current_patient_id()
    except Exception:
        patient_id = ""

    result_summary = _extract_result_summary(call_type, raw_response)

    entry = PromptLogEntry(
        call_type=call_type,
        document_id=document_id,
        patient_id=patient_id,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        raw_response=raw_response,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
        result_summary=result_summary,
        status=status,
        error_message=error_message,
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Not in async context (e.g. called from thread via asyncio.to_thread)
        return

    # Bounded fire-and-forget
    if len(_prompt_log_tasks) >= _MAX_PROMPT_LOG_TASKS:
        oldest = next(iter(_prompt_log_tasks))
        oldest.cancel()
        _prompt_log_tasks.discard(oldest)

    task = loop.create_task(_write_prompt_log(db, entry))
    _prompt_log_tasks.add(task)
    task.add_done_callback(_prompt_log_tasks.discard)
