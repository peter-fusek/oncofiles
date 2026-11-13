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
4. Scheduler: jobs are discovered by AST in every module (``add_job``,
   ``@scheduled_job``, ``create_task`` in ``lifespan``). No job, nor any
   function it reaches by name (same module or ``from oncofiles.x import``),
   references a deletion helper or hard-deletes a table outside the technical
   allowlist, except allowlisted reachable paths. Synthetic jobs prove it fails.
5. Machine-fact re-extraction soft-deletes ``clinical_records`` with an audit
   row; core tables are never hard-deleted or rewritten via ``REPLACE INTO``.

This is the single hard-DELETE allowlist; #723 (PR #732) merged its own lock
into it: treatment_events and conversation_entries are soft-deleted and listed
in ``NEVER_HARD_DELETED``.
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
    ("persistent_oauth.py", "mcp_oauth_tokens"): (
        "revoked / rotated OAuth token, and the oauth_token_cleanup job's "
        "delete_expired_oauth_tokens (credential)"
    ),
    ("session_revocation.py", "session_revocations"): "expired logout denylist rows (auth state)",
    ("server.py", "prompt_log"): "prompt_log_cleanup: 90-day AI telemetry retention (#734 open)",
    (
        "database/_documents.py",
        "document_pages",
    ): "delete_ocr_pages: OCR cache reset right before it is rewritten from the current source",
}

# Tables that hold the patient's documents and facts: never hard-deleted.
NEVER_HARD_DELETED = {
    "documents",
    "lab_values",
    "clinical_records",
    "patients",
    # #723: soft delete with deleted_at / deleted_by, restorable for 30 days
    "treatment_events",
    "conversation_entries",
}


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


# ── 4. Scheduled jobs (discovered, never listed by hand) ─────────────────
#
# Jobs are found by AST in every module under src/oncofiles: each
# ``<anything>.add_job(target, ...)`` call, each ``@<x>.scheduled_job``
# decorator, and each ``create_task`` / ``ensure_future`` inside a ``lifespan``
# function (background loops started at boot). From each job function the
# walker follows every function it references by name — in the same module, or imported with
# ``from oncofiles.<mod> import <fn>`` — and flags any deletion reference.
# Calls on objects (``db.foo()``) are not followed; locks 1-3 cover those
# call sites globally.

# Tables a scheduled job may hard-delete from: credentials and telemetry only.
JOB_SQL_ALLOWLIST = {"mcp_oauth_tokens", "prompt_log", "session_revocations"}

FORBIDDEN_IN_JOBS = (
    _DRIVE_TRASH_ATTRS
    | _DOC_TRASH_ATTRS
    | {
        "delete",
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

# (module file, function, reference) reachable from a job -> reason. May only shrink.
JOB_REACHABLE_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "split.py",
        "split_document",
        "delete_document",
    ): "soft delete: original row replaced by its parts (Drive original untouched)",
    (
        "sync.py",
        "sync_from_gdrive",
        "delete_ocr_pages",
    ): "OCR cache of the old content dropped when the Drive file's content changed",
    (
        "tools/curated.py",
        "_index_patient",
        "delete_document",
    ): "soft delete of a curated mirror row whose Drive file left the filed folders",
    (
        "tools/curated.py",
        "_index_patient",
        "delete_ocr_pages",
    ): "curated pages rewritten from the Drive sidecar in the same step",
}

REMOVED_JOB_IDS = {"trash_cleanup", "empty_folder_cleanup"}


def _module_name(rel: str) -> str:
    parts = rel[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(["oncofiles", *parts])


class _Module:
    def __init__(self, rel: str, tree: ast.Module) -> None:
        self.rel = rel
        self.tree = tree
        self.funcs: dict[str, list[ast.AST]] = defaultdict(list)
        self.imports: dict[str, tuple[str, str]] = {}  # alias -> (module, name)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[node.name].append(node)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and (node.module or "").startswith("oncofiles")
            ):
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (node.module, alias.name)


def _load_modules(sources: dict[str, str] | None = None) -> dict[str, _Module]:
    if sources is None:
        sources = {
            p.relative_to(SRC).as_posix(): p.read_text(encoding="utf-8") for p in SRC.rglob("*.py")
        }
    return {_module_name(rel): _Module(rel, ast.parse(text)) for rel, text in sources.items()}


def _job_registrations(mod: _Module) -> tuple[set[str], set[str]]:
    """(root function names, job ids) registered in one module."""
    roots: set[str] = set()
    ids: set[str] = set()
    for node in ast.walk(mod.tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_job"
        ):
            target = node.args[0] if node.args else None
            for kw in node.keywords:
                if kw.arg == "func":
                    target = kw.value
                if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                    ids.add(kw.value.value)
            if isinstance(target, (ast.Name, ast.Lambda, ast.Call, ast.Attribute)):
                # Name: the job; Lambda / functools.partial(...): every name inside.
                roots |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
                roots |= {n.attr for n in ast.walk(target) if isinstance(n, ast.Attribute)}
            else:  # make an unparseable job loud, not invisible
                raise AssertionError(f"{mod.rel}: unrecognised add_job target")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "lifespan":
            # Background loops started at boot (e.g. the curated nightly loop).
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in {"create_task", "ensure_future"}
                ):
                    roots |= {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                call = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(call, ast.Attribute) and call.attr == "scheduled_job":
                    roots.add(node.name)
    return roots, ids


def _job_offenders(modules: dict[str, _Module]) -> list[tuple[str, str, str]]:
    todo: list[tuple[str, str]] = []
    for name, mod in modules.items():
        todo.extend((name, root) for root in _job_registrations(mod)[0])
    seen: set[tuple[str, str]] = set()
    offenders: set[tuple[str, str, str]] = set()
    while todo:
        mod_name, fn_name = todo.pop()
        mod = modules.get(mod_name)
        if (mod_name, fn_name) in seen or mod is None:
            continue
        seen.add((mod_name, fn_name))
        if fn_name not in mod.funcs:
            if fn_name in mod.imports:  # re-export / module-level alias
                todo.append(mod.imports[fn_name])
            continue
        for fn in mod.funcs[fn_name]:
            for node in ast.walk(fn):
                ref = None
                if isinstance(node, ast.Attribute):
                    ref = node.attr
                elif isinstance(node, ast.Name):
                    ref = node.id
                    if ref in mod.funcs:
                        todo.append((mod_name, ref))
                    elif ref in mod.imports:
                        todo.append(mod.imports[ref])
                if ref in FORBIDDEN_IN_JOBS:
                    offenders.add((mod.rel, fn_name, ref))
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for m in DELETE_RE.finditer(node.value):
                        if m.group(1) not in JOB_SQL_ALLOWLIST:
                            offenders.add((mod.rel, fn_name, f"DELETE FROM {m.group(1)}"))
    return sorted(offenders)


def test_scheduler_roots_are_found():
    """Guard the guard: discovery must see the real job functions."""
    roots, ids = _job_registrations(_load_modules()["oncofiles.server"])
    assert {"_run_sync", "_run_oauth_token_cleanup", "_startup_catchup"} <= roots
    assert "oauth_token_cleanup" in ids


def test_removed_jobs_are_not_registered():
    ids: set[str] = set()
    for mod in _load_modules().values():
        ids |= _job_registrations(mod)[1]
    assert not ids & REMOVED_JOB_IDS, sorted(ids & REMOVED_JOB_IDS)
    server_text = (SRC / "server.py").read_text(encoding="utf-8")
    for job_id in REMOVED_JOB_IDS:  # also gone from the /readiness + dashboard job list
        assert f'"{job_id}"' not in server_text, job_id


def test_scheduled_jobs_never_delete_user_data():
    offenders = [o for o in _job_offenders(_load_modules()) if o not in JOB_REACHABLE_ALLOWLIST]
    assert not offenders, f"scheduled job deletes user data (#734): {offenders}"


def test_job_reachable_allowlist_no_stale_entries():
    stale = set(JOB_REACHABLE_ALLOWLIST) - set(_job_offenders(_load_modules()))
    assert not stale, f"allowlist entries no longer reachable, remove them: {sorted(stale)}"


# ── 4b. The scheduler lock catches a synthetic new job ──────────────────

_SYNTH_HELPER = """
from oncofiles.gdrive_client import GDriveClient

def tidy_drive(gdrive, file_id):
    gdrive.trash_file(file_id)

async def tidy_db(db):
    await db.db.execute("DELETE FROM documents WHERE deleted_at IS NOT NULL")
"""


def _synthetic(job_source: str) -> dict[str, _Module]:
    return _load_modules({"synth_helpers.py": _SYNTH_HELPER, "synth_jobs.py": job_source})


def test_synthetic_job_calling_cross_module_trash_fails_the_lock():
    job = """
from oncofiles.synth_helpers import tidy_drive

def start(scheduler, gdrive):
    async def _run_new_cleanup():
        tidy_drive(gdrive, "x")

    scheduler.add_job(_run_new_cleanup, "cron", id="new_cleanup")
"""
    assert ("synth_helpers.py", "tidy_drive", "trash_file") in _job_offenders(_synthetic(job))


def test_synthetic_lambda_job_with_hard_delete_fails_the_lock():
    job = """
from oncofiles.synth_helpers import tidy_db

def start(scheduler, db):
    scheduler.add_job(lambda: tidy_db(db), "interval", id="purge_again")
"""
    offenders = _job_offenders(_synthetic(job))
    assert ("synth_helpers.py", "tidy_db", "DELETE FROM documents") in offenders


def test_synthetic_decorated_job_calling_soft_delete_fails_the_lock():
    job = """
@sched.scheduled_job("cron", hour=3)
async def nightly_dedup(db, doc):
    await db.delete_document(doc.id)
"""
    assert ("synth_jobs.py", "nightly_dedup", "delete_document") in _job_offenders(_synthetic(job))


def test_synthetic_harmless_job_passes():
    job = """
def start(scheduler, db):
    async def _ping():
        await db.db.execute("DELETE FROM prompt_log WHERE 0")

    scheduler.add_job(_ping, "interval", id="ping")
"""
    assert _job_offenders(_synthetic(job)) == []


# ── 5. Machine-fact re-extraction is a soft delete ──────────────────────

# Tables never rewritten by INSERT OR REPLACE / REPLACE INTO (which deletes the
# old row). lab_values deliberately uses an idempotent upsert keyed on
# (document_id, parameter) and is not listed here.
REPLACE_RE = re.compile(r"(?i:REPLACE\s+INTO)\s+([a-z_][a-z0-9_]*)\b")
NEVER_REPLACED = {"documents", "clinical_records", "patients"}


def test_core_tables_never_replaced():
    found = {
        m.group(1)
        for path in SRC.rglob("*.py")
        for m in REPLACE_RE.finditer(path.read_text(encoding="utf-8"))
    }
    assert not found & NEVER_REPLACED, sorted(found & NEVER_REPLACED)


async def test_replace_document_facts_soft_deletes_with_audit(db):
    from tests.helpers import ERIKA_UUID, make_doc

    doc = await db.insert_document(make_doc(file_id="f_facts"), patient_id=ERIKA_UUID)
    fact = {"category": "lab", "occurred_at": "2027-03-01", "label": "Invented", "value_num": 1.0}
    await db.replace_document_facts(
        patient_id=ERIKA_UUID, document_id=doc.id, facts=[fact], extracted_by="test-a"
    )
    res = await db.replace_document_facts(
        patient_id=ERIKA_UUID, document_id=doc.id, facts=[fact], extracted_by="test-b"
    )
    assert res["removed"] == 1 and res["added"] == 1

    async with db.db.execute(
        "SELECT id, deleted_at FROM clinical_records WHERE source_document_id = ? ORDER BY id",
        (doc.id,),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 2  # the old row is still there
    assert rows[0]["deleted_at"] is not None and rows[1]["deleted_at"] is None

    async with db.db.execute(
        "SELECT action FROM clinical_record_audit WHERE record_id = ? ORDER BY id",
        (rows[0]["id"],),
    ) as cur:
        assert [r["action"] for r in await cur.fetchall()] == ["create", "delete"]
