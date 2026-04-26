"""Patient-scoping on `get_ocr_document_ids` (#504/#505).

Before this fix, the helper ran ``SELECT DISTINCT document_id FROM document_pages``
with no patient filter, so any caller comparing it against a per-patient
document list was silently consuming a cross-patient set. This test locks
both invariants:

* ``get_ocr_document_ids(patient_id=)`` returns only that patient's ids.
* The explicit cross-patient variant ``get_ocr_document_ids_unscoped_for_admin``
  still returns all rows — admin paths that need a global view (rare,
  documented) keep working.
"""

from __future__ import annotations

from oncofiles.database import Database
from oncofiles.models import Patient
from tests.conftest import ERIKA_UUID
from tests.helpers import make_doc

BOB_UUID = "00000000-0000-4000-8000-000000000002"


async def _seed_two_patients_with_ocr(db: Database) -> tuple[int, int]:
    """Insert Erika doc + Bob doc, save 1 OCR page on each. Returns (erika_id, bob_id)."""
    await db.insert_patient(
        Patient(
            patient_id=BOB_UUID,
            slug="bob-test",
            display_name="Bob Test",
            caregiver_email="bob@example.com",
        )
    )

    erika_doc = await db.insert_document(
        make_doc(file_id="erika_ocr", filename="20260101_Erika-NOU-Labs.pdf"),
        patient_id=ERIKA_UUID,
    )
    bob_doc = await db.insert_document(
        make_doc(file_id="bob_ocr", filename="20260101_Bob-NOU-Report.pdf"),
        patient_id=BOB_UUID,
    )
    await db.save_ocr_page(erika_doc.id, 1, "erika page text", "test-model")
    await db.save_ocr_page(bob_doc.id, 1, "bob page text", "test-model")
    return erika_doc.id, bob_doc.id


async def test_scoped_returns_only_caller_patient(db: Database):
    erika_id, bob_id = await _seed_two_patients_with_ocr(db)
    assert await db.get_ocr_document_ids(patient_id=ERIKA_UUID) == {erika_id}
    assert await db.get_ocr_document_ids(patient_id=BOB_UUID) == {bob_id}


async def test_scoped_does_not_leak_other_patient(db: Database):
    erika_id, bob_id = await _seed_two_patients_with_ocr(db)
    erika_set = await db.get_ocr_document_ids(patient_id=ERIKA_UUID)
    assert bob_id not in erika_set


async def test_scoped_empty_set_for_unknown_patient(db: Database):
    await _seed_two_patients_with_ocr(db)
    # Unknown patient id → empty set, not the global set.
    unknown = await db.get_ocr_document_ids(patient_id="00000000-0000-0000-0000-000000000099")
    assert unknown == set()


async def test_unscoped_admin_variant_still_global(db: Database):
    erika_id, bob_id = await _seed_two_patients_with_ocr(db)
    # Reserved for admin paths only; behaviour preserved.
    assert await db.get_ocr_document_ids_unscoped_for_admin() == {erika_id, bob_id}
