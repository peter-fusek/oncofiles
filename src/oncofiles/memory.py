"""Memory monitoring utilities for Oncofiles.

Provides RSS measurement and memory pressure checks used by the sync
scheduler (server.py) and the GDrive sync loop (sync.py).
"""

from __future__ import annotations

import gc
import logging
import sys

logger = logging.getLogger(__name__)

# Skip heavy operations when current RSS exceeds this threshold (MB)
MEMORY_THRESHOLD_MB = 450


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
