"""Tests for no-patient-access sentinel → first-class ValueError UX.

Post-#478, multi-patient OAuth callers without a caregiver_email match
(or without a bound email at all) get NO_PATIENT_ACCESS_SENTINEL from
_resolve_oauth_patient. Before this fix, downstream tools treated the
sentinel as a valid pid → DB query matched zero rows → caller saw
silent empty results with no hint about how to fix it.

After: _get_patient_id detects the sentinel and raises a distinct
ValueError with a 3-step remediation message (sign in on dashboard →
verify caregiver_email → reconnect claude.ai). Distinct from the
"No patient selected" error so the dashboard / client can render a
different hint for each state.
"""

from __future__ import annotations

import pytest

from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
from oncofiles.patient_middleware import _current_patient_id
from oncofiles.tools._helpers import _get_patient_id


def test_sentinel_raises_with_remediation_hint():
    """Required=True + sentinel pid → ValueError naming the 3-step fix."""
    token = _current_patient_id.set(NO_PATIENT_ACCESS_SENTINEL)
    try:
        with pytest.raises(ValueError) as exc_info:
            _get_patient_id(required=True)
        msg = str(exc_info.value)
        # Core message beats
        assert "No patient access resolved" in msg
        assert "caregiver_email" in msg
        assert "dashboard" in msg
        # Remediation steps appear
        assert "sign in" in msg.lower() or "Sign in" in msg
        assert "reconnect" in msg.lower() or "claude.ai" in msg.lower()
        # Explicit workaround for CLI users
        assert "patient_slug" in msg
    finally:
        _current_patient_id.reset(token)


def test_sentinel_without_required_returns_empty_string():
    """Bootstrapping tools (list_patients, select_patient) pass required=False
    and should get "" not the sentinel — otherwise they'd silently query
    WHERE patient_id='__no_patient_access__' too."""
    token = _current_patient_id.set(NO_PATIENT_ACCESS_SENTINEL)
    try:
        result = _get_patient_id(required=False)
        assert result == ""
    finally:
        _current_patient_id.reset(token)


def test_empty_string_raises_different_message():
    """The "no patient selected" error is distinct from the sentinel error.

    Empty string means "middleware didn't set a patient AT ALL" (stdio
    without config, or a ContextVar never populated). That's a different
    UX from "authenticated but no caregiver match" — dashboard / client
    should show different hints.
    """
    token = _current_patient_id.set("")
    try:
        with pytest.raises(ValueError) as exc_info:
            _get_patient_id(required=True)
        msg = str(exc_info.value)
        assert "No patient selected" in msg
        # Must NOT carry the sentinel-specific caregiver_email hint
        assert "caregiver_email" not in msg
        assert "reconnect" not in msg.lower()
    finally:
        _current_patient_id.reset(token)


def test_valid_pid_passes_through_unchanged():
    """Sanity: a normal UUID just gets returned."""
    test_pid = "11111111-1111-4111-8111-111111111111"
    token = _current_patient_id.set(test_pid)
    try:
        assert _get_patient_id(required=True) == test_pid
        assert _get_patient_id(required=False) == test_pid
    finally:
        _current_patient_id.reset(token)
