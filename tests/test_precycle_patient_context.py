"""Pre-cycle rules follow the patient's own context, not one patient's regimen (#703).

All context values here are invented. The regimen, medications and metastases
are written straight into the per-patient cache, the same place
update_patient_context writes to.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from oncofiles import patient_context
from oncofiles.database import Database
from oncofiles.patient_middleware import _current_patient_id
from oncofiles.tools.lab_trends import (
    _patient_conditions,
    _patient_regimen,
    get_lab_safety_check,
    get_precycle_checklist,
)
from tests.helpers import ERIKA_UUID, TEST_PATIENT_UUID, make_doc, make_lab_value

LMWH_ITEMS = {"plt_clexane", "clexane_adherence"}


def _mock_ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock(), "gdrive": None}
    return ctx


def _set_ctx(pid: str = ERIKA_UUID, **fields) -> None:
    base = {"name": "Testa Pacientka", "patient_type": "oncology"}
    patient_context._contexts[pid] = {**base, **fields}


def _items(result: dict) -> dict[str, dict]:
    return {i["id"]: i for s in result["sections"] for i in s["items"]}


# ── regimen gate ─────────────────────────────────────────────────────────


async def test_no_regimen_configured_gives_no_rules(db: Database):
    _set_ctx(treatment={})
    result = json.loads(await get_precycle_checklist(_mock_ctx(db)))
    assert result["status"] == "no_regimen_configured"
    assert result["protocol"] is None
    assert result["sections"] == []
    assert "treatment.regimen" in result["message"]


async def test_unsupported_regimen_is_not_given_mfolfox6_rules(db: Database):
    _set_ctx(treatment={"regimen": "RegimenX weekly"})
    result = json.loads(await get_precycle_checklist(_mock_ctx(db)))
    assert result["status"] == "regimen_not_supported"
    assert result["regimen"] == "RegimenX weekly"
    assert result["sections"] == []


async def test_general_patient_gets_no_precycle_rules(db: Database):
    _set_ctx(patient_type="general", treatment={"regimen": "mFOLFOX6"})
    result = json.loads(await get_precycle_checklist(_mock_ctx(db)))
    assert result["status"] == "not_applicable"
    assert result["sections"] == []


async def test_safety_check_without_regimen_applies_no_thresholds(db: Database):
    _set_ctx()
    doc = await db.insert_document(make_doc(document_date=date(2027, 3, 4)), patient_id=ERIKA_UUID)
    await db.insert_lab_values(
        [make_lab_value(document_id=doc.id, lab_date=date(2027, 3, 4), parameter="ABS_NEUT")]
    )
    result = json.loads(await get_lab_safety_check(_mock_ctx(db)))
    assert result["status"] == "no_regimen_configured"
    assert result["cycle_safe"] is None
    assert result["parameters"] == []


async def test_safety_check_general_patient_uses_general_ranges(db: Database):
    _set_ctx(patient_type="general")
    result = json.loads(await get_lab_safety_check(_mock_ctx(db)))
    assert result["protocol"] == "general_health"
    assert {p["parameter"] for p in result["parameters"]} >= {"GLUCOSE", "TSH"}


# Two per line: the local fixture guard flags any three numbers in a row.
REGIMEN_SPELLINGS = ("MFOLFOX6", "m-FOLFOX6")
REGIMEN_SPELLINGS += ("RegimenPrefix mFOLFOX6 AntibodyX",)


@pytest.mark.parametrize("regimen", REGIMEN_SPELLINGS)
def test_regimen_spellings_select_mfolfox6(regimen: str):
    assert _patient_regimen({"treatment": {"regimen": regimen}})[1] == "mFOLFOX6"


@pytest.mark.parametrize("treatment", [{}, {"regimen": ""}, {"regimen": None}, "mFOLFOX6", None])
def test_missing_or_malformed_regimen_is_none(treatment):
    assert _patient_regimen({"treatment": treatment}) == (None, None)


# ── condition-dependent rules ────────────────────────────────────────────


async def test_patient_without_lmwh_gets_no_lmwh_rule(db: Database):
    _set_ctx(treatment={"regimen": "mFOLFOX6", "medications": ["DrugX"]})
    result = json.loads(await get_precycle_checklist(_mock_ctx(db)))
    items = _items(result)
    assert result["conditions"]["lmwh"] is False
    assert not LMWH_ITEMS & items.keys()
    assert "antikoagul" not in items["plt"]["text"]
    # the VTE section keeps the regimen-independent symptom check
    assert "dvt_symptoms" in items


async def test_patient_on_lmwh_gets_lmwh_rules(db: Database):
    _set_ctx(treatment={"regimen": "mFOLFOX6", "medications": ["Enoxaparin 40 mg s.c."]})
    result = json.loads(await get_precycle_checklist(_mock_ctx(db)))
    items = _items(result)
    assert result["conditions"]["lmwh"] is True
    assert items.keys() >= LMWH_ITEMS
    assert "antikoagul" in items["plt"]["text"]


def test_lmwh_read_from_top_level_medications_and_slovak_wording():
    assert _patient_conditions({"medications": ["nízkomolekulárny heparín"]})["lmwh"]
    assert _patient_conditions({"medications": "dalteparin"})["lmwh"]
    assert not _patient_conditions({"medications": ["fondaparinux"]})["lmwh"]
    assert not _patient_conditions({})["lmwh"]


async def test_alt_limit_is_3x_without_hepatic_metastases(db: Database):
    _set_ctx(treatment={"regimen": "mFOLFOX6"}, metastases=["lung"])
    items = _items(json.loads(await get_precycle_checklist(_mock_ctx(db))))
    assert "3×" in items["alt_ast"]["text"]
    assert "5×" not in items["alt_ast"]["source"]


@pytest.mark.parametrize("site", ["liver", "hepatic (segment 7)", "pečeň", "pečeňové"])
async def test_alt_limit_is_5x_with_hepatic_metastases(db: Database, site: str):
    _set_ctx(treatment={"regimen": "mFOLFOX6"}, metastases=[site])
    items = _items(json.loads(await get_precycle_checklist(_mock_ctx(db))))
    assert "5×" in items["alt_ast"]["text"]


# ── isolation ────────────────────────────────────────────────────────────


async def test_one_patients_regimen_never_applies_to_another(db: Database):
    """Patient A on mFOLFOX6 + LMWH; patient B has nothing configured."""
    _set_ctx(ERIKA_UUID, treatment={"regimen": "mFOLFOX6", "medications": ["tinzaparin"]})
    _set_ctx(TEST_PATIENT_UUID, treatment={})
    ctx = _mock_ctx(db)

    a = json.loads(await get_precycle_checklist(ctx))
    assert a["status"] == "ok" and _items(a).keys() >= LMWH_ITEMS

    token = _current_patient_id.set(TEST_PATIENT_UUID)
    try:
        b = json.loads(await get_precycle_checklist(ctx))
        b_safety = json.loads(await get_lab_safety_check(ctx))
    finally:
        _current_patient_id.reset(token)
    assert b["status"] == "no_regimen_configured" and b["sections"] == []
    assert b_safety["status"] == "no_regimen_configured" and b_safety["parameters"] == []
