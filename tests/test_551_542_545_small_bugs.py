"""Regression tests for three small old defects.

#551  the two legacy backfill scripts called ``list_documents`` without the
      required ``patient_id`` (TypeError before doing anything). They now take
      an explicit patient and read only that patient's documents.
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
from oncofiles.database import Database
from oncofiles.database._analytics import PipelineStats, PromptStats, ToolUsageStats
from oncofiles.models import ActivityLogEntry
from tests.helpers import ERIKA_UUID, make_doc

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"

SECOND_UUID = "00000000-0000-4000-8000-0000000000b2"
SECOND_SLUG = "bob-test"
FIRST_SLUG = "jana-test"
MODIFIED = "2029-11-23T17:41:00Z"


async def _seed_two_patients(db: Database) -> None:
    """Two patients; two documents for the first, one for the second."""
    await db.db.execute(
        "UPDATE patients SET slug = ? WHERE patient_id = ?", (FIRST_SLUG, ERIKA_UUID)
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
        (SECOND_UUID, SECOND_SLUG, "Bob Test"),
    )
    await db.db.commit()
    for file_id, name, pid in (
        ("first_a", "20270301_JanaTest_Clinic_Labs_BloodCount.pdf", ERIKA_UUID),
        ("first_b", "20270302_JanaTest_Clinic_Report_Visit.pdf", ERIKA_UUID),
        ("second_a", "20270303_BobTest_Clinic_Labs_BloodCount.pdf", SECOND_UUID),
    ):
        await db.insert_document(
            make_doc(file_id=file_id, filename=name, original_filename=name),
            patient_id=pid,
        )


# ── #551: legacy scripts are patient-scoped ──────────────────────────────


class _FakeDrive:
    """Lists one Drive file per seeded document (all three patients' names)."""

    def __init__(self) -> None:
        self.listed: list[str] = []

    def list_folder(self, folder_id: str) -> list[dict]:
        self.listed.append(folder_id)
        return [
            {
                "id": "drv-1",
                "name": "20270301_JanaTest_Clinic_Labs_BloodCount.pdf",
                "modifiedTime": MODIFIED,
            },
            {
                "id": "drv-2",
                "name": "20270303_BobTest_Clinic_Labs_BloodCount.pdf",
                "modifiedTime": MODIFIED,
            },
        ]


async def _gdrive_ids(db: Database) -> dict[str, str | None]:
    async with db.db.execute("SELECT file_id, gdrive_id FROM documents") as cur:
        return {r["file_id"]: r["gdrive_id"] for r in await cur.fetchall()}


async def test_backfill_gdrive_ids_touches_only_the_named_patient(db, monkeypatch):
    from oncofiles import backfill_gdrive_ids as script

    await _seed_two_patients(db)
    drive = _FakeDrive()
    monkeypatch.setattr(script, "create_gdrive_client", lambda: drive)

    stats = await script.backfill(patient=SECOND_SLUG, folder_id="folder-x", db=db)

    # Only Bob's one document was considered: the first patient's file with a
    # matching name in the same Drive listing is not matched.
    assert stats == {
        "total_gdrive": 2,
        "matched": 1,
        "already_set": 0,
        "not_found": 0,
    }
    assert await _gdrive_ids(db) == {"first_a": None, "first_b": None, "second_a": "drv-2"}


async def test_backfill_gdrive_ids_dry_run_writes_nothing(db, monkeypatch):
    from oncofiles import backfill_gdrive_ids as script

    await _seed_two_patients(db)
    monkeypatch.setattr(script, "create_gdrive_client", lambda: _FakeDrive())

    stats = await script.backfill(patient=ERIKA_UUID, dry_run=True, folder_id="folder-x", db=db)

    assert (stats["matched"], stats["not_found"]) == (1, 1)
    assert set((await _gdrive_ids(db)).values()) == {None}


async def test_backfill_gdrive_ids_unknown_patient_stops_before_drive(db, monkeypatch):
    from oncofiles import backfill_gdrive_ids as script

    await _seed_two_patients(db)
    drive = _FakeDrive()
    monkeypatch.setattr(script, "create_gdrive_client", lambda: drive)

    for bad in ("no-such-patient", "   ", ""):
        with pytest.raises(SystemExit):
            await script.backfill(patient=bad, folder_id="folder-x", db=db)
    assert drive.listed == []
    assert set((await _gdrive_ids(db)).values()) == {None}


class _NoCloudFiles:
    """Stands in for FilesClient; a dry run must never download."""

    def download(self, file_id: str):  # pragma: no cover - must not be reached
        raise AssertionError("dry run downloaded a file")


async def test_backfill_ocr_reads_only_the_named_patient(db, monkeypatch):
    from oncofiles import backfill_ocr as script

    await _seed_two_patients(db)
    # A curated / Drive-only row of the same patient: local model only.
    name = "20270304_BobTest_Clinic_Report_Visit.pdf"
    await db.insert_document(
        make_doc(file_id="gdrive:drv-9", filename=name, original_filename=name),
        patient_id=SECOND_UUID,
    )
    monkeypatch.setattr(script, "files_api_refusal_reason", lambda: None)
    monkeypatch.setattr(script, "FilesClient", _NoCloudFiles)
    monkeypatch.setattr(script, "GOOGLE_DRIVE_FOLDER_ID", "")

    stats = await script.backfill(patient=SECOND_SLUG, dry_run=True, db=db)
    assert stats == {
        "total": 2,
        "skipped": 0,
        "local_only": 1,
        "processed": 1,
        "errors": 0,
    }

    stats = await script.backfill(patient=FIRST_SLUG, dry_run=True, db=db)
    assert stats == {
        "total": 2,
        "skipped": 0,
        "local_only": 0,
        "processed": 2,
        "errors": 0,
    }


async def test_backfill_ocr_refuses_without_patient_or_with_cloud_off(db, monkeypatch):
    from oncofiles import backfill_ocr as script

    await _seed_two_patients(db)

    def _never(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("a client was built after a refusal")

    monkeypatch.setattr(script, "FilesClient", _never)

    monkeypatch.setattr(script, "files_api_refusal_reason", lambda: None)
    for bad in ("no-such-patient", ""):
        with pytest.raises(SystemExit):
            await script.backfill(patient=bad, dry_run=True, db=db)

    # Cloud AI off (the production default): refuse even for a real patient.
    monkeypatch.setattr(script, "files_api_refusal_reason", lambda: "cloud AI is off")
    with pytest.raises(SystemExit):
        await script.backfill(patient=SECOND_SLUG, dry_run=True, db=db)


@pytest.mark.parametrize("module", ["backfill_gdrive_ids", "backfill_ocr"])
def test_backfill_scripts_require_a_patient(module, monkeypatch):
    import importlib

    script = importlib.import_module(f"oncofiles.{module}")

    # The function cannot be called without naming a patient ...
    param = inspect.signature(script.backfill).parameters["patient"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty

    # ... and neither can the command line.
    monkeypatch.setattr("sys.argv", [module, "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 2


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
                    patient_id=ERIKA_UUID,
                    session_id=f"s-{tool}-{i}",
                    agent_id="test",
                    tool_name=tool,
                )
            )
    sync_id = await db.insert_sync_history(patient_id=ERIKA_UUID)
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
    assert await db.get_agent_state(STATE_KEY, AGENT_ID, patient_id=ERIKA_UUID) is None


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
