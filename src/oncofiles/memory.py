"""Memory and concurrency utilities for Oncofiles.

Provides RSS measurement, memory pressure checks, and query concurrency
limits used by the sync scheduler, GDrive sync loop, and MCP tools.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Skip heavy operations when current RSS exceeds this threshold (MB)
MEMORY_THRESHOLD_MB = 450

# Configurable concurrency slots via env vars for tuning without redeploy (OF-3)
_QUERY_SLOTS = int(os.environ.get("DB_QUERY_SLOTS", "5"))
_EXPRESS_SLOTS = int(os.environ.get("DB_EXPRESS_SLOTS", "5"))
_BACKGROUND_SLOTS = int(os.environ.get("DB_BACKGROUND_SLOTS", "2"))

# Limit concurrent heavy DB queries (search_conversations, search_documents)
# to prevent memory spikes from parallel Oncoteam agent requests.
_query_semaphore = asyncio.Semaphore(_QUERY_SLOTS)

# Priority-aware DB concurrency — prevents dashboard 500s during sync.
# Background (sync) and express (dashboard) get independent lanes so sync
# operations can never starve dashboard queries.
_db_background = asyncio.Semaphore(_BACKGROUND_SLOTS)  # sync / background operations
_db_express = asyncio.Semaphore(_EXPRESS_SLOTS)  # dashboard / priority operations

# ── RSS trend tracking (OF-1) ──────────────────────────────────────────────
_rss_startup: float = 0.0
_rss_peak: float = 0.0
_rss_started_at: float = 0.0

# Graceful restart tracking (OF-2)
MEMORY_RESTART_THRESHOLD_MB = int(os.environ.get("MEMORY_RESTART_THRESHOLD_MB", "420"))
HARD_RSS_CEILING_MB = int(os.environ.get("HARD_RSS_CEILING_MB", "600"))


async def acquire_query_slot(label: str) -> None:
    """Acquire a slot for a heavy query. Logs when queuing occurs."""
    if _query_semaphore._value == 0:
        logger.info("Query queued — all %d slots busy: %s", _QUERY_SLOTS, label)
    await _query_semaphore.acquire()


def release_query_slot() -> None:
    """Release a heavy query slot."""
    _query_semaphore.release()


@asynccontextmanager
async def query_slot(label: str) -> AsyncIterator[None]:
    """Async context manager for heavy query concurrency control."""
    await acquire_query_slot(label)
    try:
        yield
    finally:
        release_query_slot()


@asynccontextmanager
async def db_slot(label: str, *, priority: bool = False) -> AsyncIterator[None]:
    """Acquire a DB concurrency slot.

    priority=True  → express lane (dashboard, /status)
    priority=False → background lane (sync, housekeeping)
    """
    sem = _db_express if priority else _db_background
    lane = "express" if priority else "background"
    if sem._value == 0:
        logger.info("DB slot queued — %s lane full: %s", lane, label)
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()


def get_rss_mb() -> float:
    """Return current RSS in MB (not peak).

    On Linux (Railway): reads /proc/self/statm for live RSS.
    On macOS (dev): falls back to ru_maxrss (peak, but acceptable for dev).
    """
    try:
        with open("/proc/self/statm") as f:
            parts = f.read().split()
        # Field 1 = resident pages; multiply by page size (4096)
        return int(parts[1]) * 4096 / (1024 * 1024)
    except (FileNotFoundError, IndexError, ValueError):
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def malloc_trim() -> None:
    """Return freed memory pages to the OS (Linux only).

    CPython's arena allocator doesn't return freed pages to the OS by default,
    causing RSS to grow monotonically even after gc.collect(). malloc_trim()
    forces glibc to release unused heap pages back to the kernel.

    No-op on macOS (dev) — only effective on Linux (Railway).
    """
    if sys.platform != "linux":
        return
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except (OSError, AttributeError):
        pass  # Not available — silently skip


def reclaim_memory(label: str) -> float:
    """Run gc.collect() + malloc_trim() and return current RSS.

    Call this after heavy operations (sync, search, enhance) to aggressively
    return memory to the OS.
    """
    gc.collect()
    malloc_trim()
    rss = get_rss_mb()
    logger.info("Memory reclaimed after %s — RSS: %.1f MB", label, rss)
    return rss


# RESTART_THRESHOLD_MB removed — use MEMORY_RESTART_THRESHOLD_MB everywhere (see OF-2)


def is_memory_pressure(label: str) -> bool:
    """Check if RSS exceeds threshold; if so, log a warning, run gc, and return True.

    Use this as a guard at the top of scheduled sync functions to skip
    heavy work when memory is tight.

    Args:
        label: Human-readable name for the operation (e.g. "sync", "Gmail sync").
    """
    rss_mb = get_rss_mb()
    if rss_mb <= MEMORY_THRESHOLD_MB:
        return False
    logger.warning(
        "Skipping %s — RSS %.1f MB exceeds %d MB threshold",
        label,
        rss_mb,
        MEMORY_THRESHOLD_MB,
    )
    reclaim_memory(label)
    return True


# ── OF-1: RSS trend tracking ──────────────────────────────────────────────


def init_rss_tracking() -> None:
    """Initialize RSS tracking at startup. Call once in lifespan."""
    global _rss_startup, _rss_peak, _rss_started_at
    _rss_startup = get_rss_mb()
    _rss_peak = _rss_startup
    _rss_started_at = time.time()


def update_peak_rss() -> float:
    """Update peak RSS and return current value. Call after heavy operations."""
    global _rss_peak
    rss = get_rss_mb()
    if rss > _rss_peak:
        _rss_peak = rss
    return rss


def get_rss_trend() -> dict:
    """Return RSS trend data for /health endpoint."""
    current = get_rss_mb()
    elapsed_h = (time.time() - _rss_started_at) / 3600 if _rss_started_at else 0
    growth_rate = (current - _rss_startup) / elapsed_h if elapsed_h > 0.01 else 0.0
    return {
        "current_mb": round(current, 1),
        "startup_mb": round(_rss_startup, 1),
        "peak_mb": round(_rss_peak, 1),
        "growth_rate_mb_per_hour": round(growth_rate, 1),
    }


def get_semaphore_status() -> dict:
    """Return current semaphore slot availability for /health endpoint (OF-3)."""
    return {
        "query": {"total": _QUERY_SLOTS, "available": _query_semaphore._value},
        "express": {"total": _EXPRESS_SLOTS, "available": _db_express._value},
        "background": {"total": _BACKGROUND_SLOTS, "available": _db_background._value},
    }


# ── OF-2: Graceful restart on memory pressure ─────────────────────────────


def check_memory_restart() -> bool:
    """Check if RSS exceeds restart threshold after reclaiming memory.

    Returns True if the process should gracefully restart.
    Reclaims memory first to avoid false positives from transient spikes.
    """
    rss_before = get_rss_mb()
    if rss_before <= MEMORY_RESTART_THRESHOLD_MB:
        return False
    # Above threshold — try to reclaim first (gc.collect + malloc_trim)
    rss_after = reclaim_memory("pre-restart-check")
    if rss_after > MEMORY_RESTART_THRESHOLD_MB:
        logger.critical(
            "Graceful restart: RSS %.1f MB (was %.1f before reclaim) exceeds %d MB",
            rss_after,
            rss_before,
            MEMORY_RESTART_THRESHOLD_MB,
        )
        return True
    logger.info(
        "RSS dropped %.1f -> %.1f MB after reclaim — restart averted",
        rss_before,
        rss_after,
    )
    return False


async def periodic_memory_check() -> None:
    """Periodic memory maintenance. Called every 5 minutes by scheduler.

    Must be async so APScheduler runs it on the event loop thread —
    sys.exit/SIGTERM from a sync function in a thread pool is silently
    swallowed by APScheduler (the root cause of #213 restart failure).

    1. Update peak RSS
    2. Reclaim if above threshold, restart if still high
    """
    update_peak_rss()

    if check_memory_restart():
        logger.critical("Initiating graceful restart via SIGTERM")
        import signal

        os.kill(os.getpid(), signal.SIGTERM)
