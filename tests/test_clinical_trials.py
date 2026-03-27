"""Tests for ClinicalTrials.gov API integration."""

import json
from unittest.mock import MagicMock, patch

import pytest

from oncofiles.clinical_trials import parse_trial, search_trials, trial_to_research_entry

# Sample API v2 response structure
SAMPLE_STUDY = {
    "protocolSection": {
        "identificationModule": {
            "nctId": "NCT12345678",
            "briefTitle": "FOLFOX + Bevacizumab in mCRC",
            "officialTitle": "A Phase III Study of FOLFOX Plus Bevacizumab in mCRC",
        },
        "statusModule": {
            "overallStatus": "RECRUITING",
            "startDateStruct": {"date": "2025-01-15"},
            "completionDateStruct": {"date": "2027-12-31"},
        },
        "designModule": {
            "phases": ["PHASE3"],
            "enrollmentInfo": {"count": 500},
        },
        "descriptionModule": {
            "briefSummary": "This study evaluates FOLFOX plus bevacizumab in mCRC patients."
        },
        "armsInterventionsModule": {
            "interventions": [
                {"name": "FOLFOX", "type": "DRUG"},
                {"name": "Bevacizumab", "type": "DRUG"},
            ]
        },
        "eligibilityModule": {"eligibilityCriteria": "Age >= 18, ECOG 0-1, measurable disease"},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "National Cancer Institute"}},
        "contactsLocationsModule": {
            "locations": [
                {"facility": "NCI", "city": "Bethesda", "country": "United States"},
            ]
        },
        "conditionsModule": {"conditions": ["Colorectal Cancer", "Metastatic Colorectal Cancer"]},
    }
}


class TestParseTrial:
    """Tests for parsing ClinicalTrials.gov API responses."""

    def test_parse_full_trial(self):
        result = parse_trial(SAMPLE_STUDY)
        assert result["nct_id"] == "NCT12345678"
        assert result["title"] == "FOLFOX + Bevacizumab in mCRC"
        assert result["status"] == "RECRUITING"
        assert result["phase"] == "PHASE3"
        assert result["enrollment"] == 500
        assert len(result["interventions"]) == 2
        assert result["interventions"][0]["name"] == "FOLFOX"
        assert result["sponsor"] == "National Cancer Institute"
        assert len(result["locations"]) == 1
        assert result["locations"][0]["country"] == "United States"

    def test_parse_minimal_trial(self):
        minimal = {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT99999999"},
            }
        }
        result = parse_trial(minimal)
        assert result["nct_id"] == "NCT99999999"
        assert result["title"] == ""
        assert result["phase"] == ""
        assert result["interventions"] == []
        assert result["locations"] == []

    def test_parse_trial_missing_phases(self):
        study = {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT11111111"},
                "designModule": {},
            }
        }
        result = parse_trial(study)
        assert result["phase"] == ""


class TestTrialToResearchEntry:
    """Tests for converting trials to research entry format."""

    def test_full_conversion(self):
        trial = parse_trial(SAMPLE_STUDY)
        entry = trial_to_research_entry(trial)
        assert entry["source"] == "clinicaltrials"
        assert entry["external_id"] == "NCT12345678"
        assert entry["title"] == "FOLFOX + Bevacizumab in mCRC"
        assert "FOLFOX" in entry["summary"]
        assert "Bevacizumab" in entry["summary"]
        tags = json.loads(entry["tags"])
        assert "Colorectal Cancer" in tags
        assert "phase3" in tags
        assert "recruiting" in tags

    def test_raw_data_is_json(self):
        trial = parse_trial(SAMPLE_STUDY)
        entry = trial_to_research_entry(trial)
        raw = json.loads(entry["raw_data"])
        assert raw["nct_id"] == "NCT12345678"


class TestSearchTrials:
    """Tests for the search_trials function with mocked HTTP."""

    @patch("oncofiles.clinical_trials.httpx.Client")
    def test_search_returns_parsed_trials(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"studies": [SAMPLE_STUDY]}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        results = search_trials("colorectal cancer", keywords="FOLFOX")
        assert len(results) == 1
        assert results[0]["nct_id"] == "NCT12345678"

    @patch("oncofiles.clinical_trials.httpx.Client")
    def test_search_empty_results(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"studies": []}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        results = search_trials("rare condition")
        assert results == []


class TestClinicalTrialsDB:
    """Tests for storing clinical trial data in research_entries."""

    @pytest.mark.asyncio
    async def test_store_trial_as_research_entry(self, db):
        from oncofiles.models import ResearchEntry

        trial = parse_trial(SAMPLE_STUDY)
        entry_data = trial_to_research_entry(trial)
        entry = ResearchEntry(**entry_data)
        saved = await db.insert_research_entry(entry, patient_id="erika")

        assert saved.id is not None
        assert saved.source == "clinicaltrials"
        assert saved.external_id == "NCT12345678"

    @pytest.mark.asyncio
    async def test_deduplication_by_nct_id(self, db):
        from oncofiles.models import ResearchEntry

        trial = parse_trial(SAMPLE_STUDY)
        entry_data = trial_to_research_entry(trial)

        entry1 = ResearchEntry(**entry_data)
        saved1 = await db.insert_research_entry(entry1, patient_id="erika")

        entry2 = ResearchEntry(**entry_data)
        saved2 = await db.insert_research_entry(entry2, patient_id="erika")

        # Should return same entry (deduplicated)
        assert saved1.id == saved2.id
