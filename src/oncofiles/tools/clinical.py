"""Clinical trials tool."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import ResearchEntry
from oncofiles.tools._helpers import _get_db


async def fetch_clinical_trials(
    ctx: Context,
    condition: str,
    keywords: str | None = None,
    status: str = "RECRUITING",
    location_country: str | None = None,
    phase: str | None = None,
    limit: int = 20,
) -> str:
    """Fetch clinical trials from ClinicalTrials.gov and store in research_entries.

    Searches the ClinicalTrials.gov API v2 for matching studies and saves
    them to the research_entries table (deduplicates by NCT number).

    Args:
        condition: Medical condition to search for (e.g. "colorectal cancer").
        keywords: Additional search terms (e.g. "FOLFOX", "immunotherapy").
        status: Trial status filter (RECRUITING, ACTIVE_NOT_RECRUITING, COMPLETED).
        location_country: Country filter (e.g. "United States", "Slovakia").
        phase: Phase filter (PHASE1, PHASE2, PHASE3, PHASE4).
        limit: Maximum number of trials to fetch (default 20).
    """
    from oncofiles.clinical_trials import search_trials, trial_to_research_entry

    try:
        trials = search_trials(
            condition=condition,
            keywords=keywords,
            status=status,
            location_country=location_country,
            phase=phase,
            page_size=limit,
        )
    except Exception as e:
        return json.dumps({"error": f"ClinicalTrials.gov API error: {e}"})

    db = _get_db(ctx)
    stored = []
    for trial in trials:
        entry_data = trial_to_research_entry(trial)
        entry = ResearchEntry(**entry_data)
        saved = await db.insert_research_entry(entry)
        stored.append(
            {
                "id": saved.id,
                "nct_id": trial["nct_id"],
                "title": trial["title"],
                "status": trial["status"],
                "phase": trial["phase"],
            }
        )

    return json.dumps(
        {
            "fetched": len(trials),
            "stored": len(stored),
            "trials": stored,
        }
    )


def register(mcp):
    mcp.tool()(fetch_clinical_trials)
