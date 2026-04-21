"""Consolidation guardrail tests — reject cross-date / cross-institution /
low-confidence groups before they corrupt the DB. See #456 / #428 for the
incident that drove the rules.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from oncofiles.consolidate import (
    CONSOLIDATE_MAX_DATE_SPAN_DAYS,
    CONSOLIDATE_MIN_CONFIDENCE,
    _dates_within_span,
    _institutions_compatible,
    consolidate_documents,
)
from oncofiles.models import Document, DocumentCategory

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"


# ── pure helpers ────────────────────────────────────────────────────────────


def test_dates_within_span_single_date_passes():
    assert _dates_within_span([date(2026, 2, 1)], max_span_days=7) is True


def test_dates_within_span_all_none_passes():
    assert _dates_within_span([None, None], max_span_days=7) is True


def test_dates_within_span_exact_boundary_passes():
    # 7-day span should pass at exactly 7 days.
    dates = [date(2026, 2, 1), date(2026, 2, 8)]
    assert _dates_within_span(dates, max_span_days=7) is True


def test_dates_within_span_over_boundary_fails():
    dates = [date(2026, 2, 1), date(2026, 2, 9)]
    assert _dates_within_span(dates, max_span_days=7) is False


def test_dates_within_span_mixed_none_passes():
    # A missing date can't be proven too-far from anything else, so skip it
    # rather than fail. Real parts often have one unparsed date.
    dates = [date(2026, 2, 1), None, date(2026, 2, 3)]
    assert _dates_within_span(dates, max_span_days=7) is True


def test_institutions_compatible_all_none_passes():
    assert _institutions_compatible([None, None]) is True


def test_institutions_compatible_all_same_passes():
    assert _institutions_compatible(["NOU", "NOU"]) is True


def test_institutions_compatible_one_none_one_known_passes():
    # Common when only one part has institution inferred — don't reject that.
    assert _institutions_compatible(["NOU", None]) is True


def test_institutions_compatible_two_different_fails():
    # #428: Bory pathology + NOU genetics should NEVER be consolidated.
    assert _institutions_compatible(["NOU", "BoryNemocnica"]) is False


def test_institutions_compatible_whitespace_ignored():
    assert _institutions_compatible(["NOU", "  NOU  "]) is True


def test_institutions_compatible_empty_strings_ignored():
    assert _institutions_compatible(["", "NOU"]) is True


# ── consolidate_documents integration ───────────────────────────────────────


async def _insert_doc(db, *, institution, doc_date, file_id):
    doc = Document(
        file_id=file_id,
        filename=f"{file_id}.pdf",
        original_filename=f"{file_id}.pdf",
        document_date=doc_date,
        institution=institution,
        category=DocumentCategory.LABS,
        description="x",
        mime_type="application/pdf",
        size_bytes=100,
    )
    return await db.insert_document(doc, patient_id=ERIKA_UUID)


@pytest.mark.asyncio
async def test_consolidate_rejects_low_confidence(db):
    d1 = await _insert_doc(db, institution="NOU", doc_date=date(2026, 2, 1), file_id="conf_a")
    d2 = await _insert_doc(db, institution="NOU", doc_date=date(2026, 2, 1), file_id="conf_b")

    result = await consolidate_documents(
        db,
        None,
        {
            "document_ids": [d1.id, d2.id],
            "confidence": CONSOLIDATE_MIN_CONFIDENCE - 0.1,
            "reasoning": "speculative",
        },
        patient_id=ERIKA_UUID,
    )

    assert result is None
    refreshed = [await db.get_document(d1.id), await db.get_document(d2.id)]
    assert all(d.group_id is None for d in refreshed)


@pytest.mark.asyncio
async def test_consolidate_rejects_date_span_too_large(db):
    d1 = await _insert_doc(db, institution="NOU", doc_date=date(2026, 2, 1), file_id="span_a")
    d2 = await _insert_doc(
        db,
        institution="NOU",
        doc_date=date(2026, 2, 1) + timedelta(days=CONSOLIDATE_MAX_DATE_SPAN_DAYS * 2),
        file_id="span_b",
    )

    result = await consolidate_documents(
        db,
        None,
        {"document_ids": [d1.id, d2.id], "confidence": 0.95, "reasoning": "x"},
        patient_id=ERIKA_UUID,
    )

    assert result is None
    assert (await db.get_document(d1.id)).group_id is None


@pytest.mark.asyncio
async def test_consolidate_rejects_cross_institution(db):
    d1 = await _insert_doc(db, institution="NOU", doc_date=date(2026, 2, 1), file_id="inst_a")
    d2 = await _insert_doc(
        db, institution="BoryNemocnica", doc_date=date(2026, 2, 1), file_id="inst_b"
    )

    result = await consolidate_documents(
        db,
        None,
        {"document_ids": [d1.id, d2.id], "confidence": 0.95, "reasoning": "x"},
        patient_id=ERIKA_UUID,
    )

    assert result is None
    assert (await db.get_document(d1.id)).group_id is None


@pytest.mark.asyncio
async def test_consolidate_accepts_valid_group(db):
    d1 = await _insert_doc(db, institution="NOU", doc_date=date(2026, 2, 1), file_id="ok_a")
    d2 = await _insert_doc(db, institution="NOU", doc_date=date(2026, 2, 3), file_id="ok_b")

    result = await consolidate_documents(
        db,
        None,
        {"document_ids": [d1.id, d2.id], "confidence": 0.9, "reasoning": "split scan"},
        patient_id=ERIKA_UUID,
    )

    assert result is not None
    r1 = await db.get_document(d1.id)
    r2 = await db.get_document(d2.id)
    assert r1.group_id == result
    assert r2.group_id == result
    assert {r1.part_number, r2.part_number} == {1, 2}
    assert r1.total_parts == 2 and r2.total_parts == 2
