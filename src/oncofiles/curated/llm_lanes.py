"""Process-wide arbiter of the shared local model (#1021): interactive first.

Every :func:`oncofiles.curated.llm.chat` call holds one slot of the local
model for its whole request. A call declares a lane:

* ``background`` (the default): curated runs, re-OCR / re-read, the holding
  reader, extraction previews, scorecards. Every existing caller.
* ``interactive``: a person waiting in a chat (#1020).

Slot model (infra constraints of #1021, 2026-11-28; Oncofiles owns every
LM Studio slot, so there is no share against other apps):

* ``LLM_SLOTS_TOTAL`` (runtime env, default 4 = LM Studio's ``parallel``)
  is the budget for every lane together; nothing ever holds more.
* Interactive calls are admitted first: a free slot goes to a waiting
  interactive call before any waiting background call.
* While interactive work is *present* (a call in flight, a call waiting, or
  one ended less than ``LLM_INTERACTIVE_HOLD_S`` ago, so the slot stays
  reserved between the turns of one conversation) background is capped at
  ``LLM_BACKGROUND_MAX_WHILE_INTERACTIVE`` (default 3) slots.
* Background never takes a slot an interactive caller is waiting for (one
  free slot per waiting interactive call is reserved).
* An in-flight call is never interrupted: the guarantee is "the next free
  slot is the chat's", so a chat may still wait for one running background
  call to finish (at most ``LOCAL_LLM_TIMEOUT_S``).
* Fairness: a background call that has waited longer than
  ``LLM_BACKGROUND_MAX_WAIT_S`` (default 120 s) no longer yields to waiting
  interactive calls: it takes the next free slot, so a long chat session can
  never starve curation. It still respects the background cap above, which
  is the infra limit "background uses at most 3 of the 4 slots while a chat
  is active". 120 s is about two chat turns of the #1019 spike timings; a
  starved page is delayed at most that long. Background callers are served
  first come, first served among themselves.

Without any interactive call (production today: nothing passes
``lane="interactive"`` yet) background is admitted while fewer than
``LLM_SLOTS_TOTAL`` calls are in flight. Production never runs more than four
concurrent model calls, so this only moves a queue that would otherwise form
inside LM Studio into the process; request bodies are unchanged.

The arbiter uses threading primitives only (``llm.chat`` runs in
``asyncio.to_thread`` workers) and never touches the DB. Telemetry
(:func:`status`) is counts and seconds only and is served on operator
``/readiness``, never ``/health``.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

INTERACTIVE = "interactive"
BACKGROUND = "background"
LANES = (INTERACTIVE, BACKGROUND)

WAIT_SAMPLES = 200  # last N admissions per lane for the wait p50 / p95
TURN_WINDOW_S = 3600.0  # "interactive turns in the last hour"
CHECKPOINT_MAX_WAIT_S = 30.0  # longest pause of a background checkpoint


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw) if raw.strip() else default
    except ValueError:
        logger.warning("llm lanes: %s is not an integer; using %d", name, default)
        value = default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "")
    try:
        value = float(raw) if raw.strip() else default
    except ValueError:
        logger.warning("llm lanes: %s is not a number; using %s", name, default)
        value = default
    return max(minimum, value)


def _settings() -> dict:
    """The env knobs, read at call time (runtime env, patchable in tests)."""
    total = _env_int("LLM_SLOTS_TOTAL", 4, 1)
    cap = _env_int("LLM_BACKGROUND_MAX_WHILE_INTERACTIVE", 3, 0)
    return {
        "slots_total": total,
        "background_max_while_interactive": min(cap, total),
        "background_max_wait_s": _env_float("LLM_BACKGROUND_MAX_WAIT_S", 120.0, 0.0),
        "interactive_hold_s": _env_float("LLM_INTERACTIVE_HOLD_S", 120.0, 0.0),
    }


class LaneBusyError(TimeoutError):
    """No slot within the caller's ``timeout`` (nothing was sent)."""


class Arbiter:
    """Slot accounting for one process. Thread-safe; no I/O."""

    def __init__(self, settings=_settings, clock=time.monotonic) -> None:
        self._settings = settings
        self._clock = clock
        self._cond = threading.Condition(threading.Lock())
        self._active = {INTERACTIVE: 0, BACKGROUND: 0}
        self._waiting_interactive = 0
        self._bg_queue: deque[tuple[int, float, int]] = deque()  # (ticket, since, slots)
        self._tickets = itertools.count()
        self._last_interactive_end: float | None = None
        self._waits = {lane: deque(maxlen=WAIT_SAMPLES) for lane in LANES}
        self._turns: deque[float] = deque()
        self._fairness_grants = 0
        self._admitted = {INTERACTIVE: 0, BACKGROUND: 0}

    # -- state, caller holds the lock -------------------------------------
    def _in_flight(self) -> int:
        return self._active[INTERACTIVE] + self._active[BACKGROUND]

    def _interactive_present(self, s: dict, now: float) -> bool:
        if self._active[INTERACTIVE] or self._waiting_interactive:
            return True
        last = self._last_interactive_end
        return last is not None and now - last < s["interactive_hold_s"]

    def _starved_reserve(self, s: dict, now: float) -> int:
        """Slots an interactive caller must leave to a starved background head
        (fairness), so the two never race for the same freed slot."""
        if not self._bg_queue:
            return 0
        _ticket, since, slots = self._bg_queue[0]
        if now - since < s["background_max_wait_s"]:
            return 0
        if self._active[BACKGROUND] + slots > s["background_max_while_interactive"]:
            return 0  # the cap holds it back anyway
        return slots

    def _bg_admissible(self, ticket: int, since: float, slots: int, s: dict, now: float) -> bool:
        if not self._bg_queue or self._bg_queue[0][0] != ticket:
            return False  # first come, first served among background callers
        free = s["slots_total"] - self._in_flight()
        starved = now - since >= s["background_max_wait_s"]
        if not starved:
            free -= self._waiting_interactive  # reserved for waiting chats
        if free < slots:
            return False
        if self._interactive_present(s, now):
            return self._active[BACKGROUND] + slots <= s["background_max_while_interactive"]
        return True

    # -- public -------------------------------------------------------------
    def acquire(self, lane: str = BACKGROUND, *, slots: int = 1, timeout: float | None = None):
        """Block until ``slots`` slots are granted; returns a release handle.

        ``slots`` > 1 is for a child process that makes that many calls at
        once (the re-OCR child). ``timeout`` (seconds) raises
        :class:`LaneBusyError` when nothing was granted in time."""
        if lane not in LANES:
            raise ValueError(f"unknown local-model lane: {lane!r}")
        slots = max(1, int(slots))
        t0 = self._clock()
        deadline = None if timeout is None else t0 + timeout
        with self._cond:
            s = self._settings()
            slots = min(slots, s["slots_total"])
            fairness = False
            if lane == INTERACTIVE:
                self._waiting_interactive += 1
                try:
                    while s["slots_total"] - self._in_flight() < slots + self._starved_reserve(
                        s, self._clock()
                    ):
                        self._wait(deadline)
                        s = self._settings()
                finally:
                    self._waiting_interactive -= 1
                    self._cond.notify_all()  # a slot reserved for it may be free again
                self._turns.append(self._clock())
            else:
                ticket = next(self._tickets)
                entry = (ticket, t0, slots)
                self._bg_queue.append(entry)
                try:
                    while not self._bg_admissible(ticket, t0, slots, s, self._clock()):
                        self._wait(deadline, t0 + s["background_max_wait_s"])
                        s = self._settings()
                    now = self._clock()
                    fairness = (
                        self._waiting_interactive > 0 and now - t0 >= s["background_max_wait_s"]
                    )
                finally:
                    self._bg_queue.remove(entry)
                    self._cond.notify_all()  # the next background caller is head now
            self._active[lane] += slots
            self._admitted[lane] += 1
            if fairness:
                self._fairness_grants += 1
            self._waits[lane].append(self._clock() - t0)
        return _Lease(self, lane, slots)

    def _wait(self, deadline: float | None, wake_at: float | None = None) -> None:
        now = self._clock()
        if deadline is not None and now >= deadline:
            raise LaneBusyError("local model busy: no slot within the timeout")
        ends = [t for t in (deadline, wake_at) if t is not None and t > now]
        # A bounded wait also re-checks the hold window and the fairness clock.
        self._cond.wait(timeout=min([*(t - now for t in ends), 1.0]))

    def _release(self, lane: str, slots: int) -> None:
        with self._cond:
            self._active[lane] = max(0, self._active[lane] - slots)
            if lane == INTERACTIVE:
                self._last_interactive_end = self._clock()
            self._cond.notify_all()

    @contextlib.contextmanager
    def slot(self, lane: str = BACKGROUND, *, slots: int = 1, timeout: float | None = None):
        lease = self.acquire(lane, slots=slots, timeout=timeout)
        try:
            yield
        finally:
            lease.release()

    def interactive_waiting(self) -> bool:
        with self._cond:
            return self._waiting_interactive > 0

    def interactive_active(self) -> bool:
        """An interactive call is in flight, waiting, or ended within the hold."""
        with self._cond:
            return self._interactive_present(self._settings(), self._clock())

    def yield_to_interactive(self, max_wait_s: float = CHECKPOINT_MAX_WAIT_S) -> float:
        """Background checkpoint: pause (at most ``max_wait_s``) while an
        interactive call waits for a slot. Never interrupts anything; returns
        the seconds waited (0.0 at once when no chat waits)."""
        t0 = self._clock()
        end = t0 + max(0.0, max_wait_s)
        with self._cond:
            while self._waiting_interactive > 0:
                now = self._clock()
                if now >= end:
                    break
                self._cond.wait(timeout=min(end - now, 1.0))
        return self._clock() - t0

    def status(self) -> dict:
        """Counts and seconds only (operator ``/readiness``)."""
        with self._cond:
            s = self._settings()
            now = self._clock()
            while self._turns and now - self._turns[0] > TURN_WINDOW_S:
                self._turns.popleft()
            return {
                **s,
                "active": dict(self._active),
                "waiting": {
                    INTERACTIVE: self._waiting_interactive,
                    BACKGROUND: len(self._bg_queue),
                },
                "interactive_present": self._interactive_present(s, now),
                "admitted": dict(self._admitted),
                "fairness_grants": self._fairness_grants,
                "interactive_turns_last_hour": len(self._turns),
                "wait_s": {lane: _percentiles(self._waits[lane]) for lane in LANES},
            }


class _Lease:
    __slots__ = ("_arbiter", "_lane", "_slots", "_done")

    def __init__(self, arbiter: Arbiter, lane: str, slots: int) -> None:
        self._arbiter, self._lane, self._slots, self._done = arbiter, lane, slots, False

    def release(self) -> None:
        if not self._done:
            self._done = True
            self._arbiter._release(self._lane, self._slots)


def _percentiles(samples) -> dict:
    values = sorted(samples)
    if not values:
        return {"n": 0, "p50": None, "p95": None}

    def pick(q: float) -> float:
        return round(values[min(len(values) - 1, int(q * len(values)))], 3)

    return {"n": len(values), "p50": pick(0.50), "p95": pick(0.95)}


_ARBITER = Arbiter()


def arbiter() -> Arbiter:
    return _ARBITER


def slot(lane: str = BACKGROUND, *, slots: int = 1, timeout: float | None = None):
    """Context manager holding ``slots`` slots of the process arbiter."""
    return _ARBITER.slot(lane, slots=slots, timeout=timeout)


def interactive_waiting() -> bool:
    return _ARBITER.interactive_waiting()


def interactive_active() -> bool:
    return _ARBITER.interactive_active()


def yield_to_interactive(max_wait_s: float = CHECKPOINT_MAX_WAIT_S) -> float:
    return _ARBITER.yield_to_interactive(max_wait_s)


def status() -> dict:
    return _ARBITER.status()
