"""Tests for the repair_broken_groups MCP tool.

Rationale: existing production data (Erika/q1b) contains consolidation groups
that pre-date the guardrails in `consolidate_documents` — AI grouped unrelated
documents across institutions or across months. The repair tool is a one-shot
cleanup that un-groups any group whose members now fail the same guardrails
that block new bad groups.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from oncofiles.models import Document, DocumentCategory

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"


async def _insert_grouped(db, *, institution, doc_date, group_id, part, total, filename):
    doc = Document(
        file_id=f"{filename}",
        filename=filename,
        original_filename=filename,
        document_date=doc_date,
        institution=institution,
        category=DocumentCategory.LABS,
        description="x",
        mime_type="application/pdf",
        size_bytes=100,
        group_id=group_id,
        part_number=part,
        total_parts=total,
    )
    return await db.insert_document(doc, patient_id=ERIKA_UUID)


async def _invoke_repair(db, dry_run):
    """Call repair_broken_groups with a hand-rolled Context — the tool only
    uses ctx to fetch db + patient slug, both of which we stub directly."""
    from oncofiles.tools.enhance_tools import repair_broken_groups

    class _StubCtx:
        pass

    ctx = _StubCtx()

    # The tool calls _get_db(ctx) and _resolve_patient_id(slug, ctx). We patch
    # both by importing the helpers and monkey-patching at call site via
    # module-level replacements to avoid FastMCP's full context harness.
    from oncofiles.tools import _helpers as helpers

    original_get_db = helpers._get_db
    original_resolve = helpers._resolve_patient_id

    async def fake_resolve(_slug, _ctx):
        return ERIKA_UUID

    helpers._get_db = lambda _ctx: db
    helpers._resolve_patient_id = fake_resolve
    try:
        # The tool module imports the helpers at function def time, so the
        # monkey-patch needs to apply to the name resolved inside
        # enhance_tools as well.
        from oncofiles.tools import enhance_tools

        enhance_tools._get_db = helpers._get_db
        enhance_tools._resolve_patient_id = helpers._resolve_patient_id

        result = await repair_broken_groups(ctx, dry_run=dry_run)
    finally:
        helpers._get_db = original_get_db
        helpers._resolve_patient_id = original_resolve
        from oncofiles.tools import enhance_tools as et

        et._get_db = original_get_db
        et._resolve_patient_id = original_resolve
    return json.loads(result)


@pytest.mark.asyncio
async def test_repair_dry_run_flags_cross_institution_group(db):
    group_id = "broken-inst"
    await _insert_grouped(
        db,
        institution="NOU",
        doc_date=date(2026, 2, 1),
        group_id=group_id,
        part=1,
        total=2,
        filename="a_Part1of2.pdf",
    )
    await _insert_grouped(
        db,
        institution="BoryNemocnica",
        doc_date=date(2026, 2, 1),
        group_id=group_id,
        part=2,
        total=2,
        filename="b_Part2of2.pdf",
    )

    result = await _invoke_repair(db, dry_run=True)

    assert result["dry_run"] is True
    assert len(result["broken_groups"]) == 1
    assert result["broken_groups"][0]["reason"] == "institutions_differ"
    assert result["reset_document_ids"] == []


@pytest.mark.asyncio
async def test_repair_resets_cross_date_group_and_strips_part_suffix(db):
    group_id = "broken-dates"
    d1 = await _insert_grouped(
        db,
        institution="NOU",
        doc_date=date(2026, 2, 1),
        group_id=group_id,
        part=1,
        total=2,
        filename="20260201_a_Part1of2.pdf",
    )
    d2 = await _insert_grouped(
        db,
        institution="NOU",
        doc_date=date(2026, 3, 1),  # 28 days later — busts 7-day guardrail
        group_id=group_id,
        part=2,
        total=2,
        filename="20260301_b_Part2of2.pdf",
    )

    result = await _invoke_repair(db, dry_run=False)

    assert result["dry_run"] is False
    assert len(result["broken_groups"]) == 1
    assert result["broken_groups"][0]["reason"] == "date_span_too_large"
    assert sorted(result["reset_document_ids"]) == sorted([d1.id, d2.id])

    r1 = await db.get_document(d1.id, patient_id=ERIKA_UUID)
    r2 = await db.get_document(d2.id, patient_id=ERIKA_UUID)
    assert r1.group_id is None and r2.group_id is None
    assert r1.part_number is None and r2.part_number is None
    assert r1.total_parts is None and r2.total_parts is None
    # Part suffix stripped cleanly, extension preserved.
    assert r1.filename == "20260201_a.pdf"
    assert r2.filename == "20260301_b.pdf"


@pytest.mark.asyncio
async def test_repair_leaves_valid_groups_alone(db):
    group_id = "valid-group"
    d1 = await _insert_grouped(
        db,
        institution="NOU",
        doc_date=date(2026, 2, 1),
        group_id=group_id,
        part=1,
        total=2,
        filename="ok_Part1of2.pdf",
    )
    d2 = await _insert_grouped(
        db,
        institution="NOU",
        doc_date=date(2026, 2, 3),
        group_id=group_id,
        part=2,
        total=2,
        filename="ok_Part2of2.pdf",
    )

    result = await _invoke_repair(db, dry_run=False)

    assert result["broken_groups"] == []
    r1 = await db.get_document(d1.id, patient_id=ERIKA_UUID)
    r2 = await db.get_document(d2.id, patient_id=ERIKA_UUID)
    assert r1.group_id == group_id
    assert r2.group_id == group_id


@pytest.mark.asyncio
async def test_repair_flags_single_member_orphans(db):
    """A group with only one live member is by definition broken — a split
    that silently lost its sibling (pre-#456 footgun) or a partial rollback."""
    group_id = "orphan"
    d1 = await _insert_grouped(
        db,
        institution="NOU",
        doc_date=date(2026, 2, 1),
        group_id=group_id,
        part=2,
        total=2,
        filename="only_Part2of2.pdf",
    )

    result = await _invoke_repair(db, dry_run=False)

    assert len(result["broken_groups"]) == 1
    assert result["broken_groups"][0]["reason"] == "single_member"
    refreshed = await db.get_document(d1.id, patient_id=ERIKA_UUID)
    assert refreshed.group_id is None
    assert refreshed.filename == "only.pdf"
