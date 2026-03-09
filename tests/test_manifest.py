"""Tests for manifest export, render, and parse roundtrip."""

from __future__ import annotations

from datetime import date

from oncofiles.database import Database
from oncofiles.manifest import (
    export_manifest,
    group_conversations_by_month,
    parse_manifest,
    render_conversation_month,
    render_manifest_json,
    render_research_library,
    render_treatment_timeline,
)
from oncofiles.models import ConversationEntry, ResearchEntry, TreatmentEvent
from tests.helpers import make_doc, make_research_entry, make_treatment_event

# ── Manifest export/parse roundtrip ──────────────────────────────────────


async def test_export_manifest_empty(db: Database):
    """Empty DB produces valid manifest."""
    manifest = await export_manifest(db)
    assert manifest["version"] == "1.0"
    assert manifest["documents"] == []
    assert manifest["conversation_entries"] == []
    assert manifest["treatment_events"] == []
    assert manifest["research_entries"] == []


async def test_export_manifest_with_data(db: Database):
    """Manifest includes all data types."""
    await db.insert_document(make_doc())
    await db.insert_treatment_event(make_treatment_event())
    await db.insert_research_entry(make_research_entry())

    manifest = await export_manifest(db)
    assert len(manifest["documents"]) == 1
    assert len(manifest["treatment_events"]) == 1
    assert len(manifest["research_entries"]) == 1


async def test_manifest_roundtrip(db: Database):
    """Manifest can be serialized and parsed back."""
    await db.insert_document(make_doc())
    manifest = await export_manifest(db)
    json_str = render_manifest_json(manifest)
    parsed = parse_manifest(json_str)

    assert parsed["version"] == manifest["version"]
    assert len(parsed["documents"]) == len(manifest["documents"])
    assert parsed["documents"][0]["filename"] == manifest["documents"][0]["filename"]


async def test_manifest_includes_structured_metadata(db: Database):
    """Manifest export includes structured_metadata for documents that have it."""
    doc = make_doc()
    doc = await db.insert_document(doc)
    metadata_json = '{"diagnoses": ["CRC"], "medications": ["FOLFOX"]}'
    await db.update_structured_metadata(doc.id, metadata_json)

    manifest = await export_manifest(db)
    assert len(manifest["documents"]) == 1
    assert manifest["documents"][0]["structured_metadata"] == metadata_json


async def test_manifest_structured_metadata_none_when_absent(db: Database):
    """Manifest export has null structured_metadata when not set."""
    await db.insert_document(make_doc())

    manifest = await export_manifest(db)
    assert manifest["documents"][0]["structured_metadata"] is None


# ── Conversation rendering ──────────────────────────────────────────────


def test_render_conversation_month_empty():
    assert render_conversation_month([]) == ""


def test_render_conversation_month():
    entries = [
        ConversationEntry(
            entry_date=date(2026, 3, 1),
            entry_type="summary",
            title="Chemo cycle 2 summary",
            content="Treatment went well.",
            participant="claude.ai",
            tags=["chemo", "cycle-2"],
        ),
    ]
    result = render_conversation_month(entries)
    assert "---" in result
    assert "date: 2026-03-01" in result
    assert "type: summary" in result
    assert "## Chemo cycle 2 summary" in result
    assert "Treatment went well." in result


def test_group_conversations_by_month():
    entries = [
        ConversationEntry(entry_date=date(2026, 2, 15), entry_type="note", title="A", content="a"),
        ConversationEntry(entry_date=date(2026, 3, 1), entry_type="note", title="B", content="b"),
        ConversationEntry(entry_date=date(2026, 3, 20), entry_type="note", title="C", content="c"),
    ]
    by_month = group_conversations_by_month(entries)
    assert "2026-02" in by_month
    assert "2026-03" in by_month
    assert len(by_month["2026-02"]) == 1
    assert len(by_month["2026-03"]) == 2


# ── Treatment timeline rendering ────────────────────────────────────────


def test_render_treatment_timeline_empty():
    result = render_treatment_timeline([])
    assert "No treatment events" in result


def test_render_treatment_timeline():
    events = [
        TreatmentEvent(
            event_date=date(2026, 1, 15),
            event_type="chemo",
            title="FOLFOX cycle 1",
            notes="First cycle started.",
        ),
        TreatmentEvent(
            event_date=date(2026, 2, 5),
            event_type="chemo",
            title="FOLFOX cycle 2",
            notes="",
        ),
    ]
    result = render_treatment_timeline(events)
    assert "# Treatment Timeline" in result
    assert "## 2026-01-15" in result
    assert "### [chemo] FOLFOX cycle 1" in result
    assert "First cycle started." in result
    assert "## 2026-02-05" in result


# ── Research library rendering ──────────────────────────────────────────


def test_render_research_library_empty():
    result = render_research_library([])
    assert "No research entries" in result


def test_render_research_library():
    entries = [
        ResearchEntry(
            source="pubmed",
            external_id="PMID12345",
            title="FOLFOX efficacy",
            summary="A meta-analysis.",
            tags='["FOLFOX", "mCRC"]',
        ),
        ResearchEntry(
            source="clinicaltrials",
            external_id="NCT001",
            title="Phase III trial",
            summary="",
            tags="[]",
        ),
    ]
    result = render_research_library(entries)
    assert "# Research Library" in result
    assert "## pubmed" in result
    assert "### FOLFOX efficacy" in result
    assert "FOLFOX, mCRC" in result
    assert "## clinicaltrials" in result


# ── Bilingual rendering (#56, #58) ──────────────────────────────────────


def test_render_treatment_timeline_sk():
    """Slovak version uses translated header."""
    result = render_treatment_timeline([], lang="sk")
    assert "# Priebeh liečby" in result
    assert "Žiadne zaznamenané" in result


def test_render_treatment_timeline_en_default():
    """Default (EN) version uses English header."""
    result = render_treatment_timeline([])
    assert "# Treatment Timeline" in result


def test_render_research_library_sk():
    """Slovak version uses translated header and tags label."""
    entries = [
        ResearchEntry(
            source="pubmed",
            external_id="PMID12345",
            title="Test",
            summary="Summary",
            tags='["tag1"]',
        ),
    ]
    result = render_research_library(entries, lang="sk")
    assert "# Výskumná knižnica" in result
    assert "Štítky:" in result


def test_render_research_library_empty_sk():
    result = render_research_library([], lang="sk")
    assert "Žiadne uložené" in result
