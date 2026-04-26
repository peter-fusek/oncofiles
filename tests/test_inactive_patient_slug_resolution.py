"""Locks #509 — inactive patient slugs are NOT resolvable through MCP/dashboard
patient-slug paths by default.

Pre-fix: `get_patient_by_slug(slug)` returned the row regardless of
`is_active`, so a caregiver who knew an archived patient's slug could still
target them through any MCP tool that accepts `patient_slug`. Same gap on
`resolve_patient_id` (the canonical slug→UUID resolver).

Post-fix: both helpers default to `active_only=True`. Admin recovery / audit
flows that genuinely need archived rows must opt in via `active_only=False`.
"""

from __future__ import annotations

from oncofiles.database import Database
from oncofiles.models import Patient

ARCHIVED_UUID = "00000000-0000-4000-8000-000000000099"
ARCHIVED_SLUG = "archived-patient"
ACTIVE_UUID = "00000000-0000-4000-8000-000000000098"
ACTIVE_SLUG = "active-patient"


async def _seed_active_and_archived(db: Database) -> None:
    await db.insert_patient(
        Patient(
            patient_id=ACTIVE_UUID,
            slug=ACTIVE_SLUG,
            display_name="Active Patient",
            caregiver_email="active@example.com",
        )
    )
    await db.insert_patient(
        Patient(
            patient_id=ARCHIVED_UUID,
            slug=ARCHIVED_SLUG,
            display_name="Archived Patient",
            caregiver_email="archived@example.com",
        )
    )
    # Mark the second as archived AFTER insert so the insert path doesn't
    # short-circuit on is_active=False.
    await db.db.execute(
        "UPDATE patients SET is_active = 0 WHERE patient_id = ?",
        (ARCHIVED_UUID,),
    )
    await db.db.commit()


async def test_get_patient_by_slug_excludes_archived_by_default(db: Database):
    await _seed_active_and_archived(db)
    assert await db.get_patient_by_slug(ACTIVE_SLUG) is not None
    assert await db.get_patient_by_slug(ARCHIVED_SLUG) is None


async def test_get_patient_by_slug_admin_can_opt_in_to_archived(db: Database):
    """Admin recovery / audit flows need to find archived rows; opt-in via
    `active_only=False` keeps that path open."""
    await _seed_active_and_archived(db)
    p = await db.get_patient_by_slug(ARCHIVED_SLUG, active_only=False)
    assert p is not None
    assert p.is_active is False


async def test_resolve_patient_id_blocks_archived_slug_by_default(db: Database):
    """The canonical slug→UUID resolver also defaults to active-only — this
    is what every MCP `patient_slug` flow sits on top of (`_resolve_patient_id`
    in tools/_helpers.py uses `get_patient_by_slug` directly, but anything
    that calls `db.resolve_patient_id` gets the same default)."""
    await _seed_active_and_archived(db)
    assert await db.resolve_patient_id(ACTIVE_SLUG) == ACTIVE_UUID
    assert await db.resolve_patient_id(ARCHIVED_SLUG) is None


async def test_resolve_patient_id_uuid_path_also_blocks_archived_by_default(
    db: Database,
):
    """The UUID path didn't go through `get_patient_by_slug`, so the original
    fix had to also gate the UUID branch — otherwise a caller who knows the
    archived patient's UUID could still resolve it. Lock that branch too."""
    await _seed_active_and_archived(db)
    assert await db.resolve_patient_id(ACTIVE_UUID) == ACTIVE_UUID
    assert await db.resolve_patient_id(ARCHIVED_UUID) is None


async def test_resolve_patient_id_uuid_path_admin_opt_in(db: Database):
    """UUID path also honors `active_only=False` for admin recovery."""
    await _seed_active_and_archived(db)
    assert await db.resolve_patient_id(ARCHIVED_UUID, active_only=False) == ARCHIVED_UUID
