"""Patient resolution middleware — resolves bearer token to patient_id per request.

Uses a Python contextvars.ContextVar to propagate the resolved patient_id
to tool functions without modifying the shared lifespan context.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

logger = logging.getLogger(__name__)

# Per-request patient_id — set by middleware, read by _get_patient_id()
_current_patient_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "patient_id", default="erika"
)


def get_current_patient_id() -> str:
    """Get the patient_id for the current request. Thread/task-safe."""
    return _current_patient_id.get()


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

        try:
            fastmcp_ctx = context.fastmcp_context
            if fastmcp_ctx and hasattr(fastmcp_ctx, "request_context"):
                db = fastmcp_ctx.request_context.lifespan_context.get("db")
                if db:
                    # Try to resolve from patient_tokens table
                    session = getattr(fastmcp_ctx, "_session", None)
                    if session and hasattr(session, "_access_token"):
                        token = session._access_token
                        if token:
                            resolved = await db.resolve_patient_from_token(token)
                            if resolved:
                                patient_id = resolved
        except Exception:
            logger.debug("Patient resolution failed, defaulting to 'erika'", exc_info=True)

        # Set the contextvar for this request
        token = _current_patient_id.set(patient_id)
        try:
            return await call_next(context)
        finally:
            _current_patient_id.reset(token)
