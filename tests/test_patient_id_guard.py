"""#791 step 1: no write may land under a legacy ``patient_id`` default.

Twelve tables declare ``patient_id`` with a column DEFAULT (``'erika'``,
``'default'``, ``''``) and ``curated_settings.patient_id`` is a nullable
primary key. Until the schema rebuild drops them (#791 step 2), every write
helper for those tables calls ``database._tenant.require_patient_id`` first.

Locks:
  * the guard itself (every rejected value);
  * the guarded table list equals what the migrated schema says;
  * every write helper raises (and writes nothing) for a missing pid, ``''``,
    whitespace, each legacy default and the no-access sentinel;
  * every INSERT/UPSERT into a guarded table, and every UPDATE that sets its
    ``patient_id``, anywhere in ``src/`` sits in a function that calls the
    guard, or on an allowlist with a reason. The allowlist may only shrink.

Synthetic ids only.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from oncofiles import patient_context
from oncofiles.audit_middleware import _write_audit_log
from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL, SYSTEM_NO_PATIENT
from oncofiles.database import Database
from oncofiles.database._tenant import LEGACY_DEFAULT_PATIENT_IDS, require_patient_id
from oncofiles.models import (
    CalendarEntry,
    ConversationEntry,
    EmailEntry,
    OAuthToken,
    PromptLogEntry,
)

from .helpers import (
    ERIKA_UUID,
    make_activity_log,
    make_agent_state,
    make_doc,
    make_research_entry,
    make_treatment_event,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"

# Tables whose patient_id has a DEFAULT, or is nullable, in the migrated schema.
GUARDED_TABLES = frozenset(
    {
        "activity_log",
        "agent_state",
        "calendar_entries",
        "conversation_entries",
        "curated_settings",
        "documents",
        "email_entries",
        "oauth_tokens",
        "patient_context",
        "prompt_log",
        "research_entries",
        "sync_history",
        "treatment_events",
    }
)

# Nullable patient_id on purpose; not part of #791.
SCHEMA_EXEMPT = {
    "patients": "the tenant table itself (patient_id is its primary key)",
    "codings": "shared vocabulary rows carry patient_id NULL (#711, helper-validated)",
    "identifiers": "organisation/practitioner identifiers are shared (#711, helper-validated)",
}

# Tables whose system rows may use SYSTEM_NO_PATIENT ("no patient", explicit).
SYSTEM_ROW_TABLES = {"activity_log", "agent_state", "prompt_log"}

BAD_PIDS = [None, "", "   ", " \t", *sorted(LEGACY_DEFAULT_PATIENT_IDS - {""}),
            NO_PATIENT_ACCESS_SENTINEL, f" {ERIKA_UUID}"]  # fmt: skip


# ── the guard ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [*BAD_PIDS, 0, 123, b"x", ["x"]])
def test_guard_rejects(bad):
    with pytest.raises(ValueError):
        require_patient_id(bad)
    with pytest.raises(ValueError):
        require_patient_id(bad, allow_system=True)


def test_guard_rejects_each_legacy_default():
    assert {"", "erika", "default"} <= LEGACY_DEFAULT_PATIENT_IDS
    for literal in LEGACY_DEFAULT_PATIENT_IDS:
        with pytest.raises(ValueError, match="legacy"):
            require_patient_id(literal)


def test_guard_system_marker_only_when_allowed():
    with pytest.raises(ValueError):
        require_patient_id(SYSTEM_NO_PATIENT)
    assert require_patient_id(SYSTEM_NO_PATIENT, allow_system=True) == SYSTEM_NO_PATIENT


def test_guard_accepts_a_patient():
    assert require_patient_id(ERIKA_UUID) == ERIKA_UUID
    assert require_patient_id(ERIKA_UUID, allow_system=True) == ERIKA_UUID


# ── the schema ─────────────────────────────────────────────────────────────


async def test_guarded_tables_match_schema(db: Database):
    found: dict[str, tuple] = {}
    async with db.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cur:
        tables = [r[0] for r in await cur.fetchall()]
    for table in tables:
        async with db.db.execute(f"PRAGMA table_info('{table}')") as cur:
            for col in await cur.fetchall():
                col = dict(col)
                nullable_or_defaulted = col["dflt_value"] is not None or not col["notnull"]
                if col["name"] == "patient_id" and nullable_or_defaulted:
                    found[table] = (col["notnull"], col["dflt_value"])
    assert set(found) - set(SCHEMA_EXEMPT) == GUARDED_TABLES, found
    # Step 1 is code only: the defaults are still there (step 2 removes them,
    # and then this table list shrinks with the schema).
    defaults = {t: d for t, (_nn, d) in found.items() if d is not None}
    assert len(defaults) == 12, defaults


# ── every write helper ─────────────────────────────────────────────────────


async def _count(db: Database, table: str) -> int:
    async with db.db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
        return (await cur.fetchone())[0]


def _with_pid(model, pid):
    # bypass validation so None / non-str reach the helper as a careless caller would
    object.__setattr__(model, "patient_id", pid)
    return model


def _writers():
    """(table, async writer(db, pid)) for every write helper of a guarded table."""
    now = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
    return [
        ("documents", lambda db, pid: db.insert_document(make_doc(), patient_id=pid)),
        ("treatment_events",
         lambda db, pid: db.insert_treatment_event(make_treatment_event(), patient_id=pid)),
        ("research_entries",
         lambda db, pid: db.insert_research_entry(make_research_entry(), patient_id=pid)),
        ("conversation_entries", lambda db, pid: db.insert_conversation_entry(
            ConversationEntry(entry_date=date(2027, 3, 1), title="t", content="c"),
            patient_id=pid)),
        ("agent_state", lambda db, pid: db.set_agent_state(_with_pid(make_agent_state(), pid))),
        ("activity_log",
         lambda db, pid: db.insert_activity_log(_with_pid(make_activity_log(), pid))),
        ("oauth_tokens", lambda db, pid: db.upsert_oauth_token(
            _with_pid(OAuthToken(access_token="a", refresh_token="r"), pid))),
        ("sync_history", lambda db, pid: db.insert_sync_history("manual", patient_id=pid)),
        ("email_entries", lambda db, pid: db.upsert_email_entry(
            _with_pid(EmailEntry(gmail_message_id="m-2027", date=now), pid))),
        ("calendar_entries", lambda db, pid: db.upsert_calendar_entry(
            _with_pid(CalendarEntry(google_event_id="e-2027", start_time=now), pid))),
        ("prompt_log", lambda db, pid: db.insert_prompt_log(
            _with_pid(PromptLogEntry(call_type="ocr", model="m"), pid))),
        ("curated_settings", lambda db, pid: db.set_curated_nightly(
            patient_id=pid, enabled=True, updated_by="test")),
        ("patient_context",
         lambda db, pid: patient_context.save_to_db(db.db, {"name": "X"}, patient_id=pid)),
    ]  # fmt: skip


def test_every_guarded_table_has_a_writer_under_test():
    assert {t for t, _ in _writers()} == GUARDED_TABLES


@pytest.mark.parametrize("table,writer", _writers(), ids=[t for t, _ in _writers()])
@pytest.mark.parametrize("bad", BAD_PIDS, ids=[repr(b) for b in BAD_PIDS])
async def test_write_helper_refuses_missing_or_default_pid(db: Database, table, writer, bad):
    before = await _count(db, table)
    with pytest.raises(ValueError):
        await writer(db, bad)
    assert await _count(db, table) == before


@pytest.mark.parametrize("table,writer", _writers(), ids=[t for t, _ in _writers()])
async def test_system_marker_only_for_operational_logs(db: Database, table, writer):
    before = await _count(db, table)
    if table in SYSTEM_ROW_TABLES:
        await writer(db, SYSTEM_NO_PATIENT)
        assert await _count(db, table) == before + 1
    else:
        with pytest.raises(ValueError):
            await writer(db, SYSTEM_NO_PATIENT)
        assert await _count(db, table) == before


@pytest.mark.parametrize("table,writer", _writers(), ids=[t for t, _ in _writers()])
async def test_write_helper_accepts_a_patient(db: Database, table, writer):
    before = await _count(db, table)
    await writer(db, ERIKA_UUID)
    assert await _count(db, table) == before + 1


@pytest.mark.parametrize("bad", BAD_PIDS, ids=[repr(b) for b in BAD_PIDS])
async def test_audit_middleware_never_writes_a_default(db: Database, bad):
    """The audit write is fire-and-forget: it refuses (logs an error), never raises."""
    before = await _count(db, "activity_log")
    await _write_audit_log(db, "s", "t", "", 1, "ok", None, bad)
    assert await _count(db, "activity_log") == before
    await _write_audit_log(db, "s", "t", "", 1, "ok", None, SYSTEM_NO_PATIENT)
    assert await _count(db, "activity_log") == before + 1


# ── static: every write site is guarded ────────────────────────────────────

_TABLES_RE = "|".join(sorted(GUARDED_TABLES))
_INSERT_RE = re.compile(
    rf"\b(?:INSERT|REPLACE)\b(?:\s+OR\s+\w+)?\s+INTO\s+[\"`]?({_TABLES_RE})\b", re.I
)
# patient_id assigned inside the SET clause (stops at WHERE, so a scoping
# `WHERE patient_id = ?` is not a write of it)
_UPDATE_PID_RE = re.compile(
    rf"\bUPDATE\s+({_TABLES_RE})\s+SET\b(?:(?!\bWHERE\b).)*?\bpatient_id\s*=", re.I | re.S
)

# (path relative to src/oncofiles, function) → reason. May only shrink.
UNGUARDED_WRITE_ALLOWLIST = {
    ("tools/activity.py", "backfill_activity_log_attribution"):
        "admin repair of pre-#663 rows; writes only the constant SYSTEM_NO_PATIENT",
}  # fmt: skip
_ALLOWLIST_MAX = 1

# Every guarded write site today; a new one must be added here consciously.
EXPECTED_WRITE_SITES = {
    ("audit_middleware.py", "_write_audit_log", "activity_log"),
    ("patient_context.py", "save_to_db", "patient_context"),
    ("database/_documents.py", "insert_document", "documents"),
    ("database/_clinical.py", "insert_treatment_event", "treatment_events"),
    ("database/_clinical.py", "insert_research_entry", "research_entries"),
    ("database/_conversations.py", "insert_conversation_entry", "conversation_entries"),
    ("database/_operational.py", "_do_upsert", "agent_state"),
    ("database/_operational.py", "_do_insert", "activity_log"),
    ("database/_operational.py", "upsert_oauth_token", "oauth_tokens"),
    ("database/_operational.py", "insert_sync_history", "sync_history"),
    ("database/_gmail.py", "upsert_email_entry", "email_entries"),
    ("database/_calendar.py", "upsert_calendar_entry", "calendar_entries"),
    ("database/_prompt_log.py", "insert_prompt_log", "prompt_log"),
    ("database/_curated.py", "set_curated_nightly", "curated_settings"),
    ("tools/documents.py", "reassign_document", "documents"),
    ("tools/prompt_log.py", "backfill_orphan_prompt_logs", "prompt_log"),
    ("tools/activity.py", "backfill_activity_log_attribution", "activity_log"),
}


def _calls_guard(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name == "require_patient_id":
                return True
    return False


def _write_sites() -> list[tuple[str, str, str, bool]]:
    """(path, innermost function, table, guarded) for every SQL write string in src/.

    A nested helper (``_do_insert``) counts as guarded when it or any enclosing
    function calls the guard (anywhere in its body).
    """
    sites = []

    def visit(node, stack, rel):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack = [*stack, node]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for rx in (_INSERT_RE, _UPDATE_PID_RE):
                for m in rx.finditer(node.value):
                    fn = stack[-1].name if stack else "<module>"
                    guarded = any(_calls_guard(f) for f in stack)
                    sites.append((rel, fn, m.group(1).lower(), guarded))
        for child in ast.iter_child_nodes(node):
            visit(child, stack, rel)

    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        visit(ast.parse(path.read_text(encoding="utf-8")), [], rel)
    return sites


def test_every_write_to_a_guarded_table_is_guarded():
    sites = _write_sites()
    unguarded = {
        (rel, fn, table)
        for rel, fn, table, guarded in sites
        if not guarded and (rel, fn) not in UNGUARDED_WRITE_ALLOWLIST
    }
    assert not unguarded, (
        "write to a table whose patient_id has a legacy DEFAULT without "
        f"require_patient_id (#791): {sorted(unguarded)}"
    )


def test_write_sites_are_known():
    found = {(rel, fn, table) for rel, fn, table, _ in _write_sites()}
    assert found == EXPECTED_WRITE_SITES, (
        f"new: {sorted(found - EXPECTED_WRITE_SITES)}; gone: {sorted(EXPECTED_WRITE_SITES - found)}"
    )


def test_allowlist_only_shrinks():
    assert len(UNGUARDED_WRITE_ALLOWLIST) <= _ALLOWLIST_MAX
    sites = {(rel, fn) for rel, fn, _t, guarded in _write_sites() if not guarded}
    stale = set(UNGUARDED_WRITE_ALLOWLIST) - sites
    assert not stale, f"allowlist entries no longer needed, remove them: {sorted(stale)}"
    for reason in UNGUARDED_WRITE_ALLOWLIST.values():
        assert reason.strip()
