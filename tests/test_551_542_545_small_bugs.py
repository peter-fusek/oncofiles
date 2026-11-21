"""Regression tests for three small old defects.

#551  the two legacy backfill scripts called ``list_documents`` without the
      required ``patient_id``. The scripts were removed in #930; the AST sweep
      below keeps every ``list_documents`` call in src/ patient-scoped.
#542  the weekly analytics job read four attributes that never existed and
      swallowed the AttributeError, so it never wrote its summary.
#545  the ``repair_gdrive_folder_hygiene`` docstring said the smallest
      duplicate folder is kept; the code keeps the largest.

All values are invented.
"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import fields
from pathlib import Path

import pytest

from oncofiles.constants import SYSTEM_NO_PATIENT
from oncofiles.database._analytics import PipelineStats, PromptStats, ToolUsageStats
from oncofiles.models import ActivityLogEntry
from tests.helpers import PRIMARY_TEST_UUID

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"


# ── #551: every list_documents call names a patient ─────────────────────


def test_every_list_documents_call_names_a_patient():
    """AST sweep of src/: no ``<x>.list_documents(...)`` without ``patient_id=``.

    The MCP tool of the same name is a plain function (``ast.Name``), so only
    attribute calls (the DB helper) are checked.
    """
    missing: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "list_documents"
                and not any(kw.arg == "patient_id" or kw.arg is None for kw in node.keywords)
            ):
                missing.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not missing, f"list_documents called without patient_id: {missing}"


# ── #542: weekly analytics ───────────────────────────────────────────────


async def test_weekly_analytics_runs_on_a_real_db_and_persists(db):
    from oncofiles.weekly_analytics import AGENT_ID, STATE_KEY, TOP_TOOLS, run_weekly_analytics

    calls = {
        "search_documents": 3,
        "timeline": 2,
        "fact_series": 1,
        "whoami": 1,
    }
    for tool, n in calls.items():
        for i in range(n):
            await db.insert_activity_log(
                ActivityLogEntry(
                    patient_id=PRIMARY_TEST_UUID,
                    session_id=f"s-{tool}-{i}",
                    agent_id="test",
                    tool_name=tool,
                )
            )
    sync_id = await db.insert_sync_history(patient_id=PRIMARY_TEST_UUID)
    await db.db.execute(
        "UPDATE sync_history SET status = 'completed', from_gdrive_new = 4 WHERE id = ?",
        (sync_id,),
    )
    await db.db.commit()

    summary = await run_weekly_analytics(db)

    assert summary["tools"]["unique_tools"] == 4
    assert summary["tools"]["total_calls"] == 7
    top = summary["tools"]["top_3"]
    assert top[0] == {"name": "search_documents", "calls": calls["search_documents"]}
    assert len(top) == TOP_TOOLS
    assert summary["pipeline"]["total_syncs"] == 1
    assert summary["pipeline"]["success_rate"] == 1.0
    assert (
        summary["pipeline"]["docs_imported"] == (await db.get_pipeline_stats()).total_docs_imported
    )

    stored = await db.get_agent_state(STATE_KEY, AGENT_ID, patient_id=SYSTEM_NO_PATIENT)
    assert stored is not None
    assert json.loads(stored.value) == summary
    # A system aggregate is never stored under a patient.
    assert await db.get_agent_state(STATE_KEY, AGENT_ID, patient_id=PRIMARY_TEST_UUID) is None


async def test_weekly_analytics_on_an_empty_db(db):
    from oncofiles.weekly_analytics import run_weekly_analytics

    summary = await run_weekly_analytics(db)
    assert set(summary["pipeline"]) == {"total_syncs", "success_rate", "docs_imported"}
    assert not any(summary["pipeline"].values())
    assert set(summary["tools"]) == {"unique_tools", "total_calls", "top_3"}
    assert not any(summary["tools"].values())


async def test_weekly_analytics_failure_is_not_swallowed(db, monkeypatch):
    """A broken job must raise, so EVENT_JOB_ERROR records it (#552 rule)."""
    from oncofiles.weekly_analytics import AGENT_ID, STATE_KEY, run_weekly_analytics

    async def _boom(*a, **k):
        raise RuntimeError("stats unavailable")

    monkeypatch.setattr(db, "get_pipeline_stats", _boom)
    with pytest.raises(RuntimeError):
        await run_weekly_analytics(db)
    assert await db.get_agent_state(STATE_KEY, AGENT_ID, patient_id=SYSTEM_NO_PATIENT) is None


def test_weekly_analytics_reads_only_fields_that_exist():
    """Field-name drift lock: every attribute read from a stats object exists."""
    import oncofiles.weekly_analytics as mod

    known = {
        "prompt_stats": {f.name for f in fields(PromptStats)},
        "tool_stats": {f.name for f in fields(ToolUsageStats)},
        "pipeline_stats": {f.name for f in fields(PipelineStats)},
    }
    read: dict[str, set[str]] = {k: set() for k in known}
    for node in ast.walk(ast.parse(inspect.getsource(mod))):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in known
        ):
            read[node.value.id].add(node.attr)
    for name, attrs in read.items():
        assert attrs, f"{name} is never read"
        assert attrs <= known[name], f"{name}: unknown fields {attrs - known[name]}"


def test_scheduler_job_delegates_without_a_catch_all():
    from oncofiles.server import _start_sync_scheduler

    source = inspect.getsource(_start_sync_scheduler)
    body = source.split("async def _run_weekly_analytics")[1].split("scheduler.add_job")[0]
    assert "run_weekly_analytics(db)" in body
    assert "except" not in body.split('"""')[2]


# ── #545: docstring matches the code ─────────────────────────────────────


def test_repair_hygiene_docstring_says_largest_folder_is_kept():
    from oncofiles.tools import hygiene

    fn = hygiene.repair_gdrive_folder_hygiene
    fn = getattr(fn, "fn", fn)
    doc = " ".join((fn.__doc__ or "").split())
    assert "keep the LARGEST (most direct children) folder" in doc
    assert "smallest (fewest-children)" not in doc
    assert "keeps the OLDEST" in doc  # the contrast with find_or_create_folder

    # ... and the code still does what the docstring says.
    source = inspect.getsource(fn)
    assert "counts.sort(key=lambda t: len(t[1]))" in source
    assert "winners = counts[-1][0]" in source
