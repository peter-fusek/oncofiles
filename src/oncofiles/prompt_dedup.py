"""Prompt-hash dedup for AI calls (#441 Layer 5).

The prompt cache (Layer 4) discounts repeated tokens by ~90%. The dedup
helper goes one step further: when an AI call has the EXACT SAME inputs
as a recent successful call, return the persisted response without
hitting Anthropic at all.

Design:
- Hash key: SHA-256(system_prompt || user_prompt || model)
- Window: rolling 30 days (matches Anthropic's longer cache windows;
  beyond 30d the table is auto-pruned by the prompt_log retention job)
- Storage: ``prompt_log.prompt_hash`` (migration 069) — indexed for O(log N)
  lookup
- Hit path: log a synthetic dedup-hit row so cache hit rate is observable
  via the same /api/usage-analytics surface
- Miss path: silent — caller proceeds to the normal ``client.messages.create``

Skip for analyses where the candidate set or graph state changes between
calls (relationships, consolidation). Those callers do NOT use this
helper. Composition / vaccination / classification / summary / metadata /
lab-values / filename-description ARE deduplicated.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

from oncofiles.models import PromptLogEntry

logger = logging.getLogger(__name__)


def compute_prompt_hash(system_prompt: str, user_prompt: str, model: str) -> str:
    """Stable hash of the inputs that determine the AI response.

    Uses SHA-256 to avoid collision concerns at any realistic scale
    (we'd need ~2^128 calls before the first collision is likely).
    """
    h = hashlib.sha256()
    h.update(b"system\x00")
    h.update(system_prompt.encode("utf-8", errors="replace"))
    h.update(b"\x01user\x00")
    h.update(user_prompt.encode("utf-8", errors="replace"))
    h.update(b"\x01model\x00")
    h.update(model.encode("utf-8", errors="replace"))
    return h.hexdigest()


@dataclass
class CachedResponse:
    """Result of a dedup hit. The raw_response is the EXACT bytes the
    original Anthropic call returned, so the caller can parse it the
    same way it would parse a fresh response."""

    raw_response: str
    original_id: int  # prompt_log.id of the source row — for audit trail
    age_seconds: float  # how stale the hit is — informational


_DEDUP_WINDOW_DAYS = 30


async def maybe_get_cached_response(
    db,
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
) -> CachedResponse | None:
    """Look up a recent successful AI call with identical inputs.

    Returns None on:
      - cache miss (no matching hash)
      - matching row older than 30 days
      - matching row with status != 'ok' (failed calls aren't authoritative)
      - DB error (fail-open: a dedup miss costs one Anthropic call,
        a dedup-error fail-closed would block the pipeline)

    Always patient-scoped via the caller's context (the SQL filter on
    ``patient_id`` prevents one patient's response from satisfying
    another patient's query — even though hashes won't collide in
    practice, the explicit filter is defence-in-depth and covers the
    edge case where two patients upload the exact same document).
    """
    if db is None:
        return None
    prompt_hash = compute_prompt_hash(system_prompt, user_prompt, model)

    # Patient scope from the same ContextVar that prompt_logger uses.
    try:
        from oncofiles.patient_middleware import get_current_patient_id

        patient_id = get_current_patient_id() or "__system_no_patient__"
    except Exception:
        patient_id = "__system_no_patient__"

    sql = """
        SELECT id, raw_response, created_at
          FROM prompt_log
         WHERE prompt_hash = ?
           AND patient_id = ?
           AND status = 'ok'
           AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
         ORDER BY id DESC
         LIMIT 1
    """
    window_arg = f"-{_DEDUP_WINDOW_DAYS} days"
    try:
        async with db.db.execute(sql, (prompt_hash, patient_id, window_arg)) as cursor:
            row = await cursor.fetchone()
    except Exception:
        # Fail-open: a transient DB error must not block AI calls.
        logger.debug("prompt_dedup lookup failed", exc_info=True)
        return None

    if row is None:
        return None

    # Compute age. created_at is ISO-8601 UTC; subtract from now.
    try:
        from datetime import UTC, datetime

        created_dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        age = (datetime.now(UTC) - created_dt).total_seconds()
    except Exception:
        age = 0.0

    return CachedResponse(
        raw_response=row["raw_response"] or "",
        original_id=int(row["id"] or 0),
        age_seconds=age,
    )


def log_dedup_hit(
    db,
    *,
    call_type: str,
    document_id: int | None,
    model: str,
    system_prompt: str,
    user_prompt: str,
    raw_response: str,
    duration_ms: int,
    original_id: int,
) -> None:
    """Persist a synthetic dedup-hit row so cache hit rate is observable.

    The row is identifiable by ``status='dedup_hit'`` and a tiny
    ``error_message`` field that records the source row id. We store
    the same ``raw_response`` so future dedup queries can keep hitting
    on this row (the original might roll out of the 30-day window first).
    Token counts are 0 — this hit cost zero Anthropic input/output tokens.
    """
    if db is None:
        return
    # Late import to avoid the prompt_logger ↔ prompt_dedup import cycle.
    import asyncio

    from oncofiles.prompt_logger import _write_prompt_log

    no_patient_sentinel = "__system_no_patient__"
    try:
        from oncofiles.patient_middleware import get_current_patient_id

        patient_id = get_current_patient_id() or no_patient_sentinel
    except Exception:
        patient_id = no_patient_sentinel

    prompt_hash = compute_prompt_hash(system_prompt, user_prompt, model)
    entry = PromptLogEntry(
        call_type=call_type,
        document_id=document_id,
        patient_id=patient_id,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        raw_response=raw_response,
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        duration_ms=duration_ms,
        result_summary=f"[dedup_hit from #{original_id}]",
        status="dedup_hit",
        error_message=f"src_id={original_id}",
        prompt_hash=prompt_hash,
    )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_write_prompt_log(db, entry))


def stable_system_prompt_text(system_blocks: list[dict] | str) -> str:
    """Flatten the system argument we pass to ``client.messages.create``
    into a single string for hashing.

    Anthropic's ``system`` parameter accepts either a plain string or a
    list of content blocks. When we pass blocks (the post-#441-Layer-4
    shape), the dedup hash needs to fold them into one canonical form
    so the lookup matches the original storage shape.
    """
    if isinstance(system_blocks, str):
        return system_blocks
    parts = []
    for b in system_blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\x02".join(parts)


def measure_call_start() -> float:
    """Tiny helper so AI call sites can record duration consistently."""
    return time.perf_counter()


def measure_call_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
