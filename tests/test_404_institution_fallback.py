"""Lock-in for #404: break the Unknown-institution rename loop +
patient-context fallback for chemo_sheet et al.

Pre-#404: a chemo sheet with no providers in the OCR text and an
``Unknown`` placeholder in the original filename would cycle forever:
  rename → "Unknown_..." → is_standard_format() rejects → next sweep
  rebuilds the same "Unknown_..." → ad infinitum.

Option A (rename loop break): when the institution can't be resolved,
return the input filename unchanged so the doc stops oscillating in
the dashboard's NÁZOV column.

Option B (patient-context fallback): when ``infer_institution_from_providers``
returns None for a chemo_sheet / surgery / discharge document, consult
the patient_context primary clinic. The fallback is gated to those
categories — labs and prescriptions routinely come from external
sources and must not be misattributed to the primary clinic.
"""

from __future__ import annotations

import json

import pytest

from oncofiles import patient_context
from oncofiles.enhance import (
    _PRIMARY_CLINIC_FALLBACK_CATEGORIES,
    _primary_institution_for_patient,
    backfill_missing_institutions,
)
from oncofiles.filename_parser import rename_to_standard
from oncofiles.models import DocumentCategory
from tests.helpers import ERIKA_UUID, make_doc

# ── Option A: rename_to_standard never writes "Unknown" back ──────────


def test_rename_returns_input_when_institution_unknown_no_override():
    """The infinite-loop fix. Input has ``Unknown`` in the institution slot;
    no override available; rename must return the input UNCHANGED so the
    next sweep doesn't re-write the same Unknown filename."""
    src = "20230326_ErikaFusekova_Unknown_ChemoSheet_ErikaFusekovaUnkn.pdf"
    out = rename_to_standard(src, patient_id=ERIKA_UUID)
    assert out == src, (
        "rename_to_standard must return the input unchanged when institution "
        "is Unknown and no override is provided (#404 Option A)"
    )


def test_rename_writes_real_institution_when_override_provided():
    """Sanity: the loop-break only applies when there's NO override. With a
    real override the rename produces the standard-format filename."""
    src = "20230326_ErikaFusekova_Unknown_ChemoSheet_ErikaFusekovaUnkn.pdf"
    out = rename_to_standard(src, patient_id=ERIKA_UUID, institution_override="NOU")
    assert out != src
    assert "_NOU_" in out
    assert "Unknown" not in out


def test_rename_returns_input_when_institution_field_already_filename_unknown():
    """Even if the parsed filename's institution is the literal 'Unknown'
    token, no override → unchanged. Belt-and-suspenders against the loop."""
    src = "20240101_ErikaFusekova_Unknown_Labs_Description.pdf"
    out = rename_to_standard(src, patient_id=ERIKA_UUID)
    assert out == src


# ── Option B: _primary_institution_for_patient resolution ─────────────


def test_primary_institution_reads_top_level_field():
    """The explicit ``primary_institution`` patient_context field wins."""
    pid = "test-pid-primary"
    patient_context._contexts[pid] = {
        "name": "Test",
        "primary_institution": "NOU",
        "treatment": {"institution_code": "OUSA"},
    }
    try:
        assert _primary_institution_for_patient(pid) == "NOU"
    finally:
        patient_context._contexts.pop(pid, None)


def test_primary_institution_falls_back_to_treatment_subschema():
    """If top-level field is empty/missing, use treatment.institution_code."""
    pid = "test-pid-treatment"
    patient_context._contexts[pid] = {
        "name": "Test",
        "treatment": {"institution_code": "BoryNemocnica"},
    }
    try:
        assert _primary_institution_for_patient(pid) == "BoryNemocnica"
    finally:
        patient_context._contexts.pop(pid, None)


def test_primary_institution_returns_none_when_empty_strings():
    """Empty strings must not be treated as valid codes — return None so
    the caller's still-missing path runs."""
    pid = "test-pid-empty"
    patient_context._contexts[pid] = {
        "name": "Test",
        "primary_institution": "  ",
        "treatment": {"institution_code": ""},
    }
    try:
        assert _primary_institution_for_patient(pid) is None
    finally:
        patient_context._contexts.pop(pid, None)


def test_primary_institution_returns_none_when_no_data():
    """Patient with no clinic info → None, not a default."""
    pid = "test-pid-bare"
    patient_context._contexts[pid] = {"name": "Test"}
    try:
        assert _primary_institution_for_patient(pid) is None
    finally:
        patient_context._contexts.pop(pid, None)


# ── Allowlist contract ────────────────────────────────────────────────


def test_primary_clinic_allowlist_is_locked():
    """Lock the exact set of categories that participate in the fallback.
    Adding labs/imaging/prescription/advocate to this set would silently
    misattribute external-source docs to the primary clinic — review-gate."""
    expected = {"chemo_sheet", "surgery", "surgical_report", "discharge", "discharge_summary"}
    assert expected == _PRIMARY_CLINIC_FALLBACK_CATEGORIES


# ── Integration: backfill_missing_institutions uses fallback ──────────


@pytest.mark.asyncio
async def test_backfill_uses_patient_context_for_chemo_sheet(db):
    """Chemo sheet with no providers → fills institution from patient_context."""
    patient_context._contexts[ERIKA_UUID] = {
        "name": "Erika Fusekova",
        "primary_institution": "NOU",
    }
    try:
        doc = make_doc(filename="20230326_chemo.pdf", institution=None)
        doc.category = DocumentCategory.CHEMO_SHEET
        inserted = await db.insert_document(doc, patient_id=ERIKA_UUID)
        # Set structured_metadata with empty providers — the dropout case.
        await db.update_structured_metadata(inserted.id, json.dumps({"providers": []}))

        stats = await backfill_missing_institutions(db.db)
        assert stats["checked"] >= 1
        assert stats["updated"] >= 1
        assert stats["updated_from_patient_context"] >= 1

        async with db.db.execute(
            "SELECT institution FROM documents WHERE id = ?", (inserted.id,)
        ) as cursor:
            row = await cursor.fetchone()
        assert row["institution"] == "NOU"
    finally:
        patient_context._contexts.pop(ERIKA_UUID, None)


@pytest.mark.asyncio
async def test_backfill_skips_fallback_for_labs_category(db):
    """Labs from external lab → MUST NOT be misattributed to primary clinic."""
    patient_context._contexts[ERIKA_UUID] = {
        "name": "Erika Fusekova",
        "primary_institution": "NOU",
    }
    try:
        doc = make_doc(filename="20230326_labs.pdf", institution=None)
        doc.category = DocumentCategory.LABS
        inserted = await db.insert_document(doc, patient_id=ERIKA_UUID)
        await db.update_structured_metadata(inserted.id, json.dumps({"providers": []}))

        stats = await backfill_missing_institutions(db.db)
        assert stats["still_missing"] >= 1
        assert stats["updated_from_patient_context"] == 0

        async with db.db.execute(
            "SELECT institution FROM documents WHERE id = ?", (inserted.id,)
        ) as cursor:
            row = await cursor.fetchone()
        # Labs stay NULL — better than misattributed to NOU.
        assert row["institution"] in (None, "")
    finally:
        patient_context._contexts.pop(ERIKA_UUID, None)


@pytest.mark.asyncio
async def test_backfill_skips_fallback_when_no_primary_institution(db):
    """Patient with no primary_institution config → no fallback. Doc stays NULL."""
    patient_context._contexts[ERIKA_UUID] = {"name": "Erika Fusekova"}
    try:
        doc = make_doc(filename="20230326_chemo.pdf", institution=None)
        doc.category = DocumentCategory.CHEMO_SHEET
        inserted = await db.insert_document(doc, patient_id=ERIKA_UUID)
        await db.update_structured_metadata(inserted.id, json.dumps({"providers": []}))

        stats = await backfill_missing_institutions(db.db)
        assert stats["still_missing"] >= 1
        assert stats["updated_from_patient_context"] == 0
    finally:
        patient_context._contexts.pop(ERIKA_UUID, None)


@pytest.mark.asyncio
async def test_backfill_provider_path_still_takes_precedence(db):
    """When providers DO infer an institution, use that — not the fallback.
    The fallback is the safety net, not the override."""
    patient_context._contexts[ERIKA_UUID] = {
        "name": "Erika Fusekova",
        "primary_institution": "OUSA",  # different from what providers infer
    }
    try:
        doc = make_doc(filename="20230326_chemo.pdf", institution=None)
        doc.category = DocumentCategory.CHEMO_SHEET
        inserted = await db.insert_document(doc, patient_id=ERIKA_UUID)
        # Providers list a NOU doctor — should resolve to NOU, NOT the
        # patient_context's primary_institution value.
        await db.update_structured_metadata(
            inserted.id, json.dumps({"providers": ["MUDr. Stefan Porsok, PhD., NOU Klenova"]})
        )

        stats = await backfill_missing_institutions(db.db)
        assert stats["updated"] >= 1
        # NOT counted as a fallback — the provider path won.
        assert stats["updated_from_patient_context"] == 0

        async with db.db.execute(
            "SELECT institution FROM documents WHERE id = ?", (inserted.id,)
        ) as cursor:
            row = await cursor.fetchone()
        assert row["institution"] == "NOU"
    finally:
        patient_context._contexts.pop(ERIKA_UUID, None)
