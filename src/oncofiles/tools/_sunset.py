"""Oncoteam sunset (#721): deprecation of the Oncoteam storage tools.

Oncoteam was the agent layer that stored its working memory in Oncofiles
through 13 MCP tools (agent state, conversation diary, research entries,
activity log). Oncoteam is stopped, so these tools are deprecated:

* every one of them carries ``ONCOTEAM_SUNSET_PREFIX`` at the start of its
  description, so MCP clients see the deprecation in the tool list;
* the four WRITE tools refuse new writes (nothing is stored);
* the READ tools keep working, so the existing rows stay readable as an
  archive until the tools are removed.

No rows are deleted or migrated by this module. The tables stay as they are
until infra has exported them to the private archive (see #721).

Internal writers are not affected: the audit middleware still appends to
``activity_log`` and the weekly analytics job still writes its own
``agent_state`` row through the DB layer.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

ONCOTEAM_SUNSET_ISSUE = "#721"
ONCOTEAM_SUNSET_REMOVAL_DATE = "2027-01-10"
ONCOTEAM_SUNSET_PREFIX = (
    f"[DEPRECATED — Oncoteam sunset ({ONCOTEAM_SUNSET_ISSUE}); "
    f"removed after {ONCOTEAM_SUNSET_REMOVAL_DATE}]"
)

# Tool name → the read tools that still serve its data.
ONCOTEAM_SUNSET_WRITE_TOOLS: dict[str, tuple[str, ...]] = {
    "set_agent_state": ("get_agent_state", "list_agent_states"),
    "log_conversation": (
        "search_conversations",
        "get_conversation",
        "get_conversation_stats",
    ),
    "add_research_entry": ("search_research", "list_research_entries"),
    "add_activity_log": ("search_activity_log", "get_activity_stats"),
}

ONCOTEAM_SUNSET_READ_TOOLS: frozenset[str] = frozenset(
    name for reads in ONCOTEAM_SUNSET_WRITE_TOOLS.values() for name in reads
)

ONCOTEAM_SUNSET_TOOLS: frozenset[str] = (
    frozenset(ONCOTEAM_SUNSET_WRITE_TOOLS) | ONCOTEAM_SUNSET_READ_TOOLS
)


def write_refused(tool_name: str) -> str:
    """JSON refusal returned by a deprecated write tool. Stores nothing."""
    reads = ONCOTEAM_SUNSET_WRITE_TOOLS[tool_name]
    logger.warning("Refused deprecated write tool %s (%s)", tool_name, ONCOTEAM_SUNSET_ISSUE)
    return json.dumps(
        {
            "error": "deprecated",
            "tool": tool_name,
            "stored": False,
            "message": (
                f"{tool_name} no longer accepts new writes: Oncoteam was retired "
                f"({ONCOTEAM_SUNSET_ISSUE}). Nothing was stored. Existing entries stay "
                f"readable as an archive via {', '.join(reads)} until "
                f"{ONCOTEAM_SUNSET_REMOVAL_DATE}."
            ),
            "issue": ONCOTEAM_SUNSET_ISSUE,
            "removal_after": ONCOTEAM_SUNSET_REMOVAL_DATE,
        }
    )
