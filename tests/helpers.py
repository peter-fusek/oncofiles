"""Shared test helpers."""

from datetime import date

from erika_files_mcp.models import (
    ActivityLogEntry,
    AgentState,
    Document,
    DocumentCategory,
    ResearchEntry,
    TreatmentEvent,
)


def make_doc(**overrides) -> Document:
    defaults = {
        "file_id": "file_test123",
        "filename": "20240115_NOUonko_labs_krvnyObraz.pdf",
        "original_filename": "20240115_NOUonko_labs_krvnyObraz.pdf",
        "document_date": date(2024, 1, 15),
        "institution": "NOUonko",
        "category": DocumentCategory.LABS,
        "description": "krvnyObraz",
        "mime_type": "application/pdf",
        "size_bytes": 1024,
    }
    defaults.update(overrides)
    return Document(**defaults)


def make_agent_state(**overrides) -> AgentState:
    defaults = {
        "agent_id": "oncoteam",
        "key": "test_key",
        "value": '{"hello": "world"}',
    }
    defaults.update(overrides)
    return AgentState(**defaults)


def make_treatment_event(**overrides) -> TreatmentEvent:
    defaults = {
        "event_date": date(2025, 3, 1),
        "event_type": "chemo",
        "title": "FOLFOX cycle 3",
        "notes": "Started cycle 3 of FOLFOX regimen.",
        "metadata": "{}",
    }
    defaults.update(overrides)
    return TreatmentEvent(**defaults)


def make_research_entry(**overrides) -> ResearchEntry:
    defaults = {
        "source": "pubmed",
        "external_id": "PMID12345",
        "title": "FOLFOX efficacy in mCRC",
        "summary": "A meta-analysis of FOLFOX regimen efficacy.",
        "tags": '["FOLFOX","mCRC"]',
        "raw_data": "",
    }
    defaults.update(overrides)
    return ResearchEntry(**defaults)


def make_activity_log(**overrides) -> ActivityLogEntry:
    defaults = {
        "session_id": "sess-001",
        "agent_id": "oncoteam",
        "tool_name": "search_pubmed",
        "input_summary": "query=FOLFOX mCRC",
        "output_summary": "Found 5 articles",
        "duration_ms": 1200,
        "status": "ok",
        "tags": "[]",
    }
    defaults.update(overrides)
    return ActivityLogEntry(**defaults)
