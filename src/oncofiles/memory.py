"""Memory and concurrency utilities for Oncofiles.

Provides RSS measurement, memory pressure checks, and query concurrency
limits used by the sync scheduler, GDrive sync loop, and MCP tools.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import sys

logger = logging.getLogger(__name__)

# Skip heavy operations when current RSS exceeds this threshold (MB)
MEMORY_THRESHOLD_MB = 450

# Limit concurrent heavy DB queries (search_conversations, search_documents)
# to prevent memory spikes from parallel Oncoteam agent requests.
_query_semaphore = asyncio.Semaphore(3)


async def acquire_query_slot(label: str) -> None:
    """Acquire a slot for a heavy query. Logs when queuing occurs."""
    if _query_semaphore._value == 0:
        logger.info("Query queued — all 3 slots busy: %s", label)
    await _query_semaphore.acquire()


def release_query_slot() -> None:
    """Release a heavy query slot."""
    _query_semaphore.release()


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
    gc.collect()
    return True
