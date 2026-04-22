"""Tests for the Turso circuit breaker."""

import time

import pytest

from oncofiles.database._base import _CircuitBreaker


def test_closed_by_default():
    cb = _CircuitBreaker()
    assert cb.state == _CircuitBreaker.CLOSED
    cb.check()  # should not raise


def test_opens_after_max_failures():
    cb = _CircuitBreaker(max_failures=3, window=60.0, cooldown=10.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == _CircuitBreaker.CLOSED
    cb.record_failure()
    assert cb.state == _CircuitBreaker.OPEN
    with pytest.raises(RuntimeError, match="Circuit breaker open"):
        cb.check()


def test_success_resets_failures():
    cb = _CircuitBreaker(max_failures=3, window=60.0, cooldown=10.0)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.state == _CircuitBreaker.CLOSED


def test_transitions_to_half_open_after_cooldown():
    cb = _CircuitBreaker(max_failures=2, window=60.0, cooldown=1.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == _CircuitBreaker.OPEN
    # Simulate cooldown elapsed
    cb._opened_at = time.monotonic() - 2.0
    assert cb.state == _CircuitBreaker.HALF_OPEN
    cb.check()  # should not raise in half-open


def test_old_failures_outside_window_ignored():
    cb = _CircuitBreaker(max_failures=3, window=5.0, cooldown=10.0)
    # Simulate old failures outside the window
    old = time.monotonic() - 10.0
    cb._failure_times = [old, old]
    cb.record_failure()
    assert cb.state == _CircuitBreaker.CLOSED  # old ones pruned


# ── stats() for /readiness (#469 Phase 3) ────────────────────────────


def test_stats_closed_state_initial():
    cb = _CircuitBreaker(max_failures=3, window=60.0, cooldown=30.0)
    stats = cb.stats()
    assert stats["state"] == _CircuitBreaker.CLOSED
    assert stats["failures_in_window"] == 0
    assert stats["trip_count_total"] == 0
    assert stats["last_trip_at"] is None
    assert stats["last_trip_cause"] is None
    assert stats["cooldown_remaining_s"] == 0.0
    assert stats["max_failures"] == 3
    assert stats["window_seconds"] == 60.0
    assert stats["cooldown_seconds"] == 30.0


def test_stats_open_state_records_trip_metadata():
    cb = _CircuitBreaker(max_failures=2, window=60.0, cooldown=30.0)
    cb.record_failure("TimeoutError: query exceeded 30s")
    cb.record_failure("PanicException: driver crash")

    stats = cb.stats()
    assert stats["state"] == _CircuitBreaker.OPEN
    assert stats["failures_in_window"] == 2
    assert stats["trip_count_total"] == 1
    assert stats["last_trip_at"] is not None
    # Cause recorded at moment of OPEN transition (2nd failure).
    assert "PanicException" in stats["last_trip_cause"]
    # Cooldown is counting down.
    assert 0.0 < stats["cooldown_remaining_s"] <= 30.0


def test_stats_trip_count_tracks_each_open_transition():
    cb = _CircuitBreaker(max_failures=2, window=60.0, cooldown=0.5)
    # First trip
    cb.record_failure("first cause")
    cb.record_failure("first cause")
    assert cb.stats()["trip_count_total"] == 1
    # Recover
    cb.record_success()
    # Second trip with different cause
    cb.record_failure("second cause")
    cb.record_failure("second cause")
    stats = cb.stats()
    assert stats["trip_count_total"] == 2
    assert "second cause" in stats["last_trip_cause"]


def test_stats_cause_is_truncated():
    cb = _CircuitBreaker(max_failures=1, window=60.0, cooldown=1.0)
    long_cause = "X" * 500
    cb.record_failure(long_cause)
    stats = cb.stats()
    assert stats["last_trip_cause"] is not None
    # Truncated at _CAUSE_MAX_LEN
    assert len(stats["last_trip_cause"]) == cb._CAUSE_MAX_LEN


def test_stats_no_trip_on_single_failure():
    cb = _CircuitBreaker(max_failures=3, window=60.0, cooldown=30.0)
    cb.record_failure("one-off blip")
    stats = cb.stats()
    assert stats["state"] == _CircuitBreaker.CLOSED
    assert stats["failures_in_window"] == 1
    assert stats["trip_count_total"] == 0
    assert stats["last_trip_at"] is None
    assert stats["last_trip_cause"] is None
