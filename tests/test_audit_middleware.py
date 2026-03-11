"""Tests for audit middleware fire-and-forget behavior."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from oncofiles.audit_middleware import AuditMiddleware


def _make_context(tool_name: str = "test_tool", db=None):
    """Create a minimal mock MiddlewareContext."""
    context = MagicMock()
    context.message.name = tool_name
    context.message.arguments = {"key": "value"}
    context.timestamp.strftime.return_value = "20260311-1200"

    fastmcp_ctx = MagicMock()
    fastmcp_ctx.request_context.lifespan_context = {"db": db}
    context.fastmcp_context = fastmcp_ctx
    return context


@pytest.mark.asyncio
async def test_audit_middleware_non_blocking():
    """Audit log write does not block tool call response."""
    # DB mock that simulates slow commit
    db = MagicMock()
    write_started = asyncio.Event()
    write_done = asyncio.Event()

    async def slow_execute(*args):
        write_started.set()
        await asyncio.sleep(0.5)

    async def slow_commit():
        await asyncio.sleep(0.5)
        write_done.set()

    db.db = MagicMock()
    db.db.execute = slow_execute
    db.db.commit = slow_commit

    context = _make_context(db=db)

    async def call_next(_ctx):
        return "tool_result"

    middleware = AuditMiddleware()

    start = time.perf_counter()
    result = await middleware.on_call_tool(context, call_next)
    elapsed = time.perf_counter() - start

    assert result == "tool_result"
    # Tool call should return almost immediately (< 100ms), not wait for 0.5s DB write
    assert elapsed < 0.1, f"Tool call blocked for {elapsed:.3f}s — audit log is not fire-and-forget"

    # Wait for background task to complete
    await asyncio.sleep(0.1)
    # The background task should have been created (write_started should be set)
    assert write_started.is_set(), "Background audit write was never started"
