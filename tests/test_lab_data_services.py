"""Tests for lab data service tools (S5 v3.14.0): time series, comparison, summary."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

from oncofiles.database import Database
from oncofiles.tools.lab_trends import compare_lab_panels, get_lab_summary, get_lab_time_series
from tests.helpers import make_doc, make_lab_value


def _mock_ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock(), "gdrive": None}
    return ctx


async def _seed_lab_data(db: Database) -> int:
    """Insert a doc and multi-date lab values. Returns doc id."""
    doc = make_doc()
    doc = await db.insert_document(doc)
    doc2 = make_doc(file_id="file_test456", filename="20260301_NOUonko_labs_bio.pdf")
    doc2 = await db.insert_document(doc2)

    values = [
        # Date 1: 2026-02-15
        make_lab_value(
            document_id=doc.id,
            lab_date=date(2026, 2, 15),
            parameter="WBC",
            value=6.8,
            reference_low=4.0,
            reference_high=10.0,
        ),
        make_lab_value(
            document_id=doc.id,
            lab_date=date(2026, 2, 15),
            parameter="PLT",
            value=220.0,
            unit="10^9/L",
            reference_low=150.0,
            reference_high=400.0,
        ),
        make_lab_value(
            document_id=doc.id,
            lab_date=date(2026, 2, 15),
            parameter="ABS_NEUT",
            value=4.2,
            reference_low=1.8,
            reference_high=7.0,
        ),
        make_lab_value(
            document_id=doc.id,
            lab_date=date(2026, 2, 15),
            parameter="ABS_LYMPH",
            value=1.5,
            reference_low=1.0,
            reference_high=4.0,
        ),
        make_lab_value(
            document_id=doc.id,
            lab_date=date(2026, 2, 15),
            parameter="CEA",
            value=12.5,
            unit="ng/mL",
            reference_low=None,
            reference_high=5.0,
        ),
        # Date 2: 2026-03-01
        make_lab_value(
            document_id=doc2.id,
            lab_date=date(2026, 3, 1),
            parameter="WBC",
            value=5.2,
            reference_low=4.0,
            reference_high=10.0,
        ),
        make_lab_value(
            document_id=doc2.id,
            lab_date=date(2026, 3, 1),
            parameter="PLT",
            value=180.0,
            unit="10^9/L",
            reference_low=150.0,
            reference_high=400.0,
        ),
        make_lab_value(
            document_id=doc2.id,
            lab_date=date(2026, 3, 1),
            parameter="ABS_NEUT",
            value=3.1,
            reference_low=1.8,
            reference_high=7.0,
        ),
        make_lab_value(
            document_id=doc2.id,
            lab_date=date(2026, 3, 1),
            parameter="ABS_LYMPH",
            value=1.8,
            reference_low=1.0,
            reference_high=4.0,
        ),
        make_lab_value(
            document_id=doc2.id,
            lab_date=date(2026, 3, 1),
            parameter="CEA",
            value=8.3,
            unit="ng/mL",
            reference_low=None,
            reference_high=5.0,
        ),
    ]
    await db.insert_lab_values(values)
    return doc.id


# ── get_lab_time_series ──────────────────────────────────────────────────


async def test_time_series_single_param(db: Database):
    await _seed_lab_data(db)
    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_time_series(ctx, parameters="WBC"))

    assert "parameters" in result
    assert "WBC" in result["parameters"]
    series = result["parameters"]["WBC"]["series"]
    assert len(series) == 2
    assert series[0]["value"] == 6.8
    assert series[1]["value"] == 5.2
    # Second point should have delta
    assert "delta" in series[1]
    assert series[1]["delta"] == round(5.2 - 6.8, 4)
    assert "delta_pct" in series[1]


async def test_time_series_multiple_params(db: Database):
    await _seed_lab_data(db)
    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_time_series(ctx, parameters="WBC,CEA,PLT"))

    assert len(result["parameters"]) == 3
    assert result["parameters"]["CEA"]["count"] == 2
    assert result["parameters"]["PLT"]["count"] == 2


async def test_time_series_date_filter(db: Database):
    await _seed_lab_data(db)
    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_time_series(ctx, parameters="WBC", date_from="2026-02-20"))
    series = result["parameters"]["WBC"]["series"]
    assert len(series) == 1
    assert series[0]["date"] == "2026-03-01"


async def test_time_series_no_params(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_time_series(ctx, parameters=""))
    assert "error" in result


async def test_time_series_empty_result(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_time_series(ctx, parameters="NONEXISTENT"))
    assert result["parameters"]["NONEXISTENT"]["count"] == 0


# ── compare_lab_panels ───────────────────────────────────────────────────


async def test_compare_panels_basic(db: Database):
    await _seed_lab_data(db)
    ctx = _mock_ctx(db)
    result = json.loads(await compare_lab_panels(ctx, "2026-02-15", "2026-03-01"))

    assert result["date_a"] == "2026-02-15"
    assert result["date_b"] == "2026-03-01"
    assert result["total"] == 5

    # Check WBC comparison
    wbc = next(p for p in result["parameters"] if p["parameter"] == "WBC")
    assert wbc["delta"] == round(5.2 - 6.8, 4)
    assert wbc["direction"] == "falling"
    assert wbc["status"] == "in_range"


async def test_compare_panels_out_of_range(db: Database):
    await _seed_lab_data(db)
    ctx = _mock_ctx(db)
    result = json.loads(await compare_lab_panels(ctx, "2026-02-15", "2026-03-01"))

    # CEA is above reference_high (5.0) on both dates
    cea = next(p for p in result["parameters"] if p["parameter"] == "CEA")
    assert cea["status"] == "above_range"
    assert cea["direction"] == "falling"  # 12.5 -> 8.3


async def test_compare_panels_invalid_date(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await compare_lab_panels(ctx, "bad-date", "2026-03-01"))
    assert "error" in result


async def test_compare_panels_includes_available_dates(db: Database):
    await _seed_lab_data(db)
    ctx = _mock_ctx(db)
    result = json.loads(await compare_lab_panels(ctx, "2026-02-15", "2026-03-01"))
    assert "available_dates" in result
    assert "2026-03-01" in result["available_dates"]


async def test_compare_panels_one_date_only(db: Database):
    """When a parameter exists on only one date, status is 'only_one_date'."""
    doc = make_doc()
    doc = await db.insert_document(doc)
    await db.insert_lab_values(
        [
            make_lab_value(
                document_id=doc.id,
                lab_date=date(2026, 1, 10),
                parameter="HGB",
                value=120.0,
            ),
        ]
    )
    ctx = _mock_ctx(db)
    result = json.loads(await compare_lab_panels(ctx, "2026-01-10", "2026-01-20"))
    hgb = next(p for p in result["parameters"] if p["parameter"] == "HGB")
    assert hgb["status"] == "only_one_date"


# ── get_lab_summary ──────────────────────────────────────────────────────


async def test_summary_basic(db: Database):
    await _seed_lab_data(db)
    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_summary(ctx))

    assert result["total_parameters"] == 5
    params = {p["parameter"]: p for p in result["parameters"]}
    assert "WBC" in params
    assert "CEA" in params
    assert params["WBC"]["value"] == 5.2  # latest
    assert params["WBC"]["date"] == "2026-03-01"
    assert "days_ago" in params["WBC"]


async def test_summary_status_flags(db: Database):
    await _seed_lab_data(db)
    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_summary(ctx))

    params = {p["parameter"]: p for p in result["parameters"]}
    assert params["WBC"]["status"] == "normal"  # 5.2 in [4.0, 10.0]
    assert params["CEA"]["status"] == "high"  # 8.3 > 5.0


async def test_summary_trend_direction(db: Database):
    await _seed_lab_data(db)
    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_summary(ctx))

    params = {p["parameter"]: p for p in result["parameters"]}
    assert params["WBC"]["trend"] == "falling"  # 6.8 -> 5.2
    assert params["CEA"]["trend"] == "falling"  # 12.5 -> 8.3
    assert params["ABS_LYMPH"]["trend"] == "rising"  # 1.5 -> 1.8


async def test_summary_computed_indices(db: Database):
    await _seed_lab_data(db)
    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_summary(ctx))

    computed = {c["parameter"]: c for c in result["computed_indices"]}
    assert "SII" in computed
    assert "NE_LY_RATIO" in computed

    # SII = (3.1 * 180.0) / 1.8 = 310.0
    assert computed["SII"]["value"] == 310.0
    assert computed["SII"]["interpretation"] == "normal"  # < 1800

    # Ne/Ly = 3.1 / 1.8 = 1.72
    assert computed["NE_LY_RATIO"]["value"] == round(3.1 / 1.8, 2)
    assert computed["NE_LY_RATIO"]["interpretation"] == "improving"  # < 2.5


async def test_summary_empty(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_summary(ctx))
    assert result["total_parameters"] == 0
    assert result["computed_indices"] == []


async def test_summary_includes_available_dates(db: Database):
    await _seed_lab_data(db)
    ctx = _mock_ctx(db)
    result = json.loads(await get_lab_summary(ctx))
    assert "available_dates" in result
    assert len(result["available_dates"]) == 2


# ── Database layer: new methods ──────────────────────────────────────────


async def test_get_all_latest_lab_values(db: Database):
    await _seed_lab_data(db)
    latest = await db.get_all_latest_lab_values()
    params = {v.parameter: v for v in latest}
    assert len(params) == 5
    assert params["WBC"].value == 5.2  # latest date
    assert params["CEA"].value == 8.3


async def test_get_lab_values_by_date(db: Database):
    await _seed_lab_data(db)
    values = await db.get_lab_values_by_date("2026-02-15")
    assert len(values) == 5
    params = {v.parameter for v in values}
    assert "WBC" in params
    assert "PLT" in params


async def test_get_distinct_lab_dates(db: Database):
    await _seed_lab_data(db)
    dates = await db.get_distinct_lab_dates()
    assert dates == ["2026-03-01", "2026-02-15"]  # most recent first
