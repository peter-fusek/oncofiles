"""Weekly usage summary (scheduler job ``weekly_analytics``, #542).

A system-wide aggregate of counts only: no patient, no document content.
It is stored in ``agent_state`` under ``SYSTEM_NO_PATIENT``.

The job body lives here, at module level, so a test can run it against a
real database. Nothing in it catches exceptions: a failure reaches
APScheduler's ``EVENT_JOB_ERROR`` listener, which records ``last_error`` on
the job tracker (operator ``/readiness``). The earlier version caught
everything, so four attribute names that never existed made the job fail on
every run while it was reported as executed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from oncofiles.constants import SYSTEM_NO_PATIENT
from oncofiles.database._analytics import PipelineStats, PromptStats, ToolUsageStats
from oncofiles.models import AgentState

logger = logging.getLogger(__name__)

AGENT_ID = "oncofiles"
STATE_KEY = "analytics_weekly_summary"
TOP_TOOLS = 3


def build_summary(
    prompt_stats: PromptStats,
    tool_stats: ToolUsageStats,
    pipeline_stats: PipelineStats,
    *,
    now: datetime | None = None,
) -> dict:
    """The summary dict from the three stats objects (pure, no I/O)."""
    when = now or datetime.now(UTC)
    total_syncs = pipeline_stats.total_syncs
    return {
        "week_ending": when.strftime("%Y-%m-%d"),
        "prompts": {
            "total_calls": prompt_stats.total_calls,
            "total_input_tokens": prompt_stats.total_input_tokens,
            "total_output_tokens": prompt_stats.total_output_tokens,
            "error_rate": prompt_stats.error_rate,
        },
        "tools": {
            "unique_tools": tool_stats.unique_tools,
            "total_calls": tool_stats.total_calls,
            # top_tools rows are dicts: {"tool", "count", ...}
            "top_3": [
                {"name": t["tool"], "calls": t["count"]} for t in tool_stats.top_tools[:TOP_TOOLS]
            ],
        },
        "pipeline": {
            "total_syncs": total_syncs,
            "success_rate": (
                round(pipeline_stats.successful_syncs / total_syncs, 2) if total_syncs > 0 else 0
            ),
            "docs_imported": pipeline_stats.total_docs_imported,
        },
    }


async def run_weekly_analytics(db) -> dict:
    """Aggregate the last week's usage and persist it. Raises on any failure."""
    # Sequential: the single DB connection must not see concurrent queries.
    prompt_stats = await db.get_prompt_stats(days=7)
    tool_stats = await db.get_tool_usage_stats(days=7)
    pipeline_stats = await db.get_pipeline_stats()
    summary = build_summary(prompt_stats, tool_stats, pipeline_stats)
    await db.set_agent_state(
        AgentState(
            agent_id=AGENT_ID,
            key=STATE_KEY,
            value=json.dumps(summary),
            # system aggregate, not a patient's row (#791)
            patient_id=SYSTEM_NO_PATIENT,
        )
    )
    logger.info("Weekly analytics: %s", json.dumps(summary, indent=None)[:200])
    return summary
