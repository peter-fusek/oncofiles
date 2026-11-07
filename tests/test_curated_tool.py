"""MCP tool wiring for the curated pipeline (#562)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import config
from oncofiles.curated.runner import RunReport
from oncofiles.tools import curated as tool


@pytest.fixture(autouse=True)
def _reset():
    tool._runs.clear()
    yield
    tool._runs.clear()


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "m")


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


async def test_requires_local_llm(db, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "")
    out = json.loads(await tool.curated_folder_run(_ctx(db)))
    assert "local LLM not configured" in out["error"]


async def test_requires_google(db, llm_on):
    with patch("oncofiles.server._create_patient_clients", AsyncMock(return_value=None)):
        out = json.loads(await tool.curated_folder_run(_ctx(db)))
    assert "not connected" in out["error"]


@pytest.mark.parametrize(
    "reason", [None, "lies inside curated folder X", "contains curated folder X"]
)
async def test_refuses_non_root_folders(db, llm_on, reason):
    clients = (MagicMock(), None, None, "folder1")
    with (
        patch("oncofiles.server._create_patient_clients", AsyncMock(return_value=clients)),
        patch("oncofiles.curated_folder.curated_reason", return_value=reason),
    ):
        out = json.loads(await tool.curated_folder_run(_ctx(db)))
    assert "not a curated root" in out["error"]


async def test_dry_run_runs_in_background_and_reports(db, llm_on):
    gdrive = MagicMock()
    clients = (gdrive, None, None, "folder1")
    report = RunReport(
        run_id="r1", dry_run=True, documents=[{"source": "a.pdf", "result": "filed"}]
    )
    with (
        patch("oncofiles.server._create_patient_clients", AsyncMock(return_value=clients)),
        patch("oncofiles.curated_folder.curated_reason", return_value="is a curated folder"),
        patch("oncofiles.curated.runner.run_curated", return_value=report) as run,
    ):
        out = json.loads(await tool.curated_folder_run(_ctx(db)))
        assert out == {"status": "started", "dry_run": True, "max_docs": 20}
        for _ in range(50):
            status = json.loads(await tool.curated_folder_status(_ctx(db)))
            if status["state"] != "running":
                break
            await asyncio.sleep(0.01)
    assert status["state"] == "done" and status["report"]["run_id"] == "r1"
    assert run.call_args.args[2].dry_run is True
    gdrive.contract_writes.assert_not_called()  # a dry run never lifts the block (review #1)


async def test_one_run_at_a_time(db, llm_on):
    from tests.helpers import ERIKA_UUID

    tool._runs[ERIKA_UUID] = {"state": "running"}
    out = json.loads(await tool.curated_folder_run(_ctx(db)))
    assert "already in progress" in out["error"]
