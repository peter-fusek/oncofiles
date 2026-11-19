"""#606: MCP tool guidance after the move to self-hosting.

* No tool description and not the server instructions mention Railway.
* Legacy Drive sync tools tell the client that curated folders are skipped
  and point to the curated tools instead.
* A legacy sync that hits a curated folder returns a ``hint``.
* rename_documents_to_standard never touches curated read-model rows.
* No user-surface tool description names an operator tool, Railway or logs.
* curated_folder_status says when its numbers were true (``as_of``) and adds
  the last probe's live count for that patient only.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.database import Database
from tests.helpers import ERIKA_UUID, make_doc

LEGACY_SYNC_TOOLS = ("gdrive_sync", "sync_from_gdrive", "sync_to_gdrive")


@pytest.fixture(scope="module")
def tools_by_name():
    import asyncio

    from oncofiles.server import mcp

    tools = asyncio.run(mcp.list_tools())
    return {t.name: t for t in tools}


def test_no_tool_description_mentions_railway(tools_by_name):
    offenders = [n for n, t in tools_by_name.items() if "railway" in (t.description or "").lower()]
    assert offenders == []


def test_server_instructions_have_no_railway_and_explain_curated():
    from oncofiles.server import _MCP_INSTRUCTIONS

    text = _MCP_INSTRUCTIONS.lower()
    assert "railway" not in text
    # Every connected client sees this text: no hosting-stack details.
    assert "coolify" not in text
    for name in ("curated_index", "curated_folder_status", "curated_folder_run"):
        assert name in _MCP_INSTRUCTIONS
    assert "self-hosted" in text


@pytest.mark.parametrize("name", LEGACY_SYNC_TOOLS)
def test_legacy_sync_descriptions_point_to_curated_tools(tools_by_name, name):
    desc = tools_by_name[name].description or ""
    assert "curated" in desc.lower()
    assert "curated_folder_status" in desc or "curated_index" in desc


@pytest.mark.parametrize("fn", ["sync", "sync_to_gdrive", "sync_from_gdrive"])
async def test_curated_skip_carries_hint(fn):
    from oncofiles import sync as sync_mod

    gdrive = MagicMock()
    gdrive.write_block_reason = "folder synthetic-root is curated (README.md contract, #562)"
    db = MagicMock()
    result = await getattr(sync_mod, fn)(db, MagicMock(), gdrive, "root1", patient_id="p1")
    assert result["skipped"] is True and result["curated"] is True
    assert "curated_index" in result["hint"]
    assert "curated_folder_status" in result["hint"]


async def test_rename_skips_curated_rows(db: Database):
    from oncofiles.tools.naming import rename_documents_to_standard

    await db.insert_document(
        make_doc(file_id="gdrive:synthetic-curated-1", filename="old style scan.pdf"),
        patient_id=ERIKA_UUID,
    )
    await db.insert_document(
        make_doc(file_id="legacy-1", filename="old style legacy.pdf"),
        patient_id=ERIKA_UUID,
    )
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    with patch("oncofiles.tools.naming._get_gdrive", new=AsyncMock(return_value=None)):
        out = json.loads(await rename_documents_to_standard(ctx, dry_run=True))
    assert out["stats"]["curated_skipped"] == 1
    assert all(r["old"] != "old style scan.pdf" for r in out["renames"])


# ── user surface wording ─────────────────────────────────────────────


def test_user_tool_descriptions_do_not_send_users_to_operator_tools_or_logs(tools_by_name):
    import re

    from oncofiles.tool_surface import OPERATOR_TOOLS, USER_TOOLS

    offenders = {}
    for name in sorted(USER_TOOLS):
        desc = tools_by_name[name].description or ""
        hits = [op for op in sorted(OPERATOR_TOOLS) if re.search(rf"\b{op}\b", desc)]
        if re.search(r"\b(server|hosting|container) logs?\b|\bcheck (the )?logs?\b", desc.lower()):
            hits.append("logs")
        if hits:
            offenders[name] = hits
    assert offenders == {}


# ── curated_folder_status: as_of + live probe count ──────────────────


def _status_ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


async def _finished_run(db, pid):
    rid = await db.insert_curated_run(patient_id=pid, dry_run=False, trigger="auto")
    await db.finish_curated_run(
        rid,
        patient_id=pid,
        status="ok",
        message="",
        report={"run_id": "synthetic-run", "not_processed": ["a.pdf", "b.pdf"]},
    )
    return await db.get_curated_run(rid, patient_id=pid)


async def test_status_of_a_finished_run_carries_as_of(db: Database):
    from oncofiles.tools import curated as tool

    run = await _finished_run(db, ERIKA_UUID)
    out = json.loads(await tool.curated_folder_status(_status_ctx(db)))
    assert out["state"] == "done"
    assert out["as_of"] == run["finished_at"] and out["as_of"]
    assert "as_of" in out["as_of_note"]
    assert "live" not in out  # this process has not probed the folder yet


async def test_status_adds_the_live_probe_count_of_this_patient_only(db: Database):
    from oncofiles.tools import curated as tool

    other = "00000000-0000-4000-8000-0000000000aa"
    await _finished_run(db, ERIKA_UUID)
    mine = tool._stats(ERIKA_UUID)
    mine.last_probe_at = "2027-03-04T05:06:07+00:00"
    mine.pending_inbox_count = 3
    mine.runs_blocked["reocr_job"] = 2  # operator counter: must stay out
    theirs = tool._stats(other)
    theirs.last_probe_at = "2027-03-04T05:07:08+00:00"
    theirs.pending_inbox_count = 41
    raw = await tool.curated_folder_status(_status_ctx(db))
    out = json.loads(raw)
    assert set(out["live"]) == {"as_of", "pending_inbox_count", "note"}
    assert out["live"]["as_of"] == "2027-03-04T05:06:07+00:00"
    assert out["live"]["pending_inbox_count"] == 3
    assert other not in raw and "05:07:08" not in raw and "runs_blocked" not in raw


async def test_status_without_a_run_or_while_running_still_shows_live(db: Database):
    from oncofiles.tools import curated as tool

    st = tool._stats(ERIKA_UUID)
    st.last_probe_at = "2027-03-04T05:06:07+00:00"
    st.pending_inbox_count = 0
    out = json.loads(await tool.curated_folder_status(_status_ctx(db)))
    assert out["state"] == "none" and out["live"]["pending_inbox_count"] == 0
    tool._runs[ERIKA_UUID] = {"state": "running", "dry_run": True}
    try:
        out = json.loads(await tool.curated_folder_status(_status_ctx(db)))
    finally:
        tool._runs.pop(ERIKA_UUID, None)
    assert out["state"] == "running" and out["live"]["as_of"].startswith("2027-03-04")


def test_a_probe_without_a_count_is_not_reported_as_live():
    from oncofiles.tools import curated as tool

    tool._stats(ERIKA_UUID).last_probe_at = "2027-03-04T05:06:07+00:00"
    assert tool.live_probe(ERIKA_UUID) is None
    assert tool.live_probe("00000000-0000-4000-8000-0000000000bb") is None


def test_public_heartbeat_has_no_per_patient_counts():
    from oncofiles.tools import curated as tool

    tool._stats(ERIKA_UUID).pending_inbox_count = 7
    assert set(tool.heartbeat()) == {"curated_last_probe_age_s", "curated_loop_suspected"}
