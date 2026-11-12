"""#606: MCP tool guidance after the move to self-hosting.

* No tool description and not the server instructions mention Railway.
* Legacy Drive sync tools tell the client that curated folders are skipped
  and point to the curated tools instead.
* A legacy sync that hits a curated folder returns a ``hint``.
* rename_documents_to_standard never touches curated read-model rows.
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
