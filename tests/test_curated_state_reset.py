"""The curated tools' module state is reset between tests (#893, #823)."""

from __future__ import annotations

import ast
import asyncio
import threading
from collections import OrderedDict
from pathlib import Path

from oncofiles.tools import curated as tool
from tests import conftest

SOURCE = Path(tool.__file__)
# module-level state that is not carried from one test to the next by design
NOT_STATE = {
    "_lock",  # asyncio.Lock guarding _runs: holds no data
    "_pages_lock",  # threading.Lock guarding _inflight_pages
    "_stopping",  # cleared by the fixture through .clear()
}


def _module_state() -> set[str]:
    """Private module-level names that hold state: a mutable container, a lock
    or an event, or a plain value some function rebinds with ``global``."""
    tree = ast.parse(SOURCE.read_text())
    rebound = {n for g in ast.walk(tree) if isinstance(g, ast.Global) for n in g.names}
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        else:
            continue
        if not target.startswith("_") or target.isupper():
            continue
        live = getattr(tool, target, None)
        holder = isinstance(live, dict | set | list | OrderedDict | asyncio.Lock | threading.Event)
        if holder or type(live).__name__ == "lock" or target in rebound:
            names.add(target)
    return names


def test_every_module_level_registry_is_reset():
    covered = (
        set(conftest.CURATED_CLEARED) | set(conftest.CURATED_SCALARS)
        | set(conftest.CURATED_COUNTERS) | NOT_STATE
    )  # fmt: skip
    missing = _module_state() - covered
    assert not missing, f"add to the reset lists in tests/conftest.py: {sorted(missing)}"
    assert not (covered - NOT_STATE) - set(dir(tool)), "a reset list names state that is gone"


def test_reset_clears_what_a_test_leaves_behind():
    tool.request_stop()
    tool._runs["p"] = {"status": "running"}
    tool._reocr_jobs["p"] = {"kind": "reocr"}
    tool._dry_runs["reocr"] = 3
    tool._round_no = 7
    conftest.reset_curated_state()
    assert not tool._stopping.is_set() and not tool._runs and not tool._reocr_jobs
    assert not any(tool._dry_runs.values()) and set(tool._dry_runs) == {"reread", "reocr"}
    assert not tool._round_no


def test_dirty_state_part_one():
    tool.request_stop()
    tool._restoring["p"] = {"kind": "restore"}


def test_dirty_state_part_two_starts_clean():
    assert not tool._stopping.is_set() and not tool._restoring
