"""Tests for patient context module (per-patient only since #733)."""

import pytest

from oncofiles import patient_context
from oncofiles.database import Database
from oncofiles.models import Patient
from oncofiles.patient_context import (
    _default_context,
    format_context_text,
    get_context,
    initialize,
    load_from_db,
    save_to_db,
    update_context,
)
from oncofiles.patient_middleware import _current_patient_id

# Synthetic pid; the autouse conftest fixture restores `_contexts` after each test.
_PID = "c0000000-0000-4000-8000-00000000cccc"


async def _mem_db() -> Database:
    db = Database(":memory:")
    await db.connect()
    await db.migrate()
    return db


async def test_initialize_hydrates_every_active_patient_by_pid():
    """initialize() loads each active patient's stored row into its own cache slot."""
    db = await _mem_db()
    try:
        a = await db.insert_patient(Patient(patient_id="synth-alpha", display_name="Testa Alpha"))
        b = await db.insert_patient(Patient(patient_id="synth-beta", display_name="Testa Beta"))
        c = await db.insert_patient(Patient(patient_id="synth-gamma", display_name="Testa Gamma"))
        await save_to_db(db.db, {"name": "Testa Alpha", "note": "A"}, patient_id=a.patient_id)
        await save_to_db(db.db, {"name": "Testa Beta", "note": "B"}, patient_id=b.patient_id)
        patient_context._contexts.clear()

        loaded = await initialize(db)

        assert loaded == 2  # gamma has no stored row
        assert get_context(a.patient_id)["note"] == "A"
        assert get_context(b.patient_id)["note"] == "B"
        assert get_context(c.patient_id)["name"] == ""  # empty template, nobody else's data
        assert set(patient_context._contexts) == {a.patient_id, b.patient_id}
    finally:
        await db.close()


async def test_initialize_with_no_patients_loads_nothing():
    db = await _mem_db()
    try:
        patient_context._contexts.clear()
        assert await initialize(db) == 0
        assert patient_context._contexts == {}
    finally:
        await db.close()


async def test_save_and_load_from_db():
    """Save to DB, then reload into a cold cache."""
    db = await _mem_db()
    try:
        custom = {**_default_context(), "name": "DB Patient", "diagnosis": "From DB"}
        await save_to_db(db.db, custom, patient_id=_PID)
        patient_context._contexts.clear()

        ctx = await load_from_db(db.db, _PID)
        assert ctx["name"] == "DB Patient"
        assert get_context(_PID)["diagnosis"] == "From DB"
    finally:
        await db.close()


@pytest.mark.parametrize("pid", [None, ""])
async def test_save_and_load_require_a_patient(pid):
    db = await _mem_db()
    try:
        with pytest.raises(ValueError, match="patient_slug"):
            await save_to_db(db.db, {"name": "x"}, patient_id=pid)
        with pytest.raises(ValueError, match="patient_slug"):
            await load_from_db(db.db, pid)
    finally:
        await db.close()


async def test_update_context_merges():
    """update_context merges nested dicts."""
    patient_context._contexts[_PID] = {**_default_context(), "treatment": {"regimen": "test"}}

    updated = update_context({"treatment": {"current_cycle": 5}}, patient_id=_PID)
    assert updated["treatment"]["current_cycle"] == 5
    # Original fields preserved
    assert updated["treatment"]["regimen"] == "test"
    assert updated["name"] == ""  # default is empty


async def test_update_context_replaces_list():
    """update_context replaces lists entirely."""
    patient_context._contexts[_PID] = _default_context()

    updated = update_context({"comorbidities": ["New comorbidity"]}, patient_id=_PID)
    assert updated["comorbidities"] == ["New comorbidity"]


def test_format_context_text():
    """format_context_text returns a readable string for an explicit patient."""
    text = format_context_text(_PID)
    assert "**Patient:**" in text


def test_format_context_text_without_patient_raises():
    with pytest.raises(ValueError, match="patient_slug"):
        format_context_text()


def test_get_context_unknown_pid_returns_empty_template():
    ctx = get_context(_PID)
    assert ctx["name"] == ""
    assert ctx == _default_context()


def test_default_context_is_fresh_each_call():
    a = _default_context()
    a["treatment"]["regimen"] = "SYNTH"
    assert _default_context()["treatment"] == {}


def test_set_germline_finding_records_and_normalizes():
    """set_germline_finding uppercases gene + canonicalizes classification."""
    from oncofiles.patient_context import set_germline_finding

    ctx = set_germline_finding(
        "brca1",
        "Likely Pathogenic",
        variant="c.5266dupC",
        zygosity="heterozygous",
        test_date="2027-04-10",
        test_lab="SynthLab",
        patient_id=_PID,
    )
    f = ctx["germline_findings"]["BRCA1"]
    assert f["classification"] == "likely_pathogenic"
    assert f["variant"] == "c.5266dupC"
    assert f["test_lab"] == "SynthLab"


def test_get_germline_status_unknown_for_missing_gene():
    """get_germline_status returns 'unknown' when gene not recorded."""
    from oncofiles.patient_context import get_germline_status, set_germline_finding

    set_germline_finding("BRCA1", "pathogenic", patient_id=_PID)
    assert get_germline_status("BRCA1", patient_id=_PID) == "pathogenic"
    assert get_germline_status("MLH1", patient_id=_PID) == "unknown"
    assert get_germline_status("") == "unknown"


def test_format_context_surfaces_germline_banner():
    """format_context_text includes a 'Germline findings' section when set."""
    from oncofiles.patient_context import set_germline_finding

    set_germline_finding(
        "BRCA2",
        "pathogenic",
        variant="c.5946delT",
        test_lab="SynthLab",
        patient_id=_PID,
    )
    text = format_context_text(patient_id=_PID)
    assert "Germline findings" in text
    assert "BRCA2" in text
    assert "pathogenic" in text


def test_request_pid_resolves_without_explicit_pid():
    """Inside a request the middleware ContextVar is the patient (no explicit pid needed)."""
    patient_context._contexts[_PID] = {"name": "Testa Request"}
    token = _current_patient_id.set(_PID)
    try:
        assert get_context()["name"] == "Testa Request"
    finally:
        _current_patient_id.reset(token)
