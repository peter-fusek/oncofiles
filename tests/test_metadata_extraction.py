"""Tests for structured metadata extraction."""

import json
from unittest.mock import MagicMock, patch

import pytest

from oncofiles.enhance import extract_structured_metadata


class TestExtractStructuredMetadata:
    """Tests for the extract_structured_metadata function."""

    def test_empty_text_returns_defaults(self):
        result = extract_structured_metadata("")
        assert result["document_type"] == "other"
        assert result["findings"] == []
        assert result["diagnoses"] == []
        assert result["medications"] == []
        assert result["dates_mentioned"] == []
        assert result["providers"] == []
        assert result["plain_summary"] == ""

    def test_whitespace_only_returns_defaults(self):
        result = extract_structured_metadata("   \n  ")
        assert result["document_type"] == "other"
        assert result["findings"] == []

    @patch("oncofiles.enhance.anthropic")
    def test_successful_extraction(self, mock_anthropic):
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "document_type": "lab_report",
                        "findings": ["Elevated WBC", "Low hemoglobin"],
                        "diagnoses": [{"name": "Anemia", "icd_code": "D64.9"}],
                        "medications": ["FOLFOX", "[MEDICATION_REDACTED]"],
                        "dates_mentioned": ["2026-02-27"],
                        "providers": ["NOU Bratislava"],
                        "plain_summary": "Lab results show anemia. WBC elevated.",
                    }
                )
            )
        ]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic.Anthropic.return_value = mock_client

        result = extract_structured_metadata("WBC 12.5 HGB 95 PLT 250...")
        assert result["document_type"] == "lab_report"
        assert len(result["findings"]) == 2
        assert result["diagnoses"][0]["name"] == "Anemia"
        assert "FOLFOX" in result["medications"]
        assert result["providers"] == ["NOU Bratislava"]

    @patch("oncofiles.enhance.anthropic")
    def test_malformed_response_returns_defaults(self, mock_anthropic):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="not valid json")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic.Anthropic.return_value = mock_client

        result = extract_structured_metadata("some text")
        assert result["document_type"] == "other"
        assert result["findings"] == []

    @patch("oncofiles.enhance.anthropic")
    def test_partial_response_fills_defaults(self, mock_anthropic):
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "document_type": "imaging",
                        "findings": ["Liver metastases stable"],
                    }
                )
            )
        ]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic.Anthropic.return_value = mock_client

        result = extract_structured_metadata("CT scan report...")
        assert result["document_type"] == "imaging"
        assert result["findings"] == ["Liver metastases stable"]
        assert result["medications"] == []
        assert result["plain_summary"] == ""


class TestStructuredMetadataDB:
    """Tests for structured_metadata database operations."""

    @pytest.mark.asyncio
    async def test_update_and_read_structured_metadata(self, db):
        from tests.helpers import make_doc

        doc = await db.insert_document(make_doc())
        metadata = json.dumps({"document_type": "lab_report", "findings": ["WBC elevated"]})
        await db.update_structured_metadata(doc.id, metadata)

        updated = await db.get_document(doc.id)
        assert updated.structured_metadata is not None
        parsed = json.loads(updated.structured_metadata)
        assert parsed["document_type"] == "lab_report"
        assert "WBC elevated" in parsed["findings"]

    @pytest.mark.asyncio
    async def test_structured_metadata_null_by_default(self, db):
        from tests.helpers import make_doc

        doc = await db.insert_document(make_doc())
        fetched = await db.get_document(doc.id)
        assert fetched.structured_metadata is None
