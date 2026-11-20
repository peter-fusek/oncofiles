"""Tests for filename naming helpers — specifically patient-name swap logic.

Covers the carve-out for patients whose hospital chart is filed under a
different surname than their display name (e.g. pediatric / transplant cases
where records use the mother's surname). See #439.
"""

from __future__ import annotations

import pytest

from oncofiles import patient_context
from oncofiles.tools.naming import _try_patient_name_swap

PID_CHART = "00000000-0000-4000-8000-0000000000c1"
PID_JANA = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def chart_ctx():
    """Set up patient_context for Tomas — display name Vzorovy, chart Testovská."""
    original = dict(patient_context._contexts)
    patient_context._contexts[PID_CHART] = {
        "name": "Tomas Vzorovy",
        "medical_record_name": "Testovská",
    }
    yield
    patient_context._contexts.clear()
    patient_context._contexts.update(original)


@pytest.fixture
def jana_ctx():
    """Set up patient_context for Jana — no medical_record_name carve-out."""
    original = dict(patient_context._contexts)
    patient_context._contexts[PID_JANA] = {"name": "Jana Testova"}
    yield
    patient_context._contexts.clear()
    patient_context._contexts.update(original)


class TestPatientNameSwap:
    def test_leaves_matching_display_name_alone(self, jana_ctx):
        # Standard format with correct patient name — don't touch it
        result = _try_patient_name_swap(
            "20290310_JanaTestova_Oncopol_Labs_PreC3.pdf",
            PID_JANA,
        )
        assert result is None

    def test_swaps_wrong_name_to_display(self, jana_ctx):
        # Standard format with another patient's name — swap to display
        result = _try_patient_name_swap(
            "20290310_SomeoneElse_Oncopol_Labs_PreC3.pdf",
            PID_JANA,
        )
        assert result == "20290310_JanaTestova_Oncopol_Labs_PreC3.pdf"

    def test_leaves_medical_record_name_alone(self, chart_ctx):
        # Tomas's hospital chart is under 'Testovská' — this is a VALID name,
        # do NOT swap to 'TomasVzorovy'. This is the #439 carve-out.
        result = _try_patient_name_swap(
            "20290310_Testovská_OUSA_Labs_FollowUp.pdf",
            PID_CHART,
        )
        assert result is None, "Chart-name match must be respected, not rewritten"

    def test_leaves_matching_display_name_alone_for_tomas(self, chart_ctx):
        # Display-name match also valid for Tomas
        result = _try_patient_name_swap(
            "20290310_TomasVzorovy_OUSA_Labs_FollowUp.pdf",
            PID_CHART,
        )
        assert result is None

    def test_swaps_unrelated_name_for_tomas(self, chart_ctx):
        # Some OTHER patient's name leaks in — swap to display (not chart)
        result = _try_patient_name_swap(
            "20290310_EvaPokusna_OUSA_Labs_FollowUp.pdf",
            PID_CHART,
        )
        assert result == "20290310_TomasVzorovy_OUSA_Labs_FollowUp.pdf"

    def test_chart_name_match_is_case_insensitive(self, chart_ctx):
        # Diacritics/case shouldn't break the match
        result = _try_patient_name_swap(
            "20290310_testovská_OUSA_Labs_FollowUp.pdf",
            PID_CHART,
        )
        assert result is None

    def test_empty_medical_record_name_falls_back_to_display_only(self, jana_ctx):
        # Jana has no medical_record_name — only display_name matches should pass
        result = _try_patient_name_swap(
            "20290310_Testovská_OUSA_Labs_FollowUp.pdf",
            PID_JANA,
        )
        # Should swap to Jana since Testovská isn't her chart name
        assert result == "20290310_JanaTestova_OUSA_Labs_FollowUp.pdf"

    def test_non_standard_format_returns_none(self, chart_ctx):
        # Missing date prefix → not a swap candidate
        result = _try_patient_name_swap("just_some_file.pdf", PID_CHART)
        assert result is None

    def test_invalid_category_token_returns_none(self, jana_ctx):
        # Third position must be a valid category — otherwise this isn't
        # recognizable standard format
        result = _try_patient_name_swap(
            "20290310_SomeoneElse_Oncopol_NotACategory_PreC3.pdf",
            PID_JANA,
        )
        assert result is None
