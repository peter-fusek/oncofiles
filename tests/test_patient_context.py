"""Tests for patient context module."""

import json
from pathlib import Path

from oncofiles.database import Database
from oncofiles.patient_context import (
    _DEFAULT_CONTEXT,
    format_context_text,
    get_context,
    initialize,
    load_from_json,
    save_to_db,
    update_context,
)


async def test_initialize_default():
    """Without DB or file, falls back to hardcoded default."""
    db = Database(":memory:")
    await db.connect()
    await db.migrate()

    ctx = await initialize(db.db)
    assert ctx["name"] == ""  # default is empty, loaded from DB/env
    # Clinical data is empty in defaults (loaded from DB/JSON at runtime)
    assert ctx["biomarkers"] == {}
    await db.close()


async def test_initialize_from_json(tmp_path: Path):
    """Load patient context from JSON file."""
    ctx_file = tmp_path / "patient.json"
    ctx_file.write_text(json.dumps({"name": "Test Patient", "diagnosis": "Test diagnosis"}))

    db = Database(":memory:")
    await db.connect()
    await db.migrate()

    ctx = await initialize(db.db, str(ctx_file))
    assert ctx["name"] == "Test Patient"
    assert ctx["diagnosis"] == "Test diagnosis"
    await db.close()


async def test_save_and_load_from_db():
    """Save to DB, then reload."""
    db = Database(":memory:")
    await db.connect()
    await db.migrate()

    # Save custom context
    custom = {**_DEFAULT_CONTEXT, "name": "DB Patient", "diagnosis": "From DB"}
    await save_to_db(db.db, custom)

    # Clear module state and reload
    from oncofiles import patient_context

    patient_context._context.clear()
    ctx = await initialize(db.db)
    assert ctx["name"] == "DB Patient"
    assert ctx["diagnosis"] == "From DB"
    await db.close()


async def test_update_context_merges():
    """update_context merges nested dicts."""
    from oncofiles import patient_context

    patient_context._context.clear()
    patient_context._context.update({**_DEFAULT_CONTEXT, "treatment": {"regimen": "test"}})

    updated = update_context({"treatment": {"current_cycle": 5}})
    assert updated["treatment"]["current_cycle"] == 5
    # Original fields preserved
    assert updated["treatment"]["regimen"] == "test"
    assert updated["name"] == ""  # default is empty


async def test_update_context_replaces_list():
    """update_context replaces lists entirely."""
    from oncofiles import patient_context

    patient_context._context.clear()
    patient_context._context.update(_DEFAULT_CONTEXT.copy())

    updated = update_context({"comorbidities": ["New comorbidity"]})
    assert updated["comorbidities"] == ["New comorbidity"]


def test_format_context_text():
    """format_context_text returns a readable string."""
    text = format_context_text()
    assert "**Patient:**" in text


def test_get_context_returns_default_when_empty():
    """get_context returns default if module context is empty."""
    from oncofiles import patient_context

    patient_context._context.clear()
    ctx = get_context()
    assert ctx["name"] == ""  # default is empty, loaded from DB/env


def test_load_from_json_nonexistent(tmp_path: Path):
    """Loading from nonexistent file returns empty dict."""
    result = load_from_json(tmp_path / "nonexistent.json")
    assert result == {}


def test_set_germline_finding_records_and_normalizes():
    """set_germline_finding uppercases gene + canonicalizes classification."""
    from oncofiles import patient_context
    from oncofiles.patient_context import set_germline_finding

    patient_context._context.clear()
    patient_context._context.update(_DEFAULT_CONTEXT.copy())

    ctx = set_germline_finding(
        "brca1",
        "Likely Pathogenic",
        variant="c.5266dupC",
        zygosity="heterozygous",
        test_date="2026-04-10",
        test_lab="Ambry",
    )
    f = ctx["germline_findings"]["BRCA1"]
    assert f["classification"] == "likely_pathogenic"
    assert f["variant"] == "c.5266dupC"
    assert f["test_lab"] == "Ambry"


def test_get_germline_status_unknown_for_missing_gene():
    """get_germline_status returns 'unknown' when gene not recorded."""
    from oncofiles import patient_context
    from oncofiles.patient_context import get_germline_status, set_germline_finding

    patient_context._context.clear()
    patient_context._context.update(_DEFAULT_CONTEXT.copy())

    set_germline_finding("BRCA1", "pathogenic")
    assert get_germline_status("BRCA1") == "pathogenic"
    assert get_germline_status("MLH1") == "unknown"
    assert get_germline_status("") == "unknown"


def test_format_context_surfaces_germline_banner():
    """format_context_text includes a 'Germline findings' section when set."""
    from oncofiles import patient_context
    from oncofiles.patient_context import format_context_text, set_germline_finding

    patient_context._context.clear()
    patient_context._context.update(_DEFAULT_CONTEXT.copy())
    set_germline_finding(
        "BRCA2",
        "pathogenic",
        variant="c.5946delT",
        test_lab="Centogene",
    )
    text = format_context_text()
    assert "Germline findings" in text
    assert "BRCA2" in text
    assert "pathogenic" in text
