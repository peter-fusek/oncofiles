"""Memory and concurrency utilities for Oncofiles.

Provides RSS measurement, memory pressure checks, and query concurrency
limits used by the sync scheduler, GDrive sync loop, and MCP tools.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Skip heavy operations when current RSS exceeds this threshold (MB)
MEMORY_THRESHOLD_MB = 450

# Limit concurrent heavy DB queries (search_conversations, search_documents)
# to prevent memory spikes from parallel Oncoteam agent requests.
_query_semaphore = asyncio.Semaphore(3)

# Priority-aware DB concurrency — prevents dashboard 500s during sync.
# Background (sync) and express (dashboard) get independent lanes so sync
# operations can never starve dashboard queries.
_db_background = asyncio.Semaphore(2)  # sync / background operations
_db_express = asyncio.Semaphore(2)  # dashboard / priority operations


async def acquire_query_slot(label: str) -> None:
    """Acquire a slot for a heavy query. Logs when queuing occurs."""
    if _query_semaphore._value == 0:
        logger.info("Query queued — all 3 slots busy: %s", label)
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


# RSS threshold for graceful self-restart (below OOM but above normal)
RESTART_THRESHOLD_MB = 400


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
