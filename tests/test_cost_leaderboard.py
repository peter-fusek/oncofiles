"""Tests for `db.get_per_patient_cost_leaderboard` (#411 Part A).

The leaderboard sums `prompt_log.estimated_cost_usd` per patient over a
rolling window and returns the top spenders so admin can spot anomalies
without drilling into each patient one at a time.

Locked invariants:
- Sorted by total_cost_usd descending.
- Patients with zero activity in the window are excluded.
- COALESCE fallback: when `estimated_cost_usd` is NULL on every row for a
  patient, cost is recomputed from token counts via the same Haiku formula
  used by `get_prompt_stats` — patients don't silently fall under "free".
- The leaderboard surfaces slug + display_name from the `patients` table so
  the dashboard doesn't have to re-query per row.
- The `__system_no_patient__` sentinel never crashes the resolver — it's
  passed through as a row with empty slug/display_name (operator-level
  activity that doesn't belong to any caregiver).
"""

from __future__ import annotations

from oncofiles.models import Patient, PromptCallType, PromptLogEntry
from tests.conftest import ERIKA_UUID

BOB_UUID = "00000000-0000-4000-8000-000000000002"
CAROL_UUID = "00000000-0000-4000-8000-000000000003"


async def _seed_three_patients(db) -> None:
    """Seed Bob and Carol alongside the conftest's Erika."""
    await db.insert_patient(
        Patient(
            patient_id=BOB_UUID,
            slug="bob-test",
            display_name="Bob Test",
            caregiver_email="bob@example.com",
        )
    )
    await db.insert_patient(
        Patient(
            patient_id=CAROL_UUID,
            slug="carol-test",
            display_name="Carol Test",
            caregiver_email="carol@example.com",
        )
    )


async def _log(db, *, patient_id: str, cost: float | None, call_type=PromptCallType.SUMMARY_TAGS):
    entry = PromptLogEntry(
        call_type=call_type,
        patient_id=patient_id,
        model="claude-haiku-4-5",
        input_tokens=100,
        output_tokens=50,
    )
    inserted = await db.insert_prompt_log(entry)
    if cost is not None:
        await db.db.execute(
            "UPDATE prompt_log SET estimated_cost_usd = ? WHERE id = ?",
            (cost, inserted.id),
        )
        await db.db.commit()


# ── Sort + filter ────────────────────────────────────────────────────


async def test_leaderboard_sorts_by_cost_desc(db):
    """The whole point of the leaderboard: top spender first."""
    await _seed_three_patients(db)
    await _log(db, patient_id=ERIKA_UUID, cost=0.50)
    await _log(db, patient_id=BOB_UUID, cost=2.10)
    await _log(db, patient_id=CAROL_UUID, cost=0.05)

    rows = await db.get_per_patient_cost_leaderboard(days=30)
    pids = [r["patient_id"] for r in rows]
    assert pids == [BOB_UUID, ERIKA_UUID, CAROL_UUID]


async def test_leaderboard_excludes_patients_with_no_activity(db):
    """Patients with zero rows in the window do NOT appear on the leaderboard.

    Empty patients are noise on the anomaly-review surface — admin only
    cares about who's actually spending. Bob is logged; Erika and Carol
    have no prompt_log rows.
    """
    await _seed_three_patients(db)
    await _log(db, patient_id=BOB_UUID, cost=1.00)

    rows = await db.get_per_patient_cost_leaderboard(days=30)
    assert len(rows) == 1
    assert rows[0]["patient_id"] == BOB_UUID


# ── COALESCE / cost-recompute fallback ───────────────────────────────


async def test_leaderboard_recomputes_cost_for_null_billed_rows(db):
    """When `estimated_cost_usd` is NULL on every row for a patient (e.g.
    pre-migration-058 history), the leaderboard MUST recompute cost from
    token counts using the same Haiku formula as `get_prompt_stats` —
    otherwise old patients show as $0 and disappear under newer activity.
    """
    await _seed_three_patients(db)
    # Erika: NULL cost on every row, but real tokens
    await _log(db, patient_id=ERIKA_UUID, cost=None)
    await _log(db, patient_id=ERIKA_UUID, cost=None)
    # Bob: explicit $0.40 (one row)
    await _log(db, patient_id=BOB_UUID, cost=0.40)

    rows = await db.get_per_patient_cost_leaderboard(days=30)
    by_pid = {r["patient_id"]: r for r in rows}
    # Erika: 200 input + 100 output tokens × Haiku pricing → tiny but > 0
    assert by_pid[ERIKA_UUID]["total_cost_usd"] > 0
    assert by_pid[BOB_UUID]["total_cost_usd"] == 0.40


# ── Slug / display_name surfacing ────────────────────────────────────


async def test_leaderboard_surfaces_slug_and_display_name(db):
    """Dashboard renders the patient row; it should NOT have to round-trip
    `/api/patients` per leaderboard row."""
    await _seed_three_patients(db)
    await _log(db, patient_id=BOB_UUID, cost=1.00)

    rows = await db.get_per_patient_cost_leaderboard(days=30)
    assert rows[0]["slug"] == "bob-test"
    assert rows[0]["display_name"] == "Bob Test"


async def test_leaderboard_handles_system_sentinel_patient(db):
    """`__system_no_patient__` rows (operator activity outside any patient)
    must not crash the slug resolver — they appear with empty slug/name so
    admin can see the volume but the row obviously isn't a caregiver.
    """
    await _log(db, patient_id="__system_no_patient__", cost=0.10)

    rows = await db.get_per_patient_cost_leaderboard(days=30)
    assert len(rows) == 1
    assert rows[0]["patient_id"] == "__system_no_patient__"
    assert rows[0]["slug"] == ""
    assert rows[0]["display_name"] == ""
    # And it doesn't error out on the patients-table lookup


# ── top_call_type per patient ────────────────────────────────────────


async def test_leaderboard_reports_top_call_type_per_patient(db):
    """The dashboard wants to label a patient row with its dominant call
    type ('summary_tags', 'doc_classification' etc.) without re-querying."""
    await _seed_three_patients(db)
    await _log(db, patient_id=BOB_UUID, cost=0.10, call_type=PromptCallType.SUMMARY_TAGS)
    await _log(db, patient_id=BOB_UUID, cost=0.10, call_type=PromptCallType.SUMMARY_TAGS)
    await _log(db, patient_id=BOB_UUID, cost=0.10, call_type=PromptCallType.STRUCTURED_METADATA)

    rows = await db.get_per_patient_cost_leaderboard(days=30)
    assert rows[0]["top_call_type"] == "summary_tags"


# ── Limit ────────────────────────────────────────────────────────────


async def test_leaderboard_limit_caps_returned_rows(db):
    """The `limit` arg actually limits."""
    await _seed_three_patients(db)
    await _log(db, patient_id=ERIKA_UUID, cost=0.50)
    await _log(db, patient_id=BOB_UUID, cost=2.10)
    await _log(db, patient_id=CAROL_UUID, cost=0.05)

    rows = await db.get_per_patient_cost_leaderboard(days=30, limit=2)
    assert len(rows) == 2
    # Top 2 by cost
    assert {r["patient_id"] for r in rows} == {BOB_UUID, ERIKA_UUID}
