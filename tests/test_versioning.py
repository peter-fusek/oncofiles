"""Tests for document versioning (migration 019)."""

from __future__ import annotations

from oncofiles.database import Database
from tests.helpers import ERIKA_UUID, make_doc

# ── Database layer ────────────────────────────────────────────────────────


async def test_new_document_defaults_to_version_1(db: Database):
    """A freshly inserted document has version=1 and no previous_version_id."""
    doc = await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    assert doc.version == 1
    assert doc.previous_version_id is None


async def test_insert_document_with_version(db: Database):
    """Version and previous_version_id are persisted correctly."""
    v1 = await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    v2 = await db.insert_document(
        make_doc(file_id="f2", version=2, previous_version_id=v1.id), patient_id=ERIKA_UUID
    )

    fetched = await db.get_document(v2.id)
    assert fetched.version == 2
    assert fetched.previous_version_id == v1.id


async def test_get_active_document_by_filename(db: Database):
    """Returns the active (non-deleted) document matching original_filename."""
    doc = await db.insert_document(
        make_doc(file_id="f1", original_filename="labs.pdf"), patient_id=ERIKA_UUID
    )
    result = await db.get_active_document_by_filename("labs.pdf", patient_id=ERIKA_UUID)
    assert result is not None
    assert result.id == doc.id


async def test_get_active_document_by_filename_excludes_deleted(db: Database):
    """Soft-deleted documents are not returned."""
    doc = await db.insert_document(
        make_doc(file_id="f1", original_filename="labs.pdf"), patient_id=ERIKA_UUID
    )
    await db.delete_document(doc.id)

    result = await db.get_active_document_by_filename("labs.pdf", patient_id=ERIKA_UUID)
    assert result is None


async def test_get_active_document_by_filename_returns_highest_version(db: Database):
    """When multiple active docs exist, returns the highest version."""
    await db.insert_document(
        make_doc(file_id="f1", original_filename="labs.pdf", version=1), patient_id=ERIKA_UUID
    )
    v2 = await db.insert_document(
        make_doc(file_id="f2", original_filename="labs.pdf", version=2), patient_id=ERIKA_UUID
    )

    result = await db.get_active_document_by_filename("labs.pdf", patient_id=ERIKA_UUID)
    assert result.id == v2.id
    assert result.version == 2


async def test_get_active_document_by_filename_not_found(db: Database):
    """Returns None when no matching document exists."""
    result = await db.get_active_document_by_filename("nonexistent.pdf", patient_id=ERIKA_UUID)
    assert result is None


# ── Version chain ─────────────────────────────────────────────────────────


async def test_version_chain_single_document(db: Database):
    """A document with no versions returns a chain of length 1."""
    doc = await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    chain = await db.get_document_version_chain(doc.id)
    assert len(chain) == 1
    assert chain[0].id == doc.id


async def test_version_chain_two_versions(db: Database):
    """Chain of two versions is returned newest first."""
    v1 = await db.insert_document(make_doc(file_id="f1", version=1), patient_id=ERIKA_UUID)
    v2 = await db.insert_document(
        make_doc(file_id="f2", version=2, previous_version_id=v1.id), patient_id=ERIKA_UUID
    )

    chain = await db.get_document_version_chain(v1.id)
    assert len(chain) == 2
    assert chain[0].id == v2.id
    assert chain[1].id == v1.id


async def test_version_chain_three_versions(db: Database):
    """Chain of three versions walks all the way back."""
    v1 = await db.insert_document(make_doc(file_id="f1", version=1), patient_id=ERIKA_UUID)
    v2 = await db.insert_document(
        make_doc(file_id="f2", version=2, previous_version_id=v1.id), patient_id=ERIKA_UUID
    )
    v3 = await db.insert_document(
        make_doc(file_id="f3", version=3, previous_version_id=v2.id), patient_id=ERIKA_UUID
    )

    # Query from any point in the chain
    chain = await db.get_document_version_chain(v1.id)
    assert len(chain) == 3
    assert [d.id for d in chain] == [v3.id, v2.id, v1.id]

    chain_from_middle = await db.get_document_version_chain(v2.id)
    assert len(chain_from_middle) == 3
    assert [d.id for d in chain_from_middle] == [v3.id, v2.id, v1.id]

    chain_from_latest = await db.get_document_version_chain(v3.id)
    assert len(chain_from_latest) == 3


async def test_version_chain_nonexistent(db: Database):
    """Chain for nonexistent document returns empty list."""
    chain = await db.get_document_version_chain(999)
    assert chain == []
