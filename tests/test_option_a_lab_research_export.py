"""Cross-patient isolation tests for Option A rollout on lab_trends, research,
export, and hygiene modules (#429).

Complements test_option_a_documents.py and test_option_a_treatment_conversations.py.
Each lab/research/export/hygiene tool patched in v5.11 should honor
``patient_slug`` over the middleware ContextVar — the stateless-HTTP guarantee.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from oncofiles.database import Database
from oncofiles.models import DocumentCategory, LabValue, ResearchEntry
from oncofiles.tools import clinical as trial_tools
from oncofiles.tools import export as export_tools
from oncofiles.tools import hygiene as hygiene_tools
from oncofiles.tools import lab_trends as lab_tools
from oncofiles.tools import research as research_tools
from tests.conftest import ERIKA_UUID
from tests.helpers import make_doc

# These tests verify slug-routing across patients — admin scope per #497/#498.
pytestmark = pytest.mark.usefixtures("admin_scope")

SECOND_UUID = "00000000-0000-4000-8000-000000000002"
SECOND_SLUG = "bob-test"


async def _seed_two_patients(db: Database) -> tuple[int, int]:
    """Insert Bob + one labs document for each patient. Returns (erika_doc_id, bob_doc_id)."""
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (SECOND_UUID, SECOND_SLUG, "Bob Test", "bob@example.com"),
    )
    await db.db.commit()

    erika_doc = await db.insert_document(
        make_doc(
            file_id="erika_labs",
            filename="20260101_Erika-NOU-Labs.pdf",
            category=DocumentCategory.LABS,
            document_date=date(2026, 1, 1),
        ),
        patient_id=ERIKA_UUID,
    )
    bob_doc = await db.insert_document(
        make_doc(
            file_id="bob_labs",
            filename="20260101_Bob-NOU-Labs.pdf",
            category=DocumentCategory.LABS,
            document_date=date(2026, 1, 1),
        ),
        patient_id=SECOND_UUID,
    )
    return erika_doc.id, bob_doc.id


async def _seed_lab_values(db: Database, erika_doc: int, bob_doc: int) -> None:
    """Insert distinct lab values under each patient's labs document."""
    await db.insert_lab_values(
        [
            LabValue(
                document_id=erika_doc,
                lab_date=date(2026, 1, 1),
                parameter="CEA",
                value=3.5,
                unit="ng/mL",
            ),
            LabValue(
                document_id=erika_doc,
                lab_date=date(2026, 1, 1),
                parameter="WBC",
                value=6.2,
                unit="10^9/L",
            ),
        ]
    )
    await db.insert_lab_values(
        [
            LabValue(
                document_id=bob_doc,
                lab_date=date(2026, 1, 2),
                parameter="CEA",
                value=9.8,  # distinctively-Bob value
                unit="ng/mL",
            ),
            LabValue(
                document_id=bob_doc,
                lab_date=date(2026, 1, 2),
                parameter="PLT",
                value=150,
                unit="10^9/L",
            ),
        ]
    )


class _StubCtx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


# ── lab_trends ────────────────────────────────────────────────────────────


async def test_get_lab_trends_scopes_to_slug(db: Database):
    e_doc, b_doc = await _seed_two_patients(db)
    await _seed_lab_values(db, e_doc, b_doc)
    ctx = _StubCtx(db)

    # Default (Erika) — sees only Erika's CEA=3.5, never Bob's CEA=9.8
    erika_result = json.loads(await lab_tools.get_lab_trends(ctx, parameter="CEA"))
    erika_values = {v["value"] for v in erika_result["values"]}
    assert 3.5 in erika_values
    assert 9.8 not in erika_values

    # Slug override — Bob's CEA=9.8 is visible, Erika's 3.5 is not
    bob_result = json.loads(
        await lab_tools.get_lab_trends(ctx, parameter="CEA", patient_slug=SECOND_SLUG)
    )
    bob_values = {v["value"] for v in bob_result["values"]}
    assert 9.8 in bob_values
    assert 3.5 not in bob_values


async def test_get_lab_summary_scopes_to_slug(db: Database):
    e_doc, b_doc = await _seed_two_patients(db)
    await _seed_lab_values(db, e_doc, b_doc)
    ctx = _StubCtx(db)

    # Erika: sees CEA=3.5 and WBC
    erika = json.loads(await lab_tools.get_lab_summary(ctx))
    erika_params = {p["parameter"] for p in erika.get("parameters", [])}
    assert "CEA" in erika_params
    # CEA latest for Erika is 3.5, NOT Bob's 9.8
    erika_cea = next(p for p in erika["parameters"] if p["parameter"] == "CEA")
    assert erika_cea["value"] == 3.5

    # Bob via slug: CEA latest is 9.8
    bob = json.loads(await lab_tools.get_lab_summary(ctx, patient_slug=SECOND_SLUG))
    bob_cea = next(p for p in bob["parameters"] if p["parameter"] == "CEA")
    assert bob_cea["value"] == 9.8


async def test_compare_lab_panels_scopes_to_slug(db: Database):
    e_doc, b_doc = await _seed_two_patients(db)
    await _seed_lab_values(db, e_doc, b_doc)
    ctx = _StubCtx(db)

    # Bob's lab dates are 2026-01-02. Erika's ContextVar call with Bob's dates
    # should return NO matches for CEA=9.8 — that's Bob's value.
    erika_compare = json.loads(
        await lab_tools.compare_lab_panels(ctx, date_a="2026-01-01", date_b="2026-01-02")
    )
    # Erika has no data on 2026-01-02 so shouldn't see 9.8
    params_blob = json.dumps(erika_compare)
    assert "9.8" not in params_blob

    # Switching to Bob — 9.8 shows up for 2026-01-02
    bob_compare = json.loads(
        await lab_tools.compare_lab_panels(
            ctx, date_a="2026-01-01", date_b="2026-01-02", patient_slug=SECOND_SLUG
        )
    )
    # Bob has CEA=9.8 on 2026-01-02
    assert "9.8" in json.dumps(bob_compare) or any(
        p.get("date_b_value") == 9.8 for p in bob_compare.get("parameters", [])
    )


# ── research ───────────────────────────────────────────────────────────────


async def test_add_and_list_research_respect_slug(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)

    # Add a PubMed entry under Bob via slug, default ContextVar is Erika
    await research_tools.add_research_entry(
        ctx,
        source="pubmed",
        external_id="12345",
        title="Bob's PubMed article",
        patient_slug=SECOND_SLUG,
    )

    # Add another under default (Erika)
    await research_tools.add_research_entry(
        ctx,
        source="pubmed",
        external_id="67890",
        title="Erika's PubMed article",
    )

    # List Erika's: only Erika's article
    erika_list = json.loads(await research_tools.list_research_entries(ctx))
    erika_titles = {e["title"] for e in erika_list["entries"]}
    assert "Erika's PubMed article" in erika_titles
    assert "Bob's PubMed article" not in erika_titles

    # List Bob's via slug
    bob_list = json.loads(await research_tools.list_research_entries(ctx, patient_slug=SECOND_SLUG))
    bob_titles = {e["title"] for e in bob_list["entries"]}
    assert "Bob's PubMed article" in bob_titles
    assert "Erika's PubMed article" not in bob_titles


async def test_search_research_scopes_to_slug(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)

    await db.insert_research_entry(
        ResearchEntry(
            source="pubmed",
            external_id="e-123",
            title="Erika-only trial",
            summary="sensitive content",
        ),
        patient_id=ERIKA_UUID,
    )
    await db.insert_research_entry(
        ResearchEntry(
            source="pubmed",
            external_id="b-456",
            title="Bob-only trial",
            summary="other content",
        ),
        patient_id=SECOND_UUID,
    )

    erika_results = json.loads(await research_tools.search_research(ctx, text="trial"))
    erika_titles = {e["title"] for e in erika_results["entries"]}
    assert "Erika-only trial" in erika_titles
    assert "Bob-only trial" not in erika_titles

    bob_results = json.loads(
        await research_tools.search_research(ctx, text="trial", patient_slug=SECOND_SLUG)
    )
    bob_titles = {e["title"] for e in bob_results["entries"]}
    assert "Bob-only trial" in bob_titles
    assert "Erika-only trial" not in bob_titles


# ── clinical_trials (trial fetch) ─────────────────────────────────────────


async def test_fetch_clinical_trials_stores_under_slug_target(db: Database, monkeypatch):
    """Fetched trials go into research_entries under the slug-resolved patient."""
    await _seed_two_patients(db)
    ctx = _StubCtx(db)

    fake_trials = [
        {
            "nct_id": "NCT99999",
            "title": "Fake trial",
            "status": "RECRUITING",
            "phase": "PHASE2",
        }
    ]
    fake_entry = {
        "source": "clinicaltrials",
        "external_id": "NCT99999",
        "title": "Fake trial",
        "summary": "",
    }
    monkeypatch.setattr("oncofiles.clinical_trials.search_trials", lambda **kw: fake_trials)
    monkeypatch.setattr("oncofiles.clinical_trials.trial_to_research_entry", lambda t: fake_entry)

    # Fetch targeted at Bob via slug
    await trial_tools.fetch_clinical_trials(
        ctx, condition="colorectal cancer", patient_slug=SECOND_SLUG
    )

    # Trial now lives under Bob, not Erika (the ContextVar default)
    bob_trials = await db.list_research_entries(source="clinicaltrials", patient_id=SECOND_UUID)
    erika_trials = await db.list_research_entries(source="clinicaltrials", patient_id=ERIKA_UUID)
    assert any(t.external_id == "NCT99999" for t in bob_trials)
    assert all(t.external_id != "NCT99999" for t in erika_trials)


# ── export ─────────────────────────────────────────────────────────────────


async def test_export_document_package_scopes_to_slug(db: Database):
    e_doc, b_doc = await _seed_two_patients(db)
    ctx = _StubCtx(db)

    erika_pkg = json.loads(await export_tools.export_document_package(ctx, include_timeline=False))
    assert erika_pkg["total_documents"] == 1
    cats = erika_pkg["documents_by_category"]
    flat_files = {d["file_id"] for docs in cats.values() for d in docs}
    assert "erika_labs" in flat_files
    assert "bob_labs" not in flat_files

    bob_pkg = json.loads(
        await export_tools.export_document_package(
            ctx, include_timeline=False, patient_slug=SECOND_SLUG
        )
    )
    assert bob_pkg["total_documents"] == 1
    cats = bob_pkg["documents_by_category"]
    flat_files = {d["file_id"] for docs in cats.values() for d in docs}
    assert "bob_labs" in flat_files
    assert "erika_labs" not in flat_files


# ── hygiene ────────────────────────────────────────────────────────────────


async def test_get_pipeline_status_scopes_to_slug(db: Database):
    e_doc, b_doc = await _seed_two_patients(db)
    # Add a second doc under Bob so counts differ
    await db.insert_document(
        make_doc(
            file_id="bob_labs2",
            filename="20260201_Bob-NOU-Labs2.pdf",
            category=DocumentCategory.LABS,
        ),
        patient_id=SECOND_UUID,
    )
    ctx = _StubCtx(db)

    # Pipeline stage counts must reflect the resolved patient, not the whole DB.
    erika = json.loads(await hygiene_tools.get_pipeline_status(ctx))
    assert erika["pipeline_stages"]["total_documents"] == 1

    bob = json.loads(await hygiene_tools.get_pipeline_status(ctx, patient_slug=SECOND_SLUG))
    assert bob["pipeline_stages"]["total_documents"] == 2


async def test_get_document_status_matrix_scopes_to_slug(db: Database):
    e_doc, b_doc = await _seed_two_patients(db)
    ctx = _StubCtx(db)

    erika = json.loads(await hygiene_tools.get_document_status_matrix(ctx))
    bob = json.loads(await hygiene_tools.get_document_status_matrix(ctx, patient_slug=SECOND_SLUG))

    erika_files = {d.get("filename", "") for d in erika.get("documents", [])}
    bob_files = {d.get("filename", "") for d in bob.get("documents", [])}

    assert any("Erika" in f for f in erika_files)
    assert not any("Bob" in f for f in erika_files)
    assert any("Bob" in f for f in bob_files)
    assert not any("Erika" in f for f in bob_files)
