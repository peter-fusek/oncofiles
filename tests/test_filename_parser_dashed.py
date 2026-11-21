"""Tests for dashed-date + Slovak keyword parsing in filename_parser.

Covers the case where a hospital chart uses "2028-09-19_kontrola
Testovska.pdf" style filenames (YYYY-MM-DD + Slovak medical keyword +
space-separated surname). See #439 and the session #438/#443 dashboard gaps.
"""

from __future__ import annotations

from datetime import date

import pytest

from oncofiles import patient_context
from oncofiles.filename_parser import parse_filename
from oncofiles.models import DocumentCategory

PID_CHART = "00000000-0000-4000-8000-0000000000c1"


@pytest.fixture
def chart_ctx():
    original = dict(patient_context._contexts)
    patient_context._contexts[PID_CHART] = {
        "name": "Tomas Vzorovy",
        "medical_record_name": "Testovská",
    }
    yield
    patient_context._contexts.clear()
    patient_context._contexts.update(original)


class TestDashedDateParsing:
    def test_kontrola_consultation_dashed_date(self, chart_ctx):
        p = parse_filename("2028-09-19_kontrola Testovska.pdf", patient_id=PID_CHART)
        assert p.document_date == date(2028, 9, 19)
        assert p.category == DocumentCategory.CONSULTATION

    def test_kontrola_follow_up_date_2029(self, chart_ctx):
        p = parse_filename("2029-03-29_kontrola Testovska.pdf", patient_id=PID_CHART)
        assert p.document_date == date(2029, 3, 29)
        assert p.category == DocumentCategory.CONSULTATION

    def test_single_digit_day(self, chart_ctx):
        # "2027-11-8" — single-digit day, must still parse
        p = parse_filename("2027-11-8-tomi_zdravotna_sprava_Testovska.pdf", patient_id=PID_CHART)
        assert p.document_date == date(2027, 11, 8)
        # "zdravotna sprava" = medical report
        assert p.category == DocumentCategory.REPORT

    def test_single_digit_month_and_day(self, chart_ctx):
        p = parse_filename("2027-1-5_kontrola Testovska.pdf", patient_id=PID_CHART)
        assert p.document_date == date(2027, 1, 5)
        assert p.category == DocumentCategory.CONSULTATION

    def test_labak_lab_keyword(self, chart_ctx):
        # "2028-06-20_kontrola Testovska labak.pdf" — the shape of a real chart filename
        # "kontrola" wins because it appears first in _CATEGORY_KEYWORDS
        p = parse_filename("2028-06-20_kontrola Testovska labak.pdf", patient_id=PID_CHART)
        assert p.document_date == date(2028, 6, 20)
        assert p.category == DocumentCategory.CONSULTATION


class TestStandardFormatStillWorks:
    """Regression: 8-digit YYYYMMDD must keep working as before."""

    def test_standard_format_unchanged(self, chart_ctx):
        p = parse_filename("20290310_TomasVzorovy_NOU_Labs_PreC3.pdf", patient_id=PID_CHART)
        assert p.document_date == date(2029, 3, 10)
        assert p.category == DocumentCategory.LABS
        assert p.institution == "NOU"

    def test_xx_format_unchanged(self, chart_ctx):
        # YYYYMM + xx for unknown day — still handled
        p = parse_filename("202902xx_some_file.pdf", patient_id=PID_CHART)
        assert p.document_date == date(2029, 2, 1)


class TestInvalidDashedDates:
    def test_malformed_date_falls_through(self, chart_ctx):
        # Invalid dates — regex shouldn't match partial garbage
        p = parse_filename("not_a_date_at_all.pdf", patient_id=PID_CHART)
        assert p.document_date is None

    def test_impossible_date_suppressed(self, chart_ctx):
        # Month 13 — regex matches shape but date() constructor raises; parser
        # swallows ValueError and leaves document_date as None.
        p = parse_filename("2028-13-45_weird.pdf", patient_id=PID_CHART)
        assert p.document_date is None


class TestSpaceSeparatedTokens:
    """Split-on-whitespace ensures Slovak-style space-separated filenames classify."""

    def test_space_separator_in_description(self, chart_ctx):
        p = parse_filename("20290310_ambulancia kontrola.pdf", patient_id=PID_CHART)
        assert p.document_date == date(2029, 3, 10)
        # "ambulancia" → ambulan — also consultation per _CATEGORY_KEYWORDS
        assert p.category == DocumentCategory.CONSULTATION
