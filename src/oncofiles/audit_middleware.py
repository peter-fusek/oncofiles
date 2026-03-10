"""Audit logging middleware — auto-logs every tool call to activity_log."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

logger = logging.getLogger(__name__)


class AuditMiddleware(Middleware):
    """Automatically logs every tool call to the activity_log table.

    Captures tool name, duration, status, and error details without
    requiring manual logging from Oncoteam or other agents.
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

        try:
            result = await call_next(context)
            return result
        except Exception as e:
            status = "error"
            error_message = str(e)[:500]
            raise
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)

            # Get DB from lifespan context (if available)
            try:
                fastmcp_ctx = context.fastmcp_context
                if fastmcp_ctx and hasattr(fastmcp_ctx, "request_context"):
                    db = fastmcp_ctx.request_context.lifespan_context.get("db")
                    if db:
                        session_id = _get_session_id(context)
                        await db.db.execute(
                            """
                            INSERT INTO activity_log
                                (session_id, agent_id, tool_name, input_summary,
                                 duration_ms, status, error_message)
                            VALUES (?, 'auto', ?, ?, ?, ?, ?)
                            """,
                            (
                                session_id,
                                tool_name,
                                _summarize_input(context.message),
                                duration_ms,
                                status,
                                error_message,
                            ),
                        )
                        await db.db.commit()
            except Exception:
                logger.debug("Audit log write failed for %s", tool_name, exc_info=True)


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
