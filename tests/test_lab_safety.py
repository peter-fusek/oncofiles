"""Tests for pre-cycle safety check tools (get_lab_safety_check, get_precycle_checklist)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from oncofiles.database import Database
from oncofiles.tools.lab_trends import get_lab_safety_check, get_precycle_checklist
from tests.helpers import make_doc, make_lab_value


def _mock_ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock(), "gdrive": None}
    return ctx


# ── get_lab_safety_check ─────────────────────────────────────────────────


async def test_lab_safety_check_green(db: Database):
    """Value well above min threshold → green."""
    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    values = [
        make_lab_value(document_id=doc.id, parameter="ABS_NEUT", value=3.0, unit="10^9/L"),
    ]
    await db.insert_lab_values(values)

    result = json.loads(await get_lab_safety_check(ctx))
    neut = next(p for p in result["parameters"] if p["parameter"] == "ABS_NEUT")
    assert neut["status"] == "green"
    assert neut["last_value"] == 3.0
    assert neut["threshold_min"] == 1.5


async def test_lab_safety_check_red_min(db: Database):
    """Value below min threshold (and below 90% borderline) → red."""
    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    # ABS_NEUT min=1.5, 90% of 1.5 = 1.35 → 1.0 is red
    values = [
        make_lab_value(document_id=doc.id, parameter="ABS_NEUT", value=1.0, unit="10^9/L"),
    ]
    await db.insert_lab_values(values)

    result = json.loads(await get_lab_safety_check(ctx))
    neut = next(p for p in result["parameters"] if p["parameter"] == "ABS_NEUT")
    assert neut["status"] == "red"


async def test_lab_safety_check_yellow_min(db: Database):
    """Value in borderline zone (between 90% and 100% of min) → yellow."""
    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    # ABS_NEUT min=1.5, 90% = 1.35 → 1.4 is yellow
    values = [
        make_lab_value(document_id=doc.id, parameter="ABS_NEUT", value=1.4, unit="10^9/L"),
    ]
    await db.insert_lab_values(values)

    result = json.loads(await get_lab_safety_check(ctx))
    neut = next(p for p in result["parameters"] if p["parameter"] == "ABS_NEUT")
    assert neut["status"] == "yellow"


async def test_lab_safety_check_red_max(db: Database):
    """Value above max threshold (and above 110% borderline) → red."""
    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    # BILIRUBIN max=26.0, 110% = 28.6 → 30.0 is red
    values = [
        make_lab_value(document_id=doc.id, parameter="BILIRUBIN", value=30.0, unit="µmol/L"),
    ]
    await db.insert_lab_values(values)

    result = json.loads(await get_lab_safety_check(ctx))
    bili = next(p for p in result["parameters"] if p["parameter"] == "BILIRUBIN")
    assert bili["status"] == "red"
    assert bili["threshold_max"] == 26.0


async def test_lab_safety_check_green_max(db: Database):
    """Value within max threshold → green."""
    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    # BILIRUBIN max=26.0 → 15.0 is green
    values = [
        make_lab_value(document_id=doc.id, parameter="BILIRUBIN", value=15.0, unit="µmol/L"),
    ]
    await db.insert_lab_values(values)

    result = json.loads(await get_lab_safety_check(ctx))
    bili = next(p for p in result["parameters"] if p["parameter"] == "BILIRUBIN")
    assert bili["status"] == "green"


async def test_lab_safety_check_missing(db: Database):
    """No stored value for a parameter → missing status."""
    ctx = _mock_ctx(db)

    result = json.loads(await get_lab_safety_check(ctx))
    # All parameters should be missing (no lab values stored)
    for param in result["parameters"]:
        assert param["status"] == "missing"
        assert param["last_value"] is None


async def test_lab_safety_check_summary(db: Database):
    """Summary counts match individual statuses, cycle_safe logic correct."""
    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    # Store good values for all 9 parameters
    good_values = [
        make_lab_value(document_id=doc.id, parameter="ABS_NEUT", value=3.0),
        make_lab_value(document_id=doc.id, parameter="PLT", value=200.0),
        make_lab_value(document_id=doc.id, parameter="HGB", value=120.0),
        make_lab_value(document_id=doc.id, parameter="WBC", value=6.0),
        make_lab_value(document_id=doc.id, parameter="BILIRUBIN", value=10.0),
        make_lab_value(document_id=doc.id, parameter="CREATININE", value=80.0),
        make_lab_value(document_id=doc.id, parameter="ALT", value=30.0),
        make_lab_value(document_id=doc.id, parameter="AST", value=25.0),
        make_lab_value(document_id=doc.id, parameter="eGFR", value=90.0),
    ]
    await db.insert_lab_values(good_values)

    result = json.loads(await get_lab_safety_check(ctx))
    assert result["protocol"] == "mFOLFOX6"
    assert result["cycle_safe"] is True
    assert result["summary"]["green"] == 9
    assert result["summary"]["red"] == 0
    assert result["summary"]["missing"] == 0


async def test_lab_safety_check_not_safe_with_red(db: Database):
    """cycle_safe is False when any parameter is red."""
    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    # ABS_NEUT red, rest missing
    values = [
        make_lab_value(document_id=doc.id, parameter="ABS_NEUT", value=0.5),
    ]
    await db.insert_lab_values(values)

    result = json.loads(await get_lab_safety_check(ctx))
    assert result["cycle_safe"] is False
    assert result["summary"]["red"] >= 1


# ── get_precycle_checklist ───────────────────────────────────────────────


async def test_precycle_checklist_structure(db: Database):
    """Verify all 4 sections are returned with correct keys."""
    ctx = _mock_ctx(db)

    result = json.loads(await get_precycle_checklist(ctx))
    assert result["protocol"] == "mFOLFOX6"
    assert result["cycle"] == 3  # default

    section_keys = [s["section"] for s in result["sections"]]
    assert section_keys == [
        "lab_safety",
        "toxicity_assessment",
        "vte_monitoring",
        "general_assessment",
    ]

    # Each section has title and items
    for section in result["sections"]:
        assert "title" in section
        assert "items" in section
        assert len(section["items"]) > 0


async def test_precycle_checklist_custom_cycle(db: Database):
    """Cycle number is passed through."""
    ctx = _mock_ctx(db)
    result = json.loads(await get_precycle_checklist(ctx, cycle_number=5))
    assert result["cycle"] == 5


async def test_precycle_checklist_links_lab_values(db: Database):
    """Lab-linked checklist items include last_value when values are stored."""
    ctx = _mock_ctx(db)
    doc = make_doc()
    doc = await db.insert_document(doc)

    values = [
        make_lab_value(document_id=doc.id, parameter="ABS_NEUT", value=2.5),
        make_lab_value(document_id=doc.id, parameter="PLT", value=180.0),
    ]
    await db.insert_lab_values(values)

    result = json.loads(await get_precycle_checklist(ctx))
    lab_section = next(s for s in result["sections"] if s["section"] == "lab_safety")

    anc_item = next(i for i in lab_section["items"] if i["id"] == "anc")
    assert anc_item["last_value"] == 2.5
    assert "last_date" in anc_item
    assert anc_item["last_document_id"] == doc.id

    plt_item = next(i for i in lab_section["items"] if i["id"] == "plt")
    assert plt_item["last_value"] == 180.0


async def test_precycle_checklist_source_urls(db: Database):
    """All checklist items have source attribution."""
    ctx = _mock_ctx(db)

    result = json.loads(await get_precycle_checklist(ctx))
    for section in result["sections"]:
        for item in section["items"]:
            assert "source" in item
            assert item["source"]  # non-empty string
            assert "source_url" in item  # key exists (may be None)
