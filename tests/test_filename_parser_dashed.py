"""Tests for dashed-date + Slovak keyword parsing in filename_parser.

Covers the Mattias Cesnak case where hospital chart uses "2025-09-19_kontrola
Gonsorcikova.pdf" style filenames (YYYY-MM-DD + Slovak medical keyword +
space-separated surname). See #439 and the session #438/#443 dashboard gaps.
"""

from __future__ import annotations

from datetime import date

import pytest

from oncofiles import patient_context
from oncofiles.filename_parser import parse_filename
from oncofiles.models import DocumentCategory

PID_MATTIAS = "faa4ca8e-6791-47de-85c1-87ceb3725b3a"


@pytest.fixture
def mattias_ctx():
    original = dict(patient_context._contexts)
    patient_context._contexts[PID_MATTIAS] = {
        "name": "Mattias Cesnak",
        "medical_record_name": "Gonsorčíková",
    }
    yield
    patient_context._contexts.clear()
    patient_context._contexts.update(original)


class TestDashedDateParsing:
    def test_kontrola_consultation_dashed_date(self, mattias_ctx):
        p = parse_filename("2025-09-19_kontrola Gonsorcikova.pdf", patient_id=PID_MATTIAS)
        assert p.document_date == date(2025, 9, 19)
        assert p.category == DocumentCategory.CONSULTATION

    def test_kontrola_follow_up_date_2026(self, mattias_ctx):
        p = parse_filename("2026-03-29_kontrola Gonsorcikova.pdf", patient_id=PID_MATTIAS)
        assert p.document_date == date(2026, 3, 29)
        assert p.category == DocumentCategory.CONSULTATION

    def test_single_digit_day(self, mattias_ctx):
        # "2024-11-8" — single-digit day, must still parse
        p = parse_filename(
            "2024-11-8-matti_zdravotna_sprava_Gonsorcikova.pdf", patient_id=PID_MATTIAS
        )
        assert p.document_date == date(2024, 11, 8)
        # "zdravotna sprava" = medical report
        assert p.category == DocumentCategory.REPORT

    def test_single_digit_month_and_day(self, mattias_ctx):
        p = parse_filename("2024-1-5_kontrola Gonsorcikova.pdf", patient_id=PID_MATTIAS)
        assert p.document_date == date(2024, 1, 5)
        assert p.category == DocumentCategory.CONSULTATION

    def test_labak_lab_keyword(self, mattias_ctx):
        # "2025-06-20_kontrola Gonsorcikova labak.pdf" — from Mattias's actual files
        # "kontrola" wins because it appears first in _CATEGORY_KEYWORDS
        p = parse_filename("2025-06-20_kontrola Gonsorcikova labak.pdf", patient_id=PID_MATTIAS)
        assert p.document_date == date(2025, 6, 20)
        assert p.category == DocumentCategory.CONSULTATION


class TestStandardFormatStillWorks:
    """Regression: 8-digit YYYYMMDD must keep working as before."""

    def test_standard_format_unchanged(self, mattias_ctx):
        p = parse_filename("20260310_MattiasCesnak_NOU_Labs_PreC3.pdf", patient_id=PID_MATTIAS)
        assert p.document_date == date(2026, 3, 10)
        assert p.category == DocumentCategory.LABS
        assert p.institution == "NOU"

    def test_xx_format_unchanged(self, mattias_ctx):
        # YYYYMM + xx for unknown day — still handled
        p = parse_filename("202602xx_some_file.pdf", patient_id=PID_MATTIAS)
        assert p.document_date == date(2026, 2, 1)


class TestInvalidDashedDates:
    def test_malformed_date_falls_through(self, mattias_ctx):
        # Invalid dates — regex shouldn't match partial garbage
        p = parse_filename("not_a_date_at_all.pdf", patient_id=PID_MATTIAS)
        assert p.document_date is None

    def test_impossible_date_suppressed(self, mattias_ctx):
        # Month 13 — regex matches shape but date() constructor raises; parser
        # swallows ValueError and leaves document_date as None.
        p = parse_filename("2025-13-45_weird.pdf", patient_id=PID_MATTIAS)
        assert p.document_date is None


class TestSpaceSeparatedTokens:
    """Split-on-whitespace ensures Slovak-style space-separated filenames classify."""

    def test_space_separator_in_description(self, mattias_ctx):
        p = parse_filename("20260310_ambulancia kontrola.pdf", patient_id=PID_MATTIAS)
        assert p.document_date == date(2026, 3, 10)
        # "ambulancia" → ambulan — also consultation per _CATEGORY_KEYWORDS
        assert p.category == DocumentCategory.CONSULTATION
