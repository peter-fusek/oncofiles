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
    assert ctx["name"] == "Erika Fusekova"
    assert ctx["biomarkers"]["KRAS"] == "mutant G12S (c.34G>A, p.(Gly12Ser))"
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
    patient_context._context.update(_DEFAULT_CONTEXT.copy())

    updated = update_context({"treatment": {"current_cycle": 5}})
    assert updated["treatment"]["current_cycle"] == 5
    # Original fields preserved
    assert updated["treatment"]["regimen"] == "mFOLFOX6 90%"
    assert updated["name"] == "Erika Fusekova"


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
    assert "**Diagnosis:**" in text
    assert "**Biomarkers:**" in text


def test_get_context_returns_default_when_empty():
    """get_context returns default if module context is empty."""
    from oncofiles import patient_context

    patient_context._context.clear()
    ctx = get_context()
    assert ctx["name"] == "Erika Fusekova"


def test_load_from_json_nonexistent(tmp_path: Path):
    """Loading from nonexistent file returns empty dict."""
    result = load_from_json(tmp_path / "nonexistent.json")
    assert result == {}
