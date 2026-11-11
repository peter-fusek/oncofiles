"""#717: no cross-patient read of ``activity_log`` is reachable by a patient-scoped caller.

Before the fix two paths returned every patient's activity log to any
authenticated caller:

* the ``files://activity-timeline`` MCP resource (no patient scope, no admin
  check), next to four sibling ``files://`` resources that were dead because
  they omitted the required ``patient_id`` (#551);
* the ``qa_analysis`` tool, which aggregated the whole log without an admin gate.

The resources were removed and ``qa_analysis`` is admin-only. These tests lock
both, plus a sweep: every MCP tool function that reads ``activity_log`` calls
``_resolve_patient_id`` (patient scope) or ``_require_admin_or_raise`` (admin).

Synthetic values only.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oncofiles.database import Database
from oncofiles.models import ActivityLogEntry
from oncofiles.persistent_oauth import _verified_caller_is_admin
from oncofiles.tools.hygiene import qa_analysis

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"
TOOLS_DIR = SRC / "tools"

# A patient other than the caller's; only used as the owner of seeded rows.
OTHER_PATIENT_UUID = "00000000-0000-4000-8000-0000000007c1"

_GATES = frozenset({"_resolve_patient_id", "_require_admin_or_raise"})
_READ_SQL_RE = re.compile(r"\b(?:FROM|JOIN)\s+activity_log\b", re.IGNORECASE)

# Tools that read activity_log without naming it in their own source: query_db
# runs caller-supplied SQL, and `activity_log` is one of its allow-listed tables.
_INDIRECT_READERS = frozenset({"query_db"})


def _ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


@pytest.fixture
def non_admin():
    tok = _verified_caller_is_admin.set(False)
    yield
    _verified_caller_is_admin.reset(tok)


async def _seed_other_patient_errors(db: Database, n: int = 3) -> None:
    for _ in range(n):
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=OTHER_PATIENT_UUID,
                session_id="synthetic-session",
                agent_id="auto",
                tool_name="synthetic_other_patient_tool",
                input_summary="patient_slug=synthetic-other",
                status="error",
                error_message="synthetic failure",
            )
        )


# ── 1. No MCP resources at all ──────────────────────────────────────────────


async def test_no_mcp_resource_is_registered():
    from oncofiles.server import mcp

    assert await mcp.list_resources() == []
    assert await mcp.list_resource_templates() == []


def test_legacy_resources_module_is_gone():
    assert importlib.util.find_spec("oncofiles.resources") is None


def test_no_resource_registration_in_source():
    """Catches a resource registered conditionally or at lifespan time too."""
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if re.search(r"\.(resource|add_resource|add_template)\(", path.read_text("utf-8"))
    ]
    assert not offenders, (
        f"MCP resource registration found in {offenders}. Resources take no patient "
        "argument; expose the data as a tool that calls _resolve_patient_id (#717)."
    )


# ── 2. qa_analysis is admin-only ────────────────────────────────────────────


async def test_qa_analysis_denies_non_admin(db: Database, non_admin):
    await _seed_other_patient_errors(db)

    raw = await qa_analysis(_ctx(db), days=7)
    out = json.loads(raw)

    assert set(out) == {"error"}
    assert "requires admin scope" in out["error"]
    assert "'qa_analysis'" in out["error"]
    assert "synthetic_other_patient_tool" not in raw


@pytest.mark.usefixtures("admin_scope")
async def test_qa_analysis_works_for_admin(db: Database):
    await _seed_other_patient_errors(db)

    out = json.loads(await qa_analysis(_ctx(db), days=7))

    assert "error" not in out
    assert out["summary"]["total_errors"] == 3
    assert any(
        f["type"] == "recurring_error" and f["tool"] == "synthetic_other_patient_tool"
        for f in out["findings"]
    )


# ── 3. Sweep: every activity_log reader is scoped or admin-gated ────────────


def _db_activity_readers() -> set[str]:
    """Database methods whose SQL reads activity_log (derived, not hand-listed)."""
    names: set[str] = set()
    for name, fn in inspect.getmembers(Database, inspect.isfunction):
        try:
            source = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if _READ_SQL_RE.search(source):
            names.add(name)
    return names


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _reads_and_gates(fn: ast.AST, db_readers: set[str]) -> tuple[bool, bool]:
    reads = gated = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in _GATES:
                gated = True
            # Attribute calls only: `db.search_activity_log(...)` is a DB read,
            # while a bare `search_activity_log(...)` would be the tool itself.
            if isinstance(node.func, ast.Attribute) and name in db_readers:
                reads = True
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _READ_SQL_RE.search(node.value)
        ):
            reads = True
    if getattr(fn, "name", None) in _INDIRECT_READERS:
        reads = True
    return reads, gated


def _tool_module_functions():
    """Every module-level function and method in src/oncofiles/tools/."""
    for path in sorted(TOOLS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for top in tree.body:
            nodes = top.body if isinstance(top, ast.ClassDef) else [top]
            for node in nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"{path.relative_to(SRC)}::{node.name}", node


def test_db_reader_detection_is_not_vacuous():
    readers = _db_activity_readers()
    assert {"search_activity_log", "get_activity_stats"} <= readers, readers
    assert "insert_activity_log" not in readers  # writes are not reads


def test_every_activity_log_reader_is_patient_scoped_or_admin_gated():
    db_readers = _db_activity_readers()
    readers: set[str] = set()
    ungated: list[str] = []
    for qualname, fn in _tool_module_functions():
        reads, gated = _reads_and_gates(fn, db_readers)
        if reads:
            readers.add(qualname)
            if not gated:
                ungated.append(qualname)

    # Non-vacuous: the sweep must see the known readers, or it proves nothing.
    expected = {
        "tools/activity.py::search_activity_log",
        "tools/activity.py::get_activity_stats",
        "tools/activity.py::backfill_activity_log_attribution",
        "tools/hygiene.py::qa_analysis",
        "tools/db_query.py::query_db",
    }
    assert expected <= readers, f"sweep missed known readers: {expected - readers}"

    assert not ungated, (
        f"{ungated} read activity_log without _resolve_patient_id or "
        "_require_admin_or_raise. The log holds every patient's tool calls; scope "
        "the read to the resolved patient or gate the tool to admin (#717)."
    )


async def test_every_registered_tool_is_covered_by_the_sweep():
    """The sweep reads src/oncofiles/tools/; a tool defined elsewhere would escape it."""
    from oncofiles.server import mcp

    outside = []
    for tool in await mcp.list_tools():
        fn = inspect.unwrap(tool.fn)
        path = Path(inspect.getsourcefile(fn)).resolve()
        if TOOLS_DIR.resolve() not in path.parents:
            outside.append(f"{tool.name} ({path.name})")
    assert not outside, (
        f"MCP tools defined outside src/oncofiles/tools/: {outside}. Move them there "
        "or extend the activity_log sweep in this file to cover their module."
    )
