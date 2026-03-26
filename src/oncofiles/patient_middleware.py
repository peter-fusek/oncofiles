"""Patient resolution middleware — resolves bearer token to patient_id per request.

Uses a Python contextvars.ContextVar to propagate the resolved patient_id
to tool functions without modifying the shared lifespan context.
Also enforces per-token rate limiting to prevent API abuse (#147).
Includes request correlation IDs for cross-system debugging (OF-4).
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from typing import Any

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

logger = logging.getLogger(__name__)

# Per-request patient_id — set by middleware, read by _get_patient_id()
_current_patient_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "patient_id", default="erika"
)

# Per-request correlation ID (OF-4) — set by middleware, included in logs
_current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# Per-token rate limiting: {token_prefix: [timestamps]} — prevents credit depletion
_tool_call_times: dict[str, list[float]] = {}
_TOOL_RATE_LIMIT = 120  # max tool calls per minute per token
_TOOL_RATE_WINDOW = 60  # 1 minute window


def get_current_patient_id() -> str:
    """Get the patient_id for the current request. Thread/task-safe."""
    return _current_patient_id.get()


def get_current_request_id() -> str:
    """Get the request correlation ID for the current request. Thread/task-safe."""
    return _current_request_id.get()


def generate_request_id() -> str:
    """Generate a short unique request ID."""
    return uuid.uuid4().hex[:12]


class PatientResolutionMiddleware(Middleware):
    """Resolve bearer token → patient_id for every MCP tool call.

    Resolution order:
    1. patient_tokens table (SHA-256 hash of bearer token)
    2. Legacy MCP_BEARER_TOKEN → "erika" (backward compatibility)
    3. Default: "erika" (single-patient fallback)
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, ToolResult],
    ) -> ToolResult:
        patient_id = "erika"  # default
        token_key = "default"  # for rate limiting

        try:
            fastmcp_ctx = context.fastmcp_context
            if fastmcp_ctx and hasattr(fastmcp_ctx, "request_context"):
                db = fastmcp_ctx.request_context.lifespan_context.get("db")
                if db:
                    # Try to resolve from patient_tokens table
                    session = getattr(fastmcp_ctx, "_session", None)
                    if session and hasattr(session, "_access_token"):
                        raw_token = session._access_token
                        if raw_token:
                            token_key = raw_token[:16]  # prefix for rate limiting
                            resolved = await db.resolve_patient_from_token(raw_token)
                            if resolved:
                                patient_id = resolved
        except Exception:
            logger.warning("Patient resolution failed, defaulting to 'erika'", exc_info=True)

        # Rate limit: prevent token abuse / credit depletion (#147)
        now = time.time()
        if token_key not in _tool_call_times:
            _tool_call_times[token_key] = []
        calls = _tool_call_times[token_key]
        _tool_call_times[token_key] = [t for t in calls if now - t < _TOOL_RATE_WINDOW]
        if len(_tool_call_times[token_key]) >= _TOOL_RATE_LIMIT:
            logger.warning(
                "Rate limit exceeded for token %s... (%d/%d)",
                token_key[:8],
                len(calls),
                _TOOL_RATE_LIMIT,
            )
            return ToolResult(
                content=f"Rate limit exceeded ({_TOOL_RATE_LIMIT} calls/min). Try again shortly."
            )
        _tool_call_times[token_key].append(now)

        # Set contextvars for this request (patient_id + correlation ID)
        request_id = generate_request_id()
        ctx_token = _current_patient_id.set(patient_id)
        req_id_token = _current_request_id.set(request_id)
        tool_name = getattr(context, "tool_name", "unknown")
        logger.info(
            "[req:%s] tool=%s patient=%s",
            request_id,
            tool_name,
            patient_id,
        )
        try:
            return await call_next(context)
        finally:
            _current_patient_id.reset(ctx_token)
            _current_request_id.reset(req_id_token)
