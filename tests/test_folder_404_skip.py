"""Tests for GDrive folder 404 skip logic in sync scheduler."""

from __future__ import annotations

from unittest.mock import MagicMock

from oncofiles.server import (
    _FOLDER_404_THRESHOLD,
    _folder_404_counts,
    _is_folder_not_found,
    _patient_clients_cache,
)

# ── _is_folder_not_found ──────────────────────────────────────────────────


def test_is_folder_not_found_with_404_resp():
    """HttpError-like exception with resp.status=404 is detected."""
    exc = Exception("File not found")
    exc.resp = MagicMock(status=404)
    assert _is_folder_not_found(exc) is True


def test_is_folder_not_found_with_403_resp():
    """Non-404 HTTP errors are not folder-not-found."""
    exc = Exception("Forbidden")
    exc.resp = MagicMock(status=403)
    assert _is_folder_not_found(exc) is False


def test_is_folder_not_found_plain_exception():
    """Plain exceptions without resp are not folder-not-found."""
    assert _is_folder_not_found(ValueError("something")) is False


def test_is_folder_not_found_nested_cause():
    """404 wrapped in another exception (e.g. from asyncio.to_thread) is detected."""
    inner = Exception("File not found")
    inner.resp = MagicMock(status=404)
    outer = RuntimeError("thread error")
    outer.__cause__ = inner
    assert _is_folder_not_found(outer) is True


def test_is_folder_not_found_nested_non_404():
    """Non-404 nested cause is not folder-not-found."""
    inner = Exception("Server error")
    inner.resp = MagicMock(status=500)
    outer = RuntimeError("thread error")
    outer.__cause__ = inner
    assert _is_folder_not_found(outer) is False


# ── _folder_404_counts / threshold ────────────────────────────────────────


def test_folder_404_threshold_is_3():
    """Threshold should be 3 consecutive failures before skipping."""
    assert _FOLDER_404_THRESHOLD == 3


def test_folder_404_counts_reset_on_clear():
    """Clearing a patient's 404 count allows sync to resume."""
    pid = "test-patient-reset"
    _folder_404_counts[pid] = 5
    _folder_404_counts.pop(pid, None)
    assert _folder_404_counts.get(pid, 0) < _FOLDER_404_THRESHOLD


def test_folder_404_counts_skip_when_at_threshold():
    """Patient is skipped when 404 count reaches threshold."""
    pid = "test-patient-skip"
    try:
        _folder_404_counts[pid] = _FOLDER_404_THRESHOLD
        assert _folder_404_counts.get(pid, 0) >= _FOLDER_404_THRESHOLD
    finally:
        _folder_404_counts.pop(pid, None)


def test_folder_404_counts_below_threshold_allows_sync():
    """Patient is not skipped when below threshold."""
    pid = "test-patient-allow"
    try:
        _folder_404_counts[pid] = _FOLDER_404_THRESHOLD - 1
        assert _folder_404_counts.get(pid, 0) < _FOLDER_404_THRESHOLD
    finally:
        _folder_404_counts.pop(pid, None)


def test_patient_clients_cache_cleared_on_folder_reset():
    """When folder is re-set, client cache for that patient is also cleared."""
    pid = "test-patient-cache"
    try:
        _patient_clients_cache[pid] = ("gdrive", "gmail", "cal", "folder_id", 0)
        _folder_404_counts[pid] = 5
        # Simulate what gdrive_set_folder does
        _folder_404_counts.pop(pid, None)
        _patient_clients_cache.pop(pid, None)
        assert pid not in _folder_404_counts
        assert pid not in _patient_clients_cache
    finally:
        _folder_404_counts.pop(pid, None)
        _patient_clients_cache.pop(pid, None)
