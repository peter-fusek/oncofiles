"""The ``/health`` hard RSS ceiling, relative to the container (#1012).

``/health`` is the scheduler-independent memory failsafe. Its ceiling used to
be a fixed ``HARD_RSS_CEILING_MB`` = 600 (a Railway-era value for a 512 MB
container), below the soft ``memory_check`` threshold of #1011 (sixty percent
of the cgroup limit), so the "hard" limit fired first and cut work mid-Drive
operation. Now:

* **Levels** (:func:`resolve`, read once at the ``memory_check`` loop start in a
  thread; ``/health`` only reads the result):

  - ceiling: an explicit ``HARD_RSS_CEILING_MB`` wins (``env``), else
    ``CEILING_SHARE`` of the cgroup memory limit (``cgroup``), else the old
    ``DEFAULT_CEILING_MB`` (``default``);
  - last resort: an explicit ``HARD_RSS_LAST_RESORT_MB`` wins (``env``), else
    ``LAST_RESORT_SHARE`` of the limit (``cgroup``), else the ceiling scaled by
    the same ratio (``derived``).

  The order must be soft threshold < ceiling < last resort < limit; an env
  combination that breaks it logs one warning naming the pair and is used as
  given (never swapped silently).
* **Between the ceiling and the last resort** ``/health`` exits only when idle
  (the #1011 busy signals, ``memory_check_loop.busy_reason``; unknown = busy).
  While work runs it stays alive and the operator ``/readiness.memory`` block
  reads ``state: degraded`` with ``since``; ``/health`` keeps answering 200 so
  Coolify (three failed checks) never replaces the container mid-work. With
  ``MEMORY_CHECK_RESTART`` off nothing restarts a degraded process once idle
  except this probe's own idle exit.
* **Above the last resort** it always exits (a kernel OOM kill would skip the
  graceful shutdown), with one line naming the reason code and whether work
  was running.

An exit is ``os.kill(os.getpid(), SIGTERM)`` (graceful shutdown, curated
``request_stop()``), sent once per process, never ``sys.exit``. No DB, no
blocking I/O on the request path.
"""

from __future__ import annotations

import logging
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CEILING_ENV = "HARD_RSS_CEILING_MB"
LAST_RESORT_ENV = "HARD_RSS_LAST_RESORT_MB"
DEFAULT_CEILING_MB = 600  # the Railway-era value, kept when no limit is known
CEILING_SHARE = 0.85
LAST_RESORT_SHARE = 0.95
_MB = 1024 * 1024


def _env_mb(env: dict[str, str] | os._Environ, name: str) -> int | None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value > 0:
        return value
    logger.warning("memory_ceiling: %s is not a positive integer, ignored", name)
    return None


def levels(limit_bytes: int | None, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Ceiling and last resort for a given container limit (pure)."""
    env = os.environ if environ is None else environ
    limit_mb = int(limit_bytes / _MB) if limit_bytes else None
    ceiling = _env_mb(env, CEILING_ENV)
    if ceiling is not None:
        ceiling_source = "env"
    elif limit_mb:
        ceiling, ceiling_source = max(1, int(limit_mb * CEILING_SHARE)), "cgroup"
    else:
        ceiling, ceiling_source = DEFAULT_CEILING_MB, "default"
    last = _env_mb(env, LAST_RESORT_ENV)
    if last is not None:
        last_source = "env"
    elif limit_mb:
        last, last_source = max(1, int(limit_mb * LAST_RESORT_SHARE)), "cgroup"
    else:
        last, last_source = int(ceiling * LAST_RESORT_SHARE / CEILING_SHARE), "derived"
    return {
        "ceiling_mb": ceiling,
        "ceiling_source": ceiling_source,
        "last_resort_mb": last,
        "last_resort_source": last_source,
        "limit_mb": limit_mb,
    }


def ordering_problems(soft_mb: int | None, lv: dict[str, Any]) -> list[str]:
    """Pairs that break soft < ceiling < last resort < limit (codes only)."""
    out = []
    if soft_mb is not None and not soft_mb < lv["ceiling_mb"]:
        out.append("soft_not_below_ceiling")
    if not lv["ceiling_mb"] < lv["last_resort_mb"]:
        out.append("ceiling_not_below_last_resort")
    if lv["limit_mb"] and not lv["last_resort_mb"] < lv["limit_mb"]:
        out.append("last_resort_not_below_limit")
    return out


# ── process state (in memory only) ───────────────────────────────────────

# Before the memory_check loop resolves the cgroup limit: env or defaults,
# no file read at import.
_levels: dict[str, Any] = levels(None)
_state: dict[str, Any] = {"state": "ok", "since": None, "reason": None}
_exit_sent = False


def resolve(
    soft_mb: int | None,
    environ: dict[str, str] | None = None,
    *,
    unified: Path | None = None,
    legacy: Path | None = None,
) -> dict[str, Any]:
    """Read the container limit (blocking, call in a thread) and store the
    levels for ``/health``. Logs one warning when the order is broken."""
    from oncofiles import memory_check_loop as mcl

    limit = mcl.cgroup_limit_bytes(
        unified if unified is not None else mcl.CGROUP_V2_MAX,
        legacy if legacy is not None else mcl.CGROUP_V1_LIMIT,
    )
    lv = levels(limit, environ)
    problems = ordering_problems(soft_mb, lv)
    if problems:
        logger.warning(
            "memory_ceiling: level order broken (%s): soft=%s ceiling=%d last_resort=%d limit=%s",
            ",".join(problems), soft_mb, lv["ceiling_mb"], lv["last_resort_mb"], lv["limit_mb"],
        )  # fmt: skip
    global _levels
    _levels = lv
    return lv


def _kill(reason: str, rss_mb: float, busy: str | None) -> None:
    global _exit_sent
    if _exit_sent:
        return
    _exit_sent = True
    logger.critical(
        "health: memory exit reason=%s rss_mb=%d work_running=%s busy=%s",
        reason, int(rss_mb), busy is not None, busy or "none",
    )  # fmt: skip
    os.kill(os.getpid(), signal.SIGTERM)


def check(rss_mb: float, semaphore: Any = None) -> str:
    """What ``/health`` does with this RSS. Returns ``ok`` | ``degraded`` |
    ``exit_idle`` | ``exit_last_resort`` | ``exit_sent`` (already signalled)."""
    from oncofiles.memory_check_loop import busy_reason

    lv = _levels
    if _exit_sent:
        return "exit_sent"
    if rss_mb > lv["last_resort_mb"]:
        _kill("last_resort", rss_mb, busy_reason(semaphore))
        return "exit_last_resort"
    if rss_mb > lv["ceiling_mb"]:
        busy = busy_reason(semaphore)
        if busy is None:
            _kill("ceiling_idle", rss_mb, None)
            return "exit_idle"
        if _state["state"] != "degraded":
            _state.update(state="degraded", since=datetime.now(UTC).isoformat(timespec="seconds"))
            logger.warning(
                "health: memory over ceiling, exit deferred reason=%s rss_mb=%d ceiling=%d",
                busy, int(rss_mb), lv["ceiling_mb"],
            )  # fmt: skip
        _state["reason"] = busy
        return "degraded"
    if _state["state"] != "ok":
        logger.info("health: memory back under ceiling")
    _state.update(state="ok", since=None, reason=None)
    return "ok"


def status() -> dict[str, Any]:
    """Operator ``/readiness.memory`` (in memory, no I/O)."""
    lv = _levels
    return {
        "state": _state["state"],
        "since": _state["since"],
        "deferred_reason": _state["reason"],
        "ceiling_mb": lv["ceiling_mb"],
        "ceiling_source": lv["ceiling_source"],
        "last_resort_mb": lv["last_resort_mb"],
        "last_resort_source": lv["last_resort_source"],
        "limit_mb": lv["limit_mb"],
        "exit_sent": _exit_sent,
    }


def _reset_for_tests(environ: dict[str, str] | None = None) -> None:
    global _levels, _exit_sent
    _levels = levels(None, environ if environ is not None else {})
    _state.update(state="ok", since=None, reason=None)
    _exit_sent = False
