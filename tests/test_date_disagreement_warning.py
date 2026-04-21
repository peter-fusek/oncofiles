"""#459: _check_filename_date_agreement warns when AI-extracted date
diverges from filename-encoded date by more than 30 days."""

from __future__ import annotations

import logging

from oncofiles.enhance import _check_filename_date_agreement


def test_no_warning_when_dates_match(caplog):
    with caplog.at_level(logging.WARNING, logger="oncofiles.enhance"):
        _check_filename_date_agreement(
            "20260213_ErikaFusekova_NOU_Labs_a.pdf", "2026-02-13", document_id=1
        )
    assert not any("date disagree" in r.message for r in caplog.records)


def test_no_warning_when_within_30d(caplog):
    with caplog.at_level(logging.WARNING, logger="oncofiles.enhance"):
        _check_filename_date_agreement(
            "20260213_ErikaFusekova_NOU_Labs_a.pdf", "2026-02-25", document_id=2
        )
    # 12d diff — under threshold, no warning.
    assert not any("date disagree" in r.message for r in caplog.records)


def test_warns_when_divergence_exceeds_30d(caplog):
    with caplog.at_level(logging.WARNING, logger="oncofiles.enhance"):
        # AI picked DOB-style date far away from filename date — classic #459.
        _check_filename_date_agreement(
            "20260213_ErikaFusekova_NOU_Labs_a.pdf", "1972-04-14", document_id=3
        )
    warnings = [r for r in caplog.records if "date disagree" in r.message]
    assert len(warnings) == 1
    msg = warnings[0].message
    assert "doc=3" in msg
    assert "1972-04-14" in msg
    assert "2026-02-13" in msg


def test_no_warning_when_filename_has_no_date(caplog):
    with caplog.at_level(logging.WARNING, logger="oncofiles.enhance"):
        _check_filename_date_agreement("scan_random.pdf", "2026-02-13", document_id=4)
    assert not any("date disagree" in r.message for r in caplog.records)


def test_no_warning_when_ai_date_missing(caplog):
    with caplog.at_level(logging.WARNING, logger="oncofiles.enhance"):
        _check_filename_date_agreement("20260213_ErikaFusekova_NOU_Labs_a.pdf", None, document_id=5)
    assert not any("date disagree" in r.message for r in caplog.records)


def test_dashed_date_filename_parses(caplog):
    """Mattias-style dashed YYYY-MM-DD filenames also feed the cross-check."""
    with caplog.at_level(logging.WARNING, logger="oncofiles.enhance"):
        _check_filename_date_agreement(
            "2025-09-19_kontrola_Gonsorcikova.pdf", "2019-01-01", document_id=6
        )
    warnings = [r for r in caplog.records if "date disagree" in r.message]
    assert len(warnings) == 1
    assert "2025-09-19" in warnings[0].message
