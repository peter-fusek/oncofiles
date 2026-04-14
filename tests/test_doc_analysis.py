"""Tests for AI document analysis module (splitting, consolidation, relationships)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from oncofiles.doc_analysis import (
    analyze_consolidation,
    analyze_document_composition,
    analyze_document_relationships,
)
from tests.helpers import make_doc

# ── analyze_document_composition ─────────────────────────────────────────────


def test_composition_empty_text():
    """Empty text returns empty list."""
    result = analyze_document_composition("")
    assert result == []


def test_composition_single_document():
    """Single document detected — returns list with one element."""
    ai_response = json.dumps(
        {
            "document_count": 1,
            "documents": [
                {
                    "page_range": [1, 3],
                    "document_date": "2026-01-15",
                    "institution": "NOU",
                    "category": "labs",
                    "description": "BloodResults",
                    "confidence": 0.95,
                    "reasoning": "Single lab report",
                }
            ],
        }
    )

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=ai_response)]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)

    with patch("oncofiles.doc_analysis.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
        result = analyze_document_composition("Lab results: WBC 5.2...")

    assert len(result) == 1
    assert result[0]["category"] == "labs"


def test_composition_multi_document():
    """Multiple documents detected — returns list with multiple elements."""
    ai_response = json.dumps(
        {
            "document_count": 2,
            "documents": [
                {
                    "page_range": [1, 2],
                    "document_date": "2026-01-15",
                    "institution": "NOU",
                    "category": "labs",
                    "description": "BloodResults",
                    "confidence": 0.92,
                    "reasoning": "Lab report on pages 1-2",
                },
                {
                    "page_range": [3, 5],
                    "document_date": "2026-01-20",
                    "institution": "NOU",
                    "category": "discharge",
                    "description": "DischargeSummary",
                    "confidence": 0.88,
                    "reasoning": "Discharge summary on pages 3-5",
                },
            ],
        }
    )

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=ai_response)]
    mock_response.usage = MagicMock(input_tokens=200, output_tokens=100)

    with patch("oncofiles.doc_analysis.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
        result = analyze_document_composition("Page 1: Lab results... Page 3: Discharge...")

    assert len(result) == 2
    assert result[0]["category"] == "labs"
    assert result[1]["category"] == "discharge"


def test_composition_invalid_json():
    """Invalid JSON from AI returns empty list."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="not valid json")]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=10)

    with patch("oncofiles.doc_analysis.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
        result = analyze_document_composition("Some text")

    assert result == []


# ── analyze_consolidation ────────────────────────────────────────────────────


def test_consolidation_too_few_docs():
    """Less than 2 documents returns empty list."""
    doc = make_doc(id=1)
    result = analyze_consolidation([(doc, "some text")])
    assert result == []


def test_consolidation_finds_group():
    """AI identifies a group of related documents."""
    ai_response = json.dumps(
        {
            "groups": [
                {
                    "document_ids": [1, 2],
                    "reasoning": "Pathology report split across two PDFs",
                    "confidence": 0.9,
                }
            ]
        }
    )

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=ai_response)]
    mock_response.usage = MagicMock(input_tokens=500, output_tokens=100)

    doc1 = make_doc(id=1, file_id="f1", filename="pathology_p1.pdf")
    doc2 = make_doc(id=2, file_id="f2", filename="pathology_p2.pdf")

    with patch("oncofiles.doc_analysis.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
        result = analyze_consolidation([(doc1, "Page 1..."), (doc2, "Page 2...")])

    assert len(result) == 1
    assert result[0]["document_ids"] == [1, 2]
    assert result[0]["confidence"] == 0.9


def test_consolidation_no_groups():
    """AI finds no documents to consolidate."""
    ai_response = json.dumps({"groups": []})

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=ai_response)]
    mock_response.usage = MagicMock(input_tokens=500, output_tokens=20)

    doc1 = make_doc(id=1, file_id="f1")
    doc2 = make_doc(id=2, file_id="f2")

    with patch("oncofiles.doc_analysis.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
        result = analyze_consolidation([(doc1, "text1"), (doc2, "text2")])

    assert result == []


# ── analyze_document_relationships ───────────────────────────────────────────


def test_relationships_no_candidates():
    """No candidates returns empty list."""
    result = analyze_document_relationships("doc text", 1, [])
    assert result == []


def test_relationships_found():
    """AI identifies relationships between documents."""
    ai_response = json.dumps(
        {
            "relationships": [
                {
                    "target_id": 2,
                    "relationship": "same_visit",
                    "confidence": 0.95,
                    "reasoning": "Same date, same institution, same visit",
                },
                {
                    "target_id": 3,
                    "relationship": "follow_up",
                    "confidence": 0.8,
                    "reasoning": "Follow-up labs after treatment",
                },
            ]
        }
    )

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=ai_response)]
    mock_response.usage = MagicMock(input_tokens=300, output_tokens=80)

    candidates = [
        {"id": 2, "filename": "labs.pdf", "category": "labs", "ai_summary": "Blood count"},
        {"id": 3, "filename": "followup.pdf", "category": "labs", "ai_summary": "Follow-up"},
    ]

    with patch("oncofiles.doc_analysis.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
        result = analyze_document_relationships("WBC 5.2...", 1, candidates)

    assert len(result) == 2
    assert result[0]["relationship"] == "same_visit"
    assert result[1]["relationship"] == "follow_up"
