"""#460 — vaccination-narrow cloning v1.

Covers the MCP tool contract: dry_run reports without touching DB/GDrive,
live run creates document_references rows keyed by (source_document_id,
event_date, event_label), repeat calls are idempotent via the UNIQUE
constraint, and non-vaccination docs are left alone.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pytest

from oncofiles.models import Document, DocumentCategory
from oncofiles.tools.enhance_tools import detect_and_clone_vaccinations

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"


class _StubCtx:
    session_id = "stub"


async def _insert_vaccine_doc(db, *, filename, doc_id_hint="v"):
    doc = Document(
        file_id=f"gdrive_{doc_id_hint}",
        filename=filename,
        original_filename=filename,
        document_date=date(2026, 4, 1),
        institution="ProCare",
        category=DocumentCategory.VACCINATION,
        description="lifetime log",
        mime_type="application/pdf",
        size_bytes=500,
        gdrive_id=f"gdrive_{doc_id_hint}",
    )
    inserted = await db.insert_document(doc, patient_id=ERIKA_UUID)
    # The tool reads OCR text — seed some pages so .has_ocr_text() is true.
    await db.db.execute(
        "INSERT INTO document_pages (document_id, page_number, extracted_text) VALUES (?, 1, ?)",
        (inserted.id, "Hepatitis B 2010-06-01; MMR 2015-04-12; Booster 2025-03-15"),
    )
    await db.db.commit()
    return inserted


async def _call_tool(db, *, dry_run, vaccine_events):
    """Invoke the tool with helpers + AI call stubbed out."""
    from oncofiles.tools import _helpers as helpers
    from oncofiles.tools import enhance_tools as mod

    orig_get_db = mod._get_db
    orig_resolve = mod._resolve_patient_id
    orig_gdrive = mod._get_gdrive

    async def fake_resolve(_slug, _ctx):
        return ERIKA_UUID

    async def fake_gdrive(_ctx, **_kwargs):
        return None  # dry_run code path skips gdrive; live path falls through

    mod._get_db = lambda _ctx: db
    mod._resolve_patient_id = fake_resolve
    mod._get_gdrive = fake_gdrive
    helpers._get_db = mod._get_db
    helpers._resolve_patient_id = mod._resolve_patient_id
    helpers._get_gdrive = mod._get_gdrive
    try:
        with patch(
            "oncofiles.doc_analysis.analyze_vaccination_events",
            return_value=vaccine_events,
        ):
            raw = await detect_and_clone_vaccinations(_StubCtx(), dry_run=dry_run)
    finally:
        mod._get_db = orig_get_db
        mod._resolve_patient_id = orig_resolve
        mod._get_gdrive = orig_gdrive
        helpers._get_db = orig_get_db
        helpers._resolve_patient_id = orig_resolve
        helpers._get_gdrive = orig_gdrive
    return json.loads(raw)


EXAMPLE_EVENTS = [
    {
        "date": "2010-06-01",
        "vaccine_name": "HepatitisB",
        "dose_label": "primary",
        "reasoning": "initial vaccination entry",
    },
    {
        "date": "2015-04-12",
        "vaccine_name": "MMR",
        "dose_label": "",
        "reasoning": "MMR vaccine",
    },
    {
        "date": "2025-03-15",
        "vaccine_name": "HepatitisB",
        "dose_label": "booster",
        "reasoning": "booster shot — same vaccine different dose label",
    },
]


@pytest.mark.asyncio
async def test_dry_run_reports_events_without_writing(db):
    doc = await _insert_vaccine_doc(db, filename="20260401_log.pdf")

    result = await _call_tool(db, dry_run=True, vaccine_events=EXAMPLE_EVENTS)

    assert result["dry_run"] is True
    assert result["scanned"] == 1
    assert result["events_found"] == 3
    assert result["references_created"] == 0

    async with db.db.execute(
        "SELECT COUNT(*) AS c FROM document_references WHERE source_document_id = ?",
        (doc.id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert dict(row)["c"] == 0


@pytest.mark.asyncio
async def test_live_run_creates_unique_references(db):
    doc = await _insert_vaccine_doc(db, filename="20260401_log.pdf")

    result = await _call_tool(db, dry_run=False, vaccine_events=EXAMPLE_EVENTS)

    assert result["dry_run"] is False
    assert result["references_created"] == 3

    async with db.db.execute(
        "SELECT event_date, event_label FROM document_references "
        "WHERE source_document_id = ? ORDER BY event_date",
        (doc.id,),
    ) as cursor:
        rows = [dict(r) for r in await cursor.fetchall()]
    assert rows == [
        {"event_date": "2010-06-01", "event_label": "HepatitisB:primary"},
        {"event_date": "2015-04-12", "event_label": "MMR"},
        {"event_date": "2025-03-15", "event_label": "HepatitisB:booster"},
    ]


@pytest.mark.asyncio
async def test_repeat_call_is_idempotent(db):
    doc = await _insert_vaccine_doc(db, filename="20260401_log.pdf")

    first = await _call_tool(db, dry_run=False, vaccine_events=EXAMPLE_EVENTS)
    assert first["references_created"] == 3

    second = await _call_tool(db, dry_run=False, vaccine_events=EXAMPLE_EVENTS)
    assert second["references_created"] == 0
    assert second["skipped_existing"] == 3

    async with db.db.execute(
        "SELECT COUNT(*) AS c FROM document_references WHERE source_document_id = ?",
        (doc.id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert dict(row)["c"] == 3


@pytest.mark.asyncio
async def test_skips_non_vaccination_docs(db):
    # Insert a lab (not vaccination) — should be ignored.
    lab = Document(
        file_id="lab_a",
        filename="lab.pdf",
        original_filename="lab.pdf",
        document_date=date(2026, 2, 1),
        institution="NOU",
        category=DocumentCategory.LABS,
        description="x",
        mime_type="application/pdf",
        size_bytes=100,
    )
    await db.insert_document(lab, patient_id=ERIKA_UUID)

    result = await _call_tool(db, dry_run=True, vaccine_events=EXAMPLE_EVENTS)
    assert result["scanned"] == 0
    assert result["events_found"] == 0
    assert "No vaccination-category documents" in result["message"]


@pytest.mark.asyncio
async def test_empty_ai_response_creates_nothing(db):
    await _insert_vaccine_doc(db, filename="20260401_log.pdf")

    result = await _call_tool(db, dry_run=False, vaccine_events=[])
    assert result["scanned"] == 1
    assert result["events_found"] == 0
    assert result["references_created"] == 0


@pytest.mark.asyncio
async def test_malformed_date_is_dropped(db):
    """Events whose date doesn't parse as YYYY-MM are silently skipped."""
    await _insert_vaccine_doc(db, filename="20260401_log.pdf")
    events = [
        {
            "date": "sometime in 2010",  # malformed
            "vaccine_name": "HepB",
            "dose_label": "primary",
        },
        {
            "date": "2020-07-01",
            "vaccine_name": "Tdap",
            "dose_label": "",
        },
    ]
    result = await _call_tool(db, dry_run=False, vaccine_events=events)
    assert result["events_found"] == 1
    assert result["references_created"] == 1
