"""Lock: nothing deletes or trashes user data on its own (#734).

Maintainer decision 2026-11-11: the 30-day trash purge and every other
background / scheduled / side-effect deletion of user data is removed, not
flagged off. Deleting is something a user asks for through a tool.

Each lock has an allowlist with a one-line reason per entry. The allowlists may
only shrink: an entry whose code is gone fails ``test_*_no_stale_entries``, so
removing a deletion path also removes its permission.

1. SQL: every ``DELETE FROM`` in ``src/oncofiles`` is an allowlisted
   (file, table) pair, and core patient tables are never hard-deleted.
2. Drive / Files API: every deletion call site (``trash_file``,
   ``trash_canary``, ``.delete(``, ``body={"trashed": True}``) is an
   allowlisted (file, function) pair.
3. Document trash: every call that moves a document to the Oncofiles trash
   (``delete_document`` / ``delete_document_by_file_id``) is an allowlisted
   (file, function) pair.
4. Scheduler: no APScheduler job in ``server.py``, nor any ``server.py``
   function it calls, references a deletion helper or hard-deletes a table
   outside the technical allowlist.

Note for PR #732 (#723): it adds a similar hard-DELETE allowlist. Whichever of
the two merges second drops the entries the other one made stale.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"

# SQL keyword in any case; table names are lower snake_case, which skips prose
# such as "# Delete from Files API".
DELETE_RE = re.compile(r"(?i:DELETE\s+FROM)\s+([a-z_][a-z0-9_]*)\b")

# ── 1. SQL hard deletes ──────────────────────────────────────────────────

# (file relative to src/oncofiles, table) -> reason. May only shrink.
SQL_DELETE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("server.py", "mcp_oauth_tokens"): "oauth_token_cleanup job: expired OAuth tokens (credential)",
    ("persistent_oauth.py", "mcp_oauth_tokens"): "revoked / rotated OAuth token (credential)",
    ("session_revocation.py", "session_revocations"): "expired logout denylist rows (auth state)",
    ("server.py", "prompt_log"): "prompt_log_cleanup: 90-day AI telemetry retention (#734 open)",
    (
        "database/_documents.py",
        "document_pages",
    ): "delete_ocr_pages: OCR cache reset right before it is rewritten from the current source",
    (
        "database/_clinical.py",
        "treatment_events",
    ): "user-invoked delete_treatment_event tool; becomes a soft delete with #723",
    (
        "database/_conversations.py",
        "conversation_entries",
    ): "delete_conversation_entry helper, no automatic caller; becomes a soft delete with #723",
}

# Tables that hold the patient's documents and facts: never hard-deleted.
NEVER_HARD_DELETED = {"documents", "lab_values", "clinical_records", "patients"}


def _sql_hard_deletes() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        for m in DELETE_RE.finditer(path.read_text(encoding="utf-8")):
            found.add((path.relative_to(SRC).as_posix(), m.group(1)))
    return found


def test_sql_hard_delete_only_on_allowlist():
    unexpected = _sql_hard_deletes() - set(SQL_DELETE_ALLOWLIST)
    assert not unexpected, (
        "hard DELETE outside the allowlist — user data is soft-deleted on a user's "
        f"request only (#734): {sorted(unexpected)}"
    )


def test_sql_allowlist_no_stale_entries():
    stale = set(SQL_DELETE_ALLOWLIST) - _sql_hard_deletes()
    assert not stale, f"allowlist entries no longer in the code, remove them: {sorted(stale)}"


def test_core_patient_tables_never_hard_deleted():
    tables = {table for _, table in _sql_hard_deletes()}
    assert not tables & NEVER_HARD_DELETED, sorted(tables & NEVER_HARD_DELETED)


# ── 2 + 3. Drive / Files API deletes and document trash calls ────────────

# (file, enclosing function) -> reason. May only shrink.
DRIVE_DELETE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("gdrive_client.py", "GDriveClient.trash_file"): "Drive trash primitive; callers listed here",
    ("curated/drive_ops.py", "trash_canary"): "curated primitive: trashes only a run's own canary",
    (
        "curated/runner.py",
        "Runner._clear_stale_canary",
    ): "trashes a dead run's canary journal (technical file it created), never a user file",
    ("curated/runner.py", "Runner._write_state"): "a finished run trashes its own canary",
    ("files_api.py", "FilesClient.delete"): "Files API delete primitive; only caller is below",
    (
        "tools/documents.py",
        "delete_document",
    ): "user-invoked delete_document tool: drops the Files API copy, Drive original untouched",
    (
        "tools/hygiene.py",
        "reconcile_gdrive",
    ): "user-invoked tool, dry_run=True by default: trashes legacy/backup folders on request",
    (
        "tools/hygiene.py",
        "repair_gdrive_folder_hygiene",
    ): "user-invoked tool, dry_run=True by default: trashes a folder after all children moved",
}

DOCUMENT_TRASH_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "split.py",
        "split_document",
    ): "split replaces the original row with its parts (Drive original untouched) and rolls "
    "back its own parts on failure; both stay restorable",
    (
        "tools/documents.py",
        "upload_document",
    ): "user-invoked upload that replaces an existing document of the same name",
    ("tools/documents.py", "delete_document"): "user-invoked delete_document tool",
    (
        "tools/curated.py",
        "_index_patient",
    ): "curated read model: hides the mirror row of a file that left the filed folders; "
    "Drive is the source and the row returns when the file does",
}

_DRIVE_TRASH_ATTRS = {"trash_file", "trash_canary", "trash", "emptyTrash", "batchDelete"}
_DOC_TRASH_ATTRS = {"delete_document", "delete_document_by_file_id"}


class _CallSites(ast.NodeVisitor):
    """Collect (file, qualified enclosing function) for deletion call sites."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.stack: list[str] = []
        self.drive: set[tuple[str, str]] = set()
        self.doc_trash: set[tuple[str, str]] = set()

    def _scope(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope(node)

    def _here(self) -> tuple[str, str]:
        return (self.rel, ".".join(self.stack) or "<module>")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Attribute, not Call: catches asyncio.to_thread(gdrive.trash_file, ...) too.
        if node.attr in _DRIVE_TRASH_ATTRS:
            self.drive.add(self._here())
        if node.attr in _DOC_TRASH_ATTRS:
            self.doc_trash.add(self._here())
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "delete":
            self.drive.add(self._here())  # Drive / Files API / Calendar .delete(...)
        for kw in node.keywords:
            if kw.arg == "body" and isinstance(kw.value, ast.Dict):
                for key, value in zip(kw.value.keys, kw.value.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "trashed"
                        and isinstance(value, ast.Constant)
                        and value.value is True
                    ):
                        self.drive.add(self._here())
        self.generic_visit(node)


def _call_sites() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    drive: set[tuple[str, str]] = set()
    doc_trash: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        visitor = _CallSites(path.relative_to(SRC).as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        drive |= visitor.drive
        doc_trash |= visitor.doc_trash
    return drive, doc_trash


def test_drive_deletes_only_on_allowlist():
    unexpected = _call_sites()[0] - set(DRIVE_DELETE_ALLOWLIST)
    assert not unexpected, (
        "Drive / Files API delete or trash outside the allowlist — nothing trashes "
        f"user files on its own (#734): {sorted(unexpected)}"
    )


def test_drive_allowlist_no_stale_entries():
    stale = set(DRIVE_DELETE_ALLOWLIST) - _call_sites()[0]
    assert not stale, f"allowlist entries no longer in the code, remove them: {sorted(stale)}"


def test_document_trash_only_on_allowlist():
    unexpected = _call_sites()[1] - set(DOCUMENT_TRASH_ALLOWLIST)
    assert not unexpected, (
        "document moved to trash outside the allowlist — no automatic dedup or "
        f"auto-trash (#734): {sorted(unexpected)}"
    )


def test_document_trash_allowlist_no_stale_entries():
    stale = set(DOCUMENT_TRASH_ALLOWLIST) - _call_sites()[1]
    assert not stale, f"allowlist entries no longer in the code, remove them: {sorted(stale)}"


# ── 4. Scheduled jobs in server.py ───────────────────────────────────────

# Tables a scheduled job may hard-delete from: credentials and telemetry only.
JOB_SQL_ALLOWLIST = {"mcp_oauth_tokens", "prompt_log", "session_revocations"}

FORBIDDEN_IN_JOBS = (
    _DRIVE_TRASH_ATTRS
    | _DOC_TRASH_ATTRS
    | {
        "purge_expired_trash",
        "delete_ocr_pages",
        "delete_treatment_event",
        "delete_conversation_entry",
        "delete_clinical_record",
        "soft_delete_hard_fact",
        "reconcile_gdrive",
        "repair_gdrive_folder_hygiene",
    }
)

REMOVED_JOB_IDS = {"trash_cleanup", "empty_folder_cleanup"}


def _server_tree() -> ast.Module:
    return ast.parse((SRC / "server.py").read_text(encoding="utf-8"))


def _add_job_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_job"
    ]


def _scheduled_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for call in _add_job_calls(tree):
        target = call.args[0]
        if isinstance(target, ast.Name):
            roots.add(target.id)
        elif isinstance(target, ast.Lambda):
            roots |= {n.id for n in ast.walk(target.body) if isinstance(n, ast.Name)}
        else:  # pragma: no cover - make an unparseable job loud, not invisible
            raise AssertionError(f"unrecognised add_job target: {ast.dump(target)}")
    return roots


def _reachable_job_functions(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """Scheduled job functions plus every server.py function they reference."""
    by_name: dict[str, list[ast.AST]] = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            by_name[node.name].append(node)
    reached: dict[str, list[ast.AST]] = {}
    todo = list(_scheduled_roots(tree))
    while todo:
        name = todo.pop()
        if name in reached or name not in by_name:
            continue
        reached[name] = by_name[name]
        for fn in by_name[name]:
            todo.extend(n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id in by_name)
    return reached


def test_scheduler_roots_are_found():
    """Guard the guard: the walker must see the real job functions."""
    roots = _scheduled_roots(_server_tree())
    assert {"_run_sync", "_run_oauth_token_cleanup", "_startup_catchup"} <= roots


def test_removed_jobs_are_not_registered():
    ids = set()
    for call in _add_job_calls(_server_tree()):
        for kw in call.keywords:
            if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                ids.add(kw.value.value)
    assert "oauth_token_cleanup" in ids  # the walker sees job ids at all
    assert not ids & REMOVED_JOB_IDS, sorted(ids & REMOVED_JOB_IDS)
    server_text = (SRC / "server.py").read_text(encoding="utf-8")
    for job_id in REMOVED_JOB_IDS:  # also gone from the /readiness + dashboard job list
        assert f'"{job_id}"' not in server_text, job_id


def test_scheduled_jobs_never_delete_user_data():
    offenders: list[str] = []
    for name, fns in _reachable_job_functions(_server_tree()).items():
        for fn in fns:
            for node in ast.walk(fn):
                ref = None
                if isinstance(node, ast.Attribute):
                    ref = node.attr
                elif isinstance(node, ast.Name):
                    ref = node.id
                if ref in FORBIDDEN_IN_JOBS:
                    offenders.append(f"{name}: {ref}")
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for m in DELETE_RE.finditer(node.value):
                        if m.group(1) not in JOB_SQL_ALLOWLIST:
                            offenders.append(f"{name}: DELETE FROM {m.group(1)}")
    assert not offenders, f"scheduled job deletes user data (#734): {sorted(set(offenders))}"
