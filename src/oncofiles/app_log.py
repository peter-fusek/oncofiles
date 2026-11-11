"""Persistent, rotating application log (#706).

Container stdout is lost when Coolify recreates the container (every deploy),
so incidents older than the last deploy could not be reconstructed. With
``APP_LOG_DIR`` set (production: ``/app/data/logs``, on the persistent volume)
the root logger also writes to ``<dir>/oncofiles.log``, rotated at UTC midnight
and kept ``APP_LOG_RETENTION_DAYS`` days (default 7). Each day's file is capped
at ``APP_LOG_MAX_MB`` (default 50): past the cap one marker line is written and
records are dropped until the next rotation, so the directory never exceeds
about (days + 1) x cap. stderr keeps receiving everything.

Off by default (unset ``APP_LOG_DIR``): local dev and tests write no files.
The handler only changes WHERE lines go — what is logged is unchanged, and
the code base logs ids, counts and reasons, never document values (PHI).
"""

from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler

LOG_FILENAME = "oncofiles.log"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_MB = 50


class CappedDailyFileHandler(TimedRotatingFileHandler):
    """Daily rotation plus a per-file size cap (records past it are dropped)."""

    def __init__(self, filename: str, *, backup_days: int, max_bytes: int) -> None:
        super().__init__(
            filename, when="midnight", backupCount=backup_days, utc=True, encoding="utf-8"
        )
        self.max_bytes = max_bytes
        self._capped = False

    def doRollover(self) -> None:  # noqa: N802 (stdlib name)
        super().doRollover()
        self._capped = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.shouldRollover(record):
                self.doRollover()
            if self.max_bytes > 0:
                if self.stream is None:
                    self.stream = self._open()
                if self.stream.tell() >= self.max_bytes:
                    if not self._capped:
                        self._capped = True
                        self.stream.write(
                            "log size cap reached; further lines dropped until rotation\n"
                        )
                        self.flush()
                    return
            logging.FileHandler.emit(self, record)
        except Exception:
            self.handleError(record)


def file_handler_from_env(fmt: str, level: int) -> logging.Handler | None:
    """The persistent handler configured from env, or None when off/unusable."""
    log_dir = os.environ.get("APP_LOG_DIR", "").strip()
    if not log_dir:
        return None
    try:
        days = max(1, int(os.environ.get("APP_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))
        max_mb = max(0, int(os.environ.get("APP_LOG_MAX_MB", DEFAULT_MAX_MB)))
        os.makedirs(log_dir, exist_ok=True)
        handler = CappedDailyFileHandler(
            os.path.join(log_dir, LOG_FILENAME), backup_days=days, max_bytes=max_mb * 1024 * 1024
        )
    except (OSError, ValueError) as exc:
        # Never stop the server over a log directory; stderr still works.
        logging.getLogger(__name__).warning(
            "persistent log disabled: %s (%s)", type(exc).__name__, log_dir
        )
        return None
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))
    return handler
