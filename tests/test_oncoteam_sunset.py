"""Lock tests for the Oncoteam sunset (#721).

* The 13 Oncoteam storage tools stay registered, each with the deprecation
  prefix at the start of its description.
* The four WRITE tools refuse and store nothing; the READ tools still serve
  the archive rows.
* The Oncoteam integration points are gone: the webhook module and its config,
  the oncoteam.cloud CORS origin, the dashboard link, and the "oncoteam"
  client name of the static bearer.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oncofiles import config
from oncofiles.database import Database
from oncofiles.models import (
    ActivityLogEntry,
    AgentState,
    ConversationEntry,
    ResearchEntry,
)
from oncofiles.tools import activity, agent_state, conversations, research
from oncofiles.tools._sunset import (
    ONCOTEAM_SUNSET_PREFIX,
    ONCOTEAM_SUNSET_READ_TOOLS,
    ONCOTEAM_SUNSET_TOOLS,
    ONCOTEAM_SUNSET_WRITE_TOOLS,
)
from tests.helpers import ERIKA_UUID

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"

EXPECTED_WRITES = {"set_agent_state", "log_conversation", "add_research_entry", "add_activity_log"}
EXPECTED_READS = {
    "get_agent_state",
    "list_agent_states",
    "search_conversations",
    "get_conversation",
    "get_conversation_stats",
    "search_research",
    "list_research_entries",
    "search_activity_log",
    "get_activity_stats",
}


def _ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    ctx.session_id = None
    return ctx


def test_sunset_tool_set_is_exactly_the_13_oncoteam_tools():
    assert set(ONCOTEAM_SUNSET_WRITE_TOOLS) == EXPECTED_WRITES
    assert ONCOTEAM_SUNSET_READ_TOOLS == EXPECTED_READS
    assert len(ONCOTEAM_SUNSET_TOOLS) == 13


def test_prefix_text():
    assert ONCOTEAM_SUNSET_PREFIX == (
        "[DEPRECATED — Oncoteam sunset (#721); removed after 2027-01-10]"
    )


async def test_every_sunset_tool_is_registered_with_the_prefix():
    from oncofiles.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    missing = ONCOTEAM_SUNSET_TOOLS - set(tools)
    assert not missing, f"deprecated tools must stay registered until removal: {missing}"
    for name in ONCOTEAM_SUNSET_TOOLS:
        assert (tools[name].description or "").startswith(ONCOTEAM_SUNSET_PREFIX), name


async def test_no_other_tool_carries_the_prefix():
    from oncofiles.server import mcp

    tagged = {
        t.name
        for t in await mcp.list_tools()
        if (t.description or "").startswith(ONCOTEAM_SUNSET_PREFIX)
    }
    assert tagged == ONCOTEAM_SUNSET_TOOLS


async def _row_counts(db: Database) -> dict[str, int]:
    counts = {}
    for table in ("agent_state", "conversation_entries", "research_entries", "activity_log"):
        async with db.db.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
            row = await cur.fetchone()
        counts[table] = row[0]
    return counts


@pytest.mark.parametrize(
    "call",
    [
        lambda ctx: agent_state.set_agent_state(ctx, key="k", value='"v"'),
        lambda ctx: conversations.log_conversation(ctx, title="t", content="c"),
        lambda ctx: research.add_research_entry(ctx, source="pubmed", external_id="1", title="t"),
        lambda ctx: activity.add_activity_log(
            ctx, session_id="s", agent_id="a", tool_name="search"
        ),
    ],
    ids=sorted(EXPECTED_WRITES),
)
async def test_write_tools_refuse_and_store_nothing(db: Database, call):
    before = await _row_counts(db)
    out = json.loads(await call(_ctx(db)))
    assert out["error"] == "deprecated"
    assert out["stored"] is False
    assert out["issue"] == "#721"
    assert out["removal_after"] == "2027-01-10"
    assert await _row_counts(db) == before


async def test_read_tools_still_serve_the_archive(db: Database):
    """Rows written before the sunset stay readable (seeded via the DB layer)."""
    await db.set_agent_state(
        AgentState(agent_id="oncoteam", key="memo", value='"x"', patient_id=ERIKA_UUID)
    )
    await db.insert_conversation_entry(
        ConversationEntry(
            entry_date=date(2027, 1, 5),
            title="Archived note",
            content="Synthetic body.",
            source="live",
        ),
        patient_id=ERIKA_UUID,
    )
    await db.insert_research_entry(
        ResearchEntry(source="pubmed", external_id="900001", title="Synthetic article"),
        patient_id=ERIKA_UUID,
    )
    await db.insert_activity_log(
        ActivityLogEntry(
            session_id="s-archive", agent_id="oncoteam", tool_name="search", patient_id=ERIKA_UUID
        )
    )
    ctx = _ctx(db)

    assert json.loads(await agent_state.get_agent_state(ctx, key="memo"))["value"] == '"x"'
    assert [s["key"] for s in json.loads(await agent_state.list_agent_states(ctx))] == ["memo"]
    assert json.loads(await conversations.search_conversations(ctx, text="Archived"))["total"] == 1
    assert json.loads(await research.list_research_entries(ctx))["total"] == 1
    assert json.loads(await research.search_research(ctx, text="Synthetic"))["total"] == 1
    found = json.loads(await activity.search_activity_log(ctx, session_id="s-archive"))
    assert found, "activity_log archive must stay readable"


def test_webhook_module_and_config_removed():
    assert importlib.util.find_spec("oncofiles.webhook") is None
    assert not hasattr(config, "ONCOTEAM_WEBHOOK_URL")
    assert not hasattr(config, "ONCOTEAM_WEBHOOK_TOKEN")
    for path in SRC.rglob("*.py"):
        assert "notify_oncoteam" not in path.read_text(), path


def test_oncoteam_cloud_not_allowed_or_linked():
    for name in ("server.py", "dashboard.js", "dashboard.html"):
        path = SRC / name
        if path.exists():
            assert "oncoteam.cloud" not in path.read_text(), name
