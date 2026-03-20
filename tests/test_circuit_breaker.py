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
