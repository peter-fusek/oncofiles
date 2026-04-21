"""Tests for Option A (#429) `patient_slug` rollout on documents.py tools.

These tests ride the ``db`` fixture (ERIKA_UUID scoped via middleware ContextVar)
but then call the tool with an explicit ``patient_slug`` — proving that the
resolved pid is driven by the slug, not by the ContextVar. That's the
stateless-HTTP guarantee needed for Claude.ai / ChatGPT connectors.
"""

from __future__ import annotations

import json

from oncofiles.database import Database
from oncofiles.models import DocumentCategory
from oncofiles.tools import documents as doc_tools
from tests.conftest import ERIKA_UUID
from tests.helpers import make_doc

SECOND_UUID = "00000000-0000-4000-8000-000000000002"
SECOND_SLUG = "bob-test"


async def _seed_two_patients(db: Database) -> None:
    """Insert a second patient + one doc per patient."""
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (SECOND_UUID, SECOND_SLUG, "Bob Test", "bob@example.com"),
    )
    await db.db.commit()

    # One doc under Erika (ContextVar scope), one under Bob (slug-targeted scope)
    await db.insert_document(
        make_doc(
            file_id="erika_doc1",
            filename="20260101_Erika-NOU-Labs.pdf",
            category=DocumentCategory.LABS,
        ),
        patient_id=ERIKA_UUID,
    )
    await db.insert_document(
        make_doc(
            file_id="bob_doc1",
            filename="20260101_Bob-NOU-Report.pdf",
            category=DocumentCategory.REPORT,
        ),
        patient_id=SECOND_UUID,
    )


class _StubCtx:
    """Minimal Context stub exposing ``request_context.lifespan_context['db']``."""

    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


async def test_list_documents_respects_explicit_patient_slug(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)

    # Default (no slug) → ContextVar = ERIKA_UUID → Erika's doc only
    default = json.loads(await doc_tools.list_documents(ctx))
    assert len(default["documents"]) == 1
    assert default["documents"][0]["file_id"] == "erika_doc1"

    # Explicit slug override → Bob's doc instead
    bob_scoped = json.loads(await doc_tools.list_documents(ctx, patient_slug=SECOND_SLUG))
    assert len(bob_scoped["documents"]) == 1
    assert bob_scoped["documents"][0]["file_id"] == "bob_doc1"


async def test_get_document_by_id_blocks_cross_patient_via_slug(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)

    # Find each doc's integer id
    all_docs = await db.list_documents(limit=10, patient_id=ERIKA_UUID)
    erika_doc_id = all_docs[0].id
    all_bob = await db.list_documents(limit=10, patient_id=SECOND_UUID)
    bob_doc_id = all_bob[0].id

    # Default ContextVar scope (Erika): Erika's doc found, Bob's doc NOT found
    erika_ok = json.loads(await doc_tools.get_document_by_id(ctx, erika_doc_id))
    assert erika_ok.get("id") == erika_doc_id

    bob_blocked = json.loads(await doc_tools.get_document_by_id(ctx, bob_doc_id))
    assert "error" in bob_blocked

    # Explicit slug override: Bob's doc found when we target Bob, Erika's not
    bob_ok = json.loads(
        await doc_tools.get_document_by_id(ctx, bob_doc_id, patient_slug=SECOND_SLUG)
    )
    assert bob_ok.get("id") == bob_doc_id

    erika_blocked = json.loads(
        await doc_tools.get_document_by_id(ctx, erika_doc_id, patient_slug=SECOND_SLUG)
    )
    assert "error" in erika_blocked


async def test_search_documents_honors_patient_slug(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)

    # Search all — default scope (Erika) sees only Erika's
    erika_results = json.loads(await doc_tools.search_documents(ctx))
    assert {d["file_id"] for d in erika_results["documents"]} == {"erika_doc1"}

    # Search with Bob slug — Bob's doc only
    bob_results = json.loads(await doc_tools.search_documents(ctx, patient_slug=SECOND_SLUG))
    assert {d["file_id"] for d in bob_results["documents"]} == {"bob_doc1"}


async def test_find_duplicates_scopes_to_slug(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)
    # Default scope: no dup groups within Erika alone (1 doc only)
    erika_dups = json.loads(await doc_tools.find_duplicates(ctx))
    assert erika_dups["total_groups"] == 0

    # With Bob slug: also 0 (one doc), but proves the call routes
    bob_dups = json.loads(await doc_tools.find_duplicates(ctx, patient_slug=SECOND_SLUG))
    assert bob_dups["total_groups"] == 0


async def test_patient_slug_unknown_raises(db: Database):
    ctx = _StubCtx(db)

    # Unknown slug must produce a clear ValueError from _resolve_patient_id,
    # not silently fall back to the ContextVar — that would break the
    # stateless-HTTP isolation guarantee.
    import pytest as _pytest

    with _pytest.raises(ValueError, match="Patient not found"):
        await doc_tools.list_documents(ctx, patient_slug="no-such-patient")
