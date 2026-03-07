"""ClinicalTrials.gov API v2 client for trial data retrieval."""

from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://clinicaltrials.gov/api/v2/studies"


def search_trials(
    condition: str,
    keywords: str | None = None,
    status: str = "RECRUITING",
    location_country: str | None = None,
    phase: str | None = None,
    page_size: int = 20,
) -> list[dict]:
    """Search ClinicalTrials.gov API v2 for studies.

    Args:
        condition: Medical condition (e.g. "colorectal cancer").
        keywords: Additional search terms (e.g. "FOLFOX").
        status: Trial status filter (RECRUITING, ACTIVE_NOT_RECRUITING, etc.).
        location_country: Country filter (e.g. "United States").
        phase: Phase filter (e.g. "PHASE3").
        page_size: Max results to return.

    Returns:
        List of parsed trial dicts.
    """
    params: dict[str, str | int] = {
        "format": "json",
        "pageSize": page_size,
    }

    # Build query
    query_parts = [f"AREA[Condition]{condition}"]
    if keywords:
        query_parts.append(keywords)
    params["query.term"] = " AND ".join(query_parts)

    if status:
        params["filter.overallStatus"] = status
    if phase:
        params["filter.phase"] = phase

    # Location filter via query
    if location_country:
        params["query.locn"] = f"AREA[LocationCountry]{location_country}"

    params["fields"] = (
        "NCTId,BriefTitle,OfficialTitle,OverallStatus,Phase,"
        "StartDate,CompletionDate,EnrollmentCount,"
        "BriefSummary,InterventionName,InterventionType,"
        "EligibilityCriteria,LeadSponsorName,"
        "LocationFacility,LocationCity,LocationCountry,"
        "Condition,Keyword"
    )

    with httpx.Client(timeout=30) as client:
        resp = client.get(API_BASE, params=params)
        resp.raise_for_status()
        data = resp.json()

    studies = data.get("studies", [])
    return [parse_trial(s) for s in studies]


def parse_trial(study: dict) -> dict:
    """Extract structured data from a ClinicalTrials.gov API v2 study response."""
    proto = study.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    status_mod = proto.get("statusModule", {})
    design = proto.get("designModule", {})
    desc = proto.get("descriptionModule", {})
    arms = proto.get("armsInterventionsModule", {})
    elig = proto.get("eligibilityModule", {})
    sponsor = proto.get("sponsorCollaboratorsModule", {})
    contacts = proto.get("contactsLocationsModule", {})

    # Extract interventions
    interventions = []
    for interv in arms.get("interventions", []):
        interventions.append(
            {
                "name": interv.get("name", ""),
                "type": interv.get("type", ""),
            }
        )

    # Extract locations
    locations = []
    for loc in contacts.get("locations", [])[:5]:
        locations.append(
            {
                "facility": loc.get("facility", ""),
                "city": loc.get("city", ""),
                "country": loc.get("country", ""),
            }
        )

    # Extract conditions
    conditions = proto.get("conditionsModule", {}).get("conditions", [])

    return {
        "nct_id": ident.get("nctId", ""),
        "title": ident.get("briefTitle", ""),
        "official_title": ident.get("officialTitle", ""),
        "status": status_mod.get("overallStatus", ""),
        "phase": (design.get("phases") or [""])[0] if design.get("phases") else "",
        "start_date": status_mod.get("startDateStruct", {}).get("date", ""),
        "completion_date": status_mod.get("completionDateStruct", {}).get("date", ""),
        "enrollment": design.get("enrollmentInfo", {}).get("count"),
        "summary": desc.get("briefSummary", ""),
        "interventions": interventions,
        "eligibility": elig.get("eligibilityCriteria", ""),
        "sponsor": sponsor.get("leadSponsor", {}).get("name", ""),
        "locations": locations,
        "conditions": conditions,
    }


def trial_to_research_entry(trial: dict) -> dict:
    """Convert a parsed trial dict to ResearchEntry field values."""
    summary_parts = [trial["summary"][:500]]
    if trial["interventions"]:
        interv_names = ", ".join(i["name"] for i in trial["interventions"])
        summary_parts.append(f"Interventions: {interv_names}")
    if trial["phase"]:
        summary_parts.append(f"Phase: {trial['phase']}")
    if trial["sponsor"]:
        summary_parts.append(f"Sponsor: {trial['sponsor']}")

    tags = list(trial.get("conditions", []))[:5]
    if trial["phase"]:
        tags.append(trial["phase"].lower().replace(" ", "_"))
    tags.append(trial["status"].lower())

    return {
        "source": "clinicaltrials",
        "external_id": trial["nct_id"],
        "title": trial["title"],
        "summary": " | ".join(summary_parts),
        "tags": json.dumps(tags),
        "raw_data": json.dumps(trial),
    }
