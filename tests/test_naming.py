"""Tests for filename naming helpers — specifically patient-name swap logic.

Covers the carve-out for patients whose hospital chart is filed under a
different surname than their display name (e.g. pediatric / transplant cases
where records use the mother's surname). See #439.
"""

from __future__ import annotations

import pytest

from oncofiles import patient_context
from oncofiles.tools.naming import _try_patient_name_swap

PID_MATTIAS = "faa4ca8e-6791-47de-85c1-87ceb3725b3a"
PID_ERIKA = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def mattias_ctx():
    """Set up patient_context for Mattias — display name Cesnak, chart Gonsorčíková."""
    original = dict(patient_context._contexts)
    patient_context._contexts[PID_MATTIAS] = {
        "name": "Mattias Cesnak",
        "medical_record_name": "Gonsorčíková",
    }
    yield
    patient_context._contexts.clear()
    patient_context._contexts.update(original)


@pytest.fixture
def erika_ctx():
    """Set up patient_context for Erika — no medical_record_name carve-out."""
    original = dict(patient_context._contexts)
    patient_context._contexts[PID_ERIKA] = {"name": "Erika Fusekova"}
    yield
    patient_context._contexts.clear()
    patient_context._contexts.update(original)


class TestPatientNameSwap:
    def test_leaves_matching_display_name_alone(self, erika_ctx):
        # Standard format with correct patient name — don't touch it
        result = _try_patient_name_swap(
            "20250310_ErikaFusekova_Oncopol_Labs_PreC3.pdf",
            PID_ERIKA,
        )
        assert result is None

    def test_swaps_wrong_name_to_display(self, erika_ctx):
        # Standard format with another patient's name — swap to display
        result = _try_patient_name_swap(
            "20250310_SomeoneElse_Oncopol_Labs_PreC3.pdf",
            PID_ERIKA,
        )
        assert result == "20250310_ErikaFusekova_Oncopol_Labs_PreC3.pdf"

    def test_leaves_medical_record_name_alone(self, mattias_ctx):
        # Mattias's hospital chart is under 'Gonsorčíková' — this is a VALID name,
        # do NOT swap to 'MattiasCesnak'. This is the #439 carve-out.
        result = _try_patient_name_swap(
            "20250310_Gonsorčíková_OUSA_Labs_FollowUp.pdf",
            PID_MATTIAS,
        )
        assert result is None, "Chart-name match must be respected, not rewritten"

    def test_leaves_matching_display_name_alone_for_mattias(self, mattias_ctx):
        # Display-name match also valid for Mattias
        result = _try_patient_name_swap(
            "20250310_MattiasCesnak_OUSA_Labs_FollowUp.pdf",
            PID_MATTIAS,
        )
        assert result is None

    def test_swaps_unrelated_name_for_mattias(self, mattias_ctx):
        # Some OTHER patient's name leaks in — swap to display (not chart)
        result = _try_patient_name_swap(
            "20250310_NoraAntalova_OUSA_Labs_FollowUp.pdf",
            PID_MATTIAS,
        )
        assert result == "20250310_MattiasCesnak_OUSA_Labs_FollowUp.pdf"

    def test_chart_name_match_is_case_insensitive(self, mattias_ctx):
        # Diacritics/case shouldn't break the match
        result = _try_patient_name_swap(
            "20250310_gonsorčíková_OUSA_Labs_FollowUp.pdf",
            PID_MATTIAS,
        )
        assert result is None

    def test_empty_medical_record_name_falls_back_to_display_only(self, erika_ctx):
        # Erika has no medical_record_name — only display_name matches should pass
        result = _try_patient_name_swap(
            "20250310_Gonsorčíková_OUSA_Labs_FollowUp.pdf",
            PID_ERIKA,
        )
        # Should swap to Erika since Gonsorčíková isn't her chart name
        assert result == "20250310_ErikaFusekova_OUSA_Labs_FollowUp.pdf"

    def test_non_standard_format_returns_none(self, mattias_ctx):
        # Missing date prefix → not a swap candidate
        result = _try_patient_name_swap("just_some_file.pdf", PID_MATTIAS)
        assert result is None

    def test_invalid_category_token_returns_none(self, erika_ctx):
        # Third position must be a valid category — otherwise this isn't
        # recognizable standard format
        result = _try_patient_name_swap(
            "20250310_SomeoneElse_Oncopol_NotACategory_PreC3.pdf",
            PID_ERIKA,
        )
        assert result is None
