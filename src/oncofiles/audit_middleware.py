"""Audit logging middleware — auto-logs every tool call to activity_log."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Any

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

logger = logging.getLogger(__name__)

# Bounded set of in-flight audit tasks to prevent accumulation
_MAX_AUDIT_TASKS = 100
_AUDIT_TIMEOUT = 5.0  # seconds per audit write
_audit_tasks: set[asyncio.Task] = set()

# Tag carried by every auto row written after #663, so pre-fix rows (which all
# got the migration-029 column default) stay distinguishable from new ones.
ATTRIBUTED_TAG = '["attributed"]'

# Per-call collector of the patient ids the tool actually resolved (#663).
# The middleware installs a fresh set before `call_next`; `_resolve_patient_id`
# and `_get_patient_id` add to it. A mutable set (not a re-set ContextVar) so a
# value added inside a copied context, e.g. a task spawned by the tool, is
# still seen here.
_call_patient_ids: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar(
    "audit_call_patient_ids", default=None
)


def note_resolved_patient(patient_id: str) -> None:
    """Record that the current tool call resolved `patient_id` (no-op outside a call)."""
    bucket = _call_patient_ids.get()
    if bucket is not None and patient_id:
        bucket.add(patient_id)


def attributed_patient(resolved: set[str]) -> str:
    """The patient a tool call is logged under: the one it resolved, else ''.

    Never the default or the bound patient by itself: a call that resolved no
    patient, or several different ones, is logged without a patient ('' is the
    "no patient" value add_activity_log already uses). The no-access sentinel
    never counts as a patient.
    """
    from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL

    real = {p for p in resolved if p and p != NO_PATIENT_ACCESS_SENTINEL}
    return next(iter(real)) if len(real) == 1 else ""


async def _write_audit_log(
    db,
    session_id,
    tool_name,
    input_summary,
    duration_ms,
    status,
    error_message,
    patient_id="",
):
    """Write audit log entry in background — never blocks tool response."""
    try:
        await db.db.execute(
            """
            INSERT INTO activity_log
                (session_id, agent_id, tool_name, input_summary,
                 duration_ms, status, error_message, tags, patient_id)
            VALUES (?, 'auto', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                tool_name,
                input_summary,
                duration_ms,
                status,
                error_message,
                ATTRIBUTED_TAG,
                patient_id,
            ),
        )
        await db.db.commit()
    except Exception:
        logger.debug("Audit log write failed for %s", tool_name, exc_info=True)


class AuditMiddleware(Middleware):
    """Automatically logs every tool call to the activity_log table.

    Captures tool name, duration, status, and error details without
    requiring manual logging from Oncoteam or other agents.
    Uses fire-and-forget to avoid blocking tool responses.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, ToolResult],
    ) -> ToolResult:
        tool_name = getattr(context.message, "name", "unknown")
        start = time.perf_counter()
        status = "ok"
        error_message = None
        resolved: set[str] = set()
        bucket_token = _call_patient_ids.set(resolved)

        try:
            result = await call_next(context)
            return result
        except Exception as e:
            status = "error"
            error_message = str(e)[:500]
            raise
        finally:
            _call_patient_ids.reset(bucket_token)
            duration_ms = int((time.perf_counter() - start) * 1000)
            patient_id = attributed_patient(resolved)

            # Fire-and-forget: audit log write runs in background
            try:
                fastmcp_ctx = context.fastmcp_context
                if fastmcp_ctx and hasattr(fastmcp_ctx, "request_context"):
                    db = fastmcp_ctx.request_context.lifespan_context.get("db")
                    if db:
                        session_id = _get_session_id(context)
                        input_summary = _summarize_input(context.message)
                        # Bounded fire-and-forget: drop oldest if at capacity
                        if len(_audit_tasks) >= _MAX_AUDIT_TASKS:
                            logger.warning(
                                "Audit task limit reached (%d), dropping oldest",
                                _MAX_AUDIT_TASKS,
                            )
                            oldest = next(iter(_audit_tasks))
                            oldest.cancel()
                            _audit_tasks.discard(oldest)

                        async def _timed_audit():
                            try:
                                await asyncio.wait_for(
                                    _write_audit_log(
                                        db,
                                        session_id,
                                        tool_name,
                                        input_summary,
                                        duration_ms,
                                        status,
                                        error_message,
                                        patient_id,
                                    ),
                                    timeout=_AUDIT_TIMEOUT,
                                )
                            except TimeoutError:
                                logger.debug("Audit write timed out for %s", tool_name)

                        task = asyncio.create_task(_timed_audit())
                        _audit_tasks.add(task)
                        task.add_done_callback(_audit_tasks.discard)
            except Exception:
                logger.debug("Audit log dispatch failed for %s", tool_name, exc_info=True)


def _get_session_id(context: MiddlewareContext[Any]) -> str:
    """Extract a session identifier from the context."""
    # Use timestamp-based session ID (MCP doesn't provide one)
    ts = context.timestamp.strftime("%Y%m%d-%H%M")
    return f"auto-{ts}"


def _summarize_input(message: Any) -> str:
    """Create a brief summary of tool input arguments."""
    try:
        args = getattr(message, "arguments", None)
        if args and isinstance(args, dict):
            # Truncate long values
            summary_parts = []
            for k, v in args.items():
                v_str = str(v)
                if len(v_str) > 100:
                    v_str = v_str[:100] + "..."
                summary_parts.append(f"{k}={v_str}")
            return ", ".join(summary_parts)[:500]
    except Exception:
        pass
    return ""
