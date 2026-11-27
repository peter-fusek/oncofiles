"""#856 data rights: Drive disconnect, export, erasure on request.

Two synthetic patients (example.test addresses, invented slugs, values and
2027+ dates); every registry table gets a row for each patient from a generic
seeder, so "the erasure leaves zero rows" and "the export holds only own rows"
are checked table by table. No network: Google's revocation endpoint is faked.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sqlite3
import uuid
import zipfile
from contextlib import contextmanager, nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import JSONResponse

from oncofiles import data_rights, oauth, patient_erasure, pre_store, server
from oncofiles.constants import SYSTEM_NO_PATIENT
from oncofiles.database import Database
from oncofiles.tools import curated
from oncofiles.tools import patient as patient_tools
from tests.helpers import PATIENT_A_UUID

A = PATIENT_A_UUID
B = "00000000-0000-4000-8000-0000000008b6"
A_SLUG, B_SLUG = "dr856-anna", "dr856-boris"
A_EMAIL, B_EMAIL = "carer-a@example.test", "carer-b@example.test"
ADMIN_EMAIL = "admin-856@example.test"


# ── generic seeder: one row per registry table and patient ───────────────


async def _table_info(db, table):
    async with db.db.execute(f'PRAGMA table_info("{table}")') as cur:
        cols = [dict(r) for r in await cur.fetchall()]
    async with db.db.execute(f'PRAGMA foreign_key_list("{table}")') as cur:
        fk_rows = await cur.fetchall()
    # PRAGMA foreign_key_list: from-column -> (target table, target column)
    fks = {row["from"]: (row["table"], row["to"]) for row in fk_rows}
    return cols, fks


async def _first_id(db, table, pid):
    if table == "patients":
        return pid
    cols, _ = await _table_info(db, table)
    if "patient_id" not in {c["name"] for c in cols}:
        return None  # shared directory row (organization, device): left NULL
    sql = f"SELECT id FROM {table} WHERE patient_id = ? ORDER BY id LIMIT 1"
    async with db.db.execute(sql, (pid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


# Values a CHECK constraint or the code expects, per (table, column).
OVERRIDES = {
    ("oauth_tokens", "provider"): "google",
    ("activity_log", "status"): "ok",
    ("inbound_jobs", "state"): "done",
    ("inbound_claimed_jobs", "state"): "done",
    ("inbound_quarantine", "state"): "rejected",
    ("inbound_quarantine", "file_count"): 0,
    ("inbound_quarantine_tokens", "action"): "reject",
    ("dashboard_access_log", "event"): "patient_read",
    ("patient_consents", "kind"): "wizard",
}


async def _seed_row(db, table, pid, tag):
    cols, fks = await _table_info(db, table)
    names, values = [], []
    for c in cols:
        name = c["name"]
        if c["pk"] and c["type"].upper() == "INTEGER":
            continue
        value = None
        if (table, name) in OVERRIDES:
            value = OVERRIDES[(table, name)]
        elif name in ("patient_id", "target_patient_id"):
            value = pid
        elif name.endswith("_uuid"):
            value = str(uuid.uuid4())
        elif name == "bucket":
            async with db.db.execute(
                "SELECT token_hash FROM inbound_addresses WHERE patient_id = ? LIMIT 1", (pid,)
            ) as cur:
                value = (await cur.fetchone())[0]
        elif name in fks:
            target = fks[name][0]
            value = await _first_id(db, target, pid)
            if value is None and not c["notnull"]:
                continue
        elif c["notnull"] and c["dflt_value"] is None:
            t = (c["type"] or "").upper()
            value = 1 if "INT" in t else 1.0 if "REAL" in t else f"{tag}-{table}-{name}"
        else:
            continue
        names.append(f'"{name}"')
        values.append(value)
    placeholders = ", ".join("?" for _ in values)
    sql = f'INSERT INTO "{table}" ({", ".join(names)}) VALUES ({placeholders})'
    for _ in range(12):  # a CHECK ... IN ('a', ...) names the allowed values: take the first
        try:
            await db.db.execute(sql, values)
            return
        except sqlite3.IntegrityError as exc:
            m = re.search(r"CHECK constraint failed: (\w+) IN \('([^']*)'", str(exc))
            if not m or f'"{m.group(1)}"' not in names:
                raise
            values[names.index(f'"{m.group(1)}"')] = m.group(2)
    raise AssertionError(f"could not seed {table}")


async def _seed(db, pid, tag):
    for rule in reversed(data_rights.REGISTRY):
        if rule.table == "patients":
            continue
        await _seed_row(db, rule.table, pid, tag)
    await db.db.commit()


@pytest.fixture
async def two(db: Database) -> Database:
    await db.db.execute(
        "UPDATE patients SET slug = ?, caregiver_email = ? WHERE patient_id = ?",
        (A_SLUG, A_EMAIL, A),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (B, B_SLUG, "Boris Synthetic", B_EMAIL),
    )
    await db.db.commit()
    await _seed(db, A, "anna")
    await _seed(db, B, "boris")
    await _link(db, A, "a" * 64)
    await _link(db, B, "b" * 64)
    return db


async def _link(db, pid, account_hash, version=None):
    """The granting account as the OAuth callback stores it with the token
    (migration 104): keyed hash + hash version on the oauth_tokens row."""
    from oncofiles.google_link import GOOGLE_ACCOUNT_HASH_VERSION

    await db.db.execute(
        "UPDATE oauth_tokens SET google_account_hash = ?, google_account_hash_v = ? "
        "WHERE patient_id = ?",
        (account_hash, GOOGLE_ACCOUNT_HASH_VERSION if version is None else version, pid),
    )
    await db.db.commit()


async def _all_counts(db) -> dict[str, int]:
    async with db.db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
    ) as cur:
        names = [r[0] for r in await cur.fetchall()]
    out = {}
    for name in names:
        async with db.db.execute(f'SELECT COUNT(*) FROM "{name}"') as cur:
            out[name] = (await cur.fetchone())[0]
    return out


@pytest.fixture(autouse=True)
def _clean_curated_state():
    yield
    for pid in (A, B):
        curated._runs.pop(pid, None)
        curated._reocr_jobs.pop(pid, None)
        curated._patient_locks.pop(pid, None)


@pytest.fixture
def fake_copy(monkeypatch):
    calls = []

    def _copy(db, job, **kw):
        calls.append(job)
        return f"oncofiles-20270101T000000000000Z-{job}.db"

    monkeypatch.setattr(pre_store, "make_copy", _copy)
    return calls


@pytest.fixture
def fake_revoke(monkeypatch):
    calls = []

    def _revoke(token, **kw):
        calls.append(token)
        return "revoked"

    monkeypatch.setattr(oauth, "revoke_token", _revoke)
    return calls


# ── registry completeness (the lock the maintainer asked for) ────────────


async def test_registry_classifies_every_table_of_the_migrated_schema(db: Database):
    async with db.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cur:
        tables = {r[0] for r in await cur.fetchall()}
    registry = {r.table for r in data_rights.REGISTRY}
    keep = set(data_rights.NOT_PATIENT_DATA)
    assert not registry & keep
    unclassified = tables - registry - keep
    assert not unclassified, f"classify these tables in data_rights (#856): {sorted(unclassified)}"
    stale = (registry | keep) - tables
    assert not stale, f"no longer in the schema, remove: {sorted(stale)}"
    assert len(registry) == len(data_rights.REGISTRY)  # no table listed twice
    assert data_rights.REGISTRY[-1].table == "patients"  # erased last


async def test_every_table_with_a_patient_reference_is_in_the_registry(db: Database):
    """A table that points at a patient, directly or through a patient table,
    can never be filed under NOT_PATIENT_DATA."""
    registry = {r.table for r in data_rights.REGISTRY}
    patient_parents = {"patients", "documents", "clinical_records", "inbound_quarantine"}
    offenders = []
    for table in data_rights.NOT_PATIENT_DATA:
        cols, fks = await _table_info(db, table)
        names = {c["name"] for c in cols}
        if names & {"patient_id", "target_patient_id"}:
            offenders.append(table)
        if {t for t, _ in fks.values()} & (patient_parents | registry):
            offenders.append(table)
        # a reference to a patient row without a declared FK (C3)
        if any(
            n.endswith("document_id") or n in {"record_id", "quarantine_id", "item_uuid"}
            for n in names
        ):
            offenders.append(table)
    assert not offenders, offenders


def test_patient_consents_is_erased_and_every_rule_is_scoped():
    tables = {r.table: r for r in data_rights.REGISTRY}
    assert "patient_consents" in tables  # maintainer decision 2026-11-19
    for rule in data_rights.REGISTRY:
        assert ":pid" in rule.erase_scope and ":pid" in rule.export_where, rule.table
        assert rule.reason.strip(), rule.table


def test_credentials_are_never_exported():
    for table in ("oauth_tokens", "patient_tokens", "inbound_addresses", "inbound_jobs"):
        assert data_rights.rule_for(table).export is False
    for col in (
        "access_token",
        "refresh_token",
        "token_hash",
        "caller_email_hash",
        "sender_enc",
        "caregiver_email",
        "owner_email",
        "created_by",
        "caller_identity",
    ):
        assert data_rights.is_secret_column(col), col
    assert not data_rights.is_secret_column("surname_token")
    assert not data_rights.is_secret_column("extracted_text")


async def test_seeder_reaches_every_registry_table(two: Database):
    counts_a = await data_rights.count_rows(two, A)
    empty = [t for t, n in counts_a.items() if n == 0]
    assert not empty, empty


# ── erasure ───────────────────────────────────────────────────────────────


async def test_dry_run_counts_and_writes_nothing(two: Database, fake_copy, fake_revoke):
    before = await _all_counts(two)
    changes = two.db.total_changes
    out = await patient_erasure.erase_patient(two, patient_id=A, confirm_slug=None)
    assert out["status"] == "dry_run"
    assert out["rows"]["patient_consents"] == 1 and out["rows"]["patients"] == 1
    assert out["drive_files"] == "untouched"
    assert await _all_counts(two) == before
    assert two.db.total_changes == changes
    assert fake_copy == [] and fake_revoke == []


async def test_real_erasure_leaves_no_row_and_the_other_patient_untouched(
    two: Database, fake_copy, fake_revoke
):
    b_before = await data_rights.count_rows(two, B)
    out = await patient_erasure.erase_patient(
        two,
        patient_id=A,
        confirm_slug=A_SLUG,
        dry_run=False,
        reason="request by e-mail 2027-01-05",
        caller_email=ADMIN_EMAIL,
    )
    assert out["status"] == "erased", out
    assert out["drive_files"] == "untouched"
    assert fake_copy == ["erase"]
    assert fake_revoke == ["anna-oauth_tokens-refresh_token"]
    assert all(n == 0 for n in (await data_rights.count_rows(two, A)).values())
    assert await data_rights.count_rows(two, B) == b_before
    assert await two.get_patient(A) is None
    # the full-text index followed the documents (delete trigger)
    async with two.db.execute(
        "SELECT COUNT(*) FROM documents_fts WHERE documents_fts MATCH ?", ('"anna"',)
    ) as cur:
        assert (await cur.fetchone())[0] == 0
    # one audit row, under no patient, carrying counts and the caller's hash only
    async with two.db.execute(
        "SELECT patient_id, input_summary, output_summary FROM activity_log "
        "WHERE tool_name = 'erase_patient'"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1 and rows[0]["patient_id"] == SYSTEM_NO_PATIENT
    flat = json.dumps(rows)
    assert A not in flat and A_SLUG not in flat and "@" not in flat
    assert "request by e-mail" not in flat  # the reason is never stored


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"confirm_slug": "dr856-boris", "reason": "x"}, "confirm_slug_mismatch"),
        ({"confirm_slug": None, "reason": "x"}, "confirm_slug_mismatch"),
        ({"confirm_slug": A_SLUG, "reason": "  "}, "reason_required"),
        ({"confirm_slug": A_SLUG, "reason": "y" * 201}, "reason_too_long"),
    ],
)
async def test_real_erasure_refusals_change_nothing(two, fake_copy, fake_revoke, kwargs, code):
    before = await _all_counts(two)
    out = await patient_erasure.erase_patient(two, patient_id=A, dry_run=False, **kwargs)
    assert out == {"status": "refused", "code": code}
    assert await _all_counts(two) == before and fake_copy == [] and fake_revoke == []


async def test_erasure_refused_while_curated_work_runs(two, fake_copy, fake_revoke):
    curated._runs[A] = {"state": "running"}
    out = await patient_erasure.erase_patient(
        two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
    )
    assert out == {"status": "refused", "code": "run_in_progress"}
    assert fake_copy == []


async def test_no_pre_store_copy_no_erasure(two, monkeypatch, fake_revoke):
    def _fail(db, job, **kw):
        raise pre_store.PreStoreError("no local database file to copy")

    monkeypatch.setattr(pre_store, "make_copy", _fail)
    before = await _all_counts(two)
    out = await patient_erasure.erase_patient(
        two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
    )
    assert out == {"status": "refused", "code": "pre_store_failed"}
    assert await _all_counts(two) == before and fake_revoke == []


async def test_erasure_rolls_back_as_a_whole(two, fake_copy, fake_revoke):
    """A failure at the LAST delete (patients) leaves every row in place."""
    await two.db.execute(
        "CREATE TEMP TRIGGER block_patient_delete BEFORE DELETE ON patients "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    await two.db.commit()
    before = await _all_counts(two)
    out = await patient_erasure.erase_patient(
        two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
    )
    assert out == {"status": "refused", "code": "rolled_back"}
    after = await _all_counts(two)
    assert after == before
    assert fake_revoke == []  # the Google link keeps working after a rollback


def test_deferred_fk_failure_reports_the_violating_tables():
    """C5: a deferred FK failure at the commit names tables and counts only."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, patient_id TEXT)")
    conn.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
    )
    conn.execute("INSERT INTO parent (id, patient_id) VALUES (1, 'p')")
    conn.execute("INSERT INTO child (parent_id) VALUES (1)")
    conn.execute("INSERT INTO child (parent_id) VALUES (1)")
    rules = (data_rights.TableRule("parent", "patient_id = :pid", False, "t"),)
    with (
        patch.object(data_rights, "REGISTRY", rules),
        pytest.raises(patient_erasure.ErasureRolledBackError) as info,
    ):
        patient_erasure._delete_rows_sync(conn, "p")
    assert info.value.fk_violations == {"child": 2}
    assert conn.execute("SELECT COUNT(*) FROM parent").fetchone()[0] == 1  # rolled back
    assert not conn.in_transaction
    conn.close()


async def test_erasure_returns_fk_violations_on_rollback(two, fake_copy, fake_revoke):
    exc = patient_erasure.ErasureRolledBackError(sqlite3.IntegrityError("x"), {"links": 3})

    def _boom(conn, pid):
        raise exc

    with patch.object(patient_erasure, "_delete_rows_sync", _boom):
        out = await patient_erasure.erase_patient(
            two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
        )
    assert out == {"status": "refused", "code": "rolled_back", "fk_violations": {"links": 3}}
    assert fake_revoke == []


async def test_erasure_compacts_fts_and_reports_no_residue(two, fake_copy, fake_revoke):
    """C4 + C6: after the commit the FTS indexes are optimised and the WAL
    truncated (best effort), and the registry is re-counted."""
    seen: list[str] = []
    real = patient_erasure._compact_sync

    def _spy(conn):
        seen.append("compact")
        return real(conn)

    with patch.object(patient_erasure, "_compact_sync", _spy):
        out = await patient_erasure.erase_patient(
            two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
        )
    assert out["status"] == "erased"
    assert seen == ["compact"]
    assert out["residual_rows"] == {}
    assert out["compaction"] == "ok"


def test_compaction_optimises_existing_fts_tables_and_never_raises():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(body)")
    conn.execute("INSERT INTO documents_fts (body) VALUES ('invented text 2027')")
    conn.commit()
    # conversation_entries_fts does not exist: skipped, not a failure
    assert patient_erasure._compact_sync(conn) == []
    assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 0  # restored
    # an open transaction of someone else: nothing committed, reported
    conn.execute("INSERT INTO documents_fts (body) VALUES ('pending')")
    assert conn.in_transaction
    assert patient_erasure._compact_sync(conn) == ["db_busy"]
    assert conn.in_transaction
    conn.rollback()
    conn.close()
    # a closed connection: the erasure still never fails
    assert patient_erasure._compact_sync(conn) == ["compact"]


async def test_erasure_reports_rows_left_after_the_commit(two, fake_copy, fake_revoke):
    real = data_rights.count_rows
    calls = {"n": 0}

    async def _count(db, pid):
        calls["n"] += 1
        out = await real(db, pid)
        if calls["n"] == 2:  # the re-count after the commit
            out["documents"] = 1
        return out

    with patch.object(data_rights, "count_rows", _count):
        out = await patient_erasure.erase_patient(
            two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
        )
    assert out["status"] == "erased" and out["residual_rows"] == {"documents": 1}


async def test_erasure_removes_legacy_prompt_log_rows_of_the_patients_documents(
    two, fake_copy, fake_revoke
):
    """C2: a prompt_log row with a legacy patient_id ('') that points at one of
    the patient's documents is erased with the patient; B's stays."""
    ids = {}
    for pid in (A, B):
        async with two.db.execute("SELECT id FROM documents WHERE patient_id = ?", (pid,)) as cur:
            ids[pid] = (await cur.fetchone())[0]
    for pid in (A, B):
        await _seed_row(two, "prompt_log", "", f"legacy-{pid[-4:]}")
        await two.db.execute(
            "UPDATE prompt_log SET document_id = ? WHERE id = (SELECT MAX(id) FROM prompt_log)",
            (ids[pid],),
        )
    await two.db.commit()
    assert (await data_rights.count_rows(two, A))["prompt_log"] == 2
    rule = data_rights.rule_for("prompt_log")
    assert rule.export is False and "document_id" not in rule.export_where
    # the documents subquery must run before documents are deleted
    order = [r.table for r in data_rights.REGISTRY]
    assert order.index("prompt_log") < order.index("documents")
    out = await patient_erasure.erase_patient(
        two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
    )
    assert out["status"] == "erased" and out["rows_removed"]["prompt_log"] == 2
    async with two.db.execute(
        "SELECT COUNT(*) FROM prompt_log WHERE document_id = ?", (ids[A],)
    ) as cur:
        assert (await cur.fetchone())[0] == 0
    async with two.db.execute(
        "SELECT COUNT(*) FROM prompt_log WHERE document_id = ?", (ids[B],)
    ) as cur:
        assert (await cur.fetchone())[0] == 1  # the legacy row of B's document stays
    assert (await data_rights.count_rows(two, B))["prompt_log"] == 2


def test_erase_patient_audit_summary_never_keeps_the_slug():
    """C1: the generic audit middleware redacts patient_slug for erase_patient
    (the row is written under SYSTEM_NO_PATIENT, dry runs included)."""
    from oncofiles.audit_redaction import sensitive_strings, summarize_args

    args = {"patient_slug": A_SLUG, "confirm_slug": A_SLUG, "dry_run": True, "reason": "x"}
    summary = summarize_args(args, "erase_patient")
    assert A_SLUG not in summary and "dry_run=True" in summary
    assert f"patient_slug=<redacted:len={len(A_SLUG)}>" in summary
    assert A_SLUG in sensitive_strings(args, "erase_patient")  # scrubbed from logs too
    # every other tool keeps a slug-shaped patient_slug as before
    assert f"patient_slug={A_SLUG}" in summarize_args(args, "list_documents")
    assert A_SLUG not in sensitive_strings({"patient_slug": A_SLUG}, "list_documents")


def test_middleware_audit_row_of_erase_patient_has_no_slug():
    from types import SimpleNamespace

    from oncofiles.audit_middleware import _summarize_input

    msg = SimpleNamespace(arguments={"patient_slug": A_SLUG, "dry_run": True})
    assert A_SLUG not in _summarize_input(msg, "erase_patient")


async def test_erasure_removes_the_patients_encrypted_files_only(
    two, fake_copy, fake_revoke, tmp_path, monkeypatch
):
    from oncofiles import config

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    qroot = tmp_path / "quarantine"
    hroot = tmp_path / "holding"
    hroot.mkdir()
    files = {}
    for pid in (A, B):
        item = str(uuid.uuid4())
        (qroot / item).mkdir(parents=True)
        (qroot / item / "0.bin").write_bytes(b"cipher")
        (qroot / item / "1.bin").write_bytes(b"cipher")
        held = str(uuid.uuid4())
        (hroot / f"{held}.bin").write_bytes(b"cipher")
        await two.db.execute(
            "UPDATE inbound_quarantine SET item_uuid = ?, file_count = 2 WHERE patient_id = ?",
            (item, pid),
        )
        await two.db.execute(
            "UPDATE documents SET file_id = ? WHERE patient_id = ?", (f"holding:{held}", pid)
        )
        files[pid] = (qroot / item, hroot / f"{held}.bin")
    await two.db.commit()
    out = await patient_erasure.erase_patient(
        two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
    )
    assert out["status"] == "erased", out
    assert out["quarantine"]["files_removed"] == 2 and out["held_files"] == {"removed": 1}
    assert not files[A][0].exists() and not files[A][1].exists()
    b_item_dir, b_held = files[B]
    assert (b_item_dir / "0.bin").exists()
    assert b_held.exists()


async def test_rolled_back_erasure_leaves_the_files_intact(
    two, fake_copy, fake_revoke, tmp_path, monkeypatch
):
    """Files go only after the commit: a rollback (or db_busy) keeps every
    encrypted file and every row, and revokes nothing."""
    from oncofiles import config

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    item = str(uuid.uuid4())
    (tmp_path / "quarantine" / item).mkdir(parents=True)
    (tmp_path / "quarantine" / item / "0.bin").write_bytes(b"cipher")
    held = str(uuid.uuid4())
    (tmp_path / "holding").mkdir()
    (tmp_path / "holding" / f"{held}.bin").write_bytes(b"cipher")
    await two.db.execute(
        "UPDATE inbound_quarantine SET item_uuid = ?, file_count = 1 WHERE patient_id = ?",
        (item, A),
    )
    await two.db.execute(
        "UPDATE documents SET file_id = ? WHERE patient_id = ?", (f"holding:{held}", A)
    )
    await two.db.execute(
        "CREATE TEMP TRIGGER block_patient_delete BEFORE DELETE ON patients "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    await two.db.commit()
    before = await _all_counts(two)
    out = await patient_erasure.erase_patient(
        two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
    )
    assert out == {"status": "refused", "code": "rolled_back"}
    assert await _all_counts(two) == before
    assert (tmp_path / "quarantine" / item / "0.bin").exists()
    assert (tmp_path / "holding" / f"{held}.bin").exists()
    assert fake_revoke == []


async def test_cross_patient_reference_export_excludes_erasure_removes(two, fake_copy, fake_revoke):
    """A stray cross-reference between A's and B's documents: A's export never
    hands out B's document id; erasing A removes the row; B keeps the rest."""
    async with two.db.execute("SELECT id FROM documents WHERE patient_id = ?", (A,)) as cur:
        a_doc = (await cur.fetchone())[0]
    async with two.db.execute("SELECT id FROM documents WHERE patient_id = ?", (B,)) as cur:
        b_doc = (await cur.fetchone())[0]
    await two.db.execute(
        "INSERT INTO document_cross_references (source_document_id, target_document_id, "
        "relationship) VALUES (?, ?, 'related')",
        (a_doc, b_doc),
    )
    await two.db.commit()
    rule = data_rights.rule_for("document_cross_references")
    assert " AND " in rule.export_where and " OR " in rule.erase_scope
    raw = b"".join([c async for c in data_rights.export_stream(two, A)])
    zf = zipfile.ZipFile(io.BytesIO(raw))
    rows = [json.loads(x) for x in zf.read("tables/document_cross_references.jsonl").splitlines()]
    assert rows and all(
        b_doc not in (r["source_document_id"], r["target_document_id"]) for r in rows
    )
    b_before = await data_rights.count_rows(two, B)
    out = await patient_erasure.erase_patient(
        two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
    )
    assert out["status"] == "erased"
    async with two.db.execute(
        "SELECT COUNT(*) FROM document_cross_references WHERE source_document_id = ? "
        "OR target_document_id = ?",
        (a_doc, a_doc),
    ) as cur:
        assert (await cur.fetchone())[0] == 0
    b_after = await data_rights.count_rows(two, B)
    # B lost exactly the one cross-reference that touched A's document
    assert b_after["document_cross_references"] == b_before["document_cross_references"] - 1
    assert {t: n for t, n in b_after.items() if t != "document_cross_references"} == {
        t: n for t, n in b_before.items() if t != "document_cross_references"
    }


def test_every_two_sided_scope_has_a_narrower_export():
    """A rule whose erasure reaches rows through EITHER of two references must
    export only rows wholly the patient's."""
    for rule in data_rights.REGISTRY:
        if " OR " in rule.erase_scope and rule.export:
            assert rule.export_scope and " OR " not in rule.export_scope, rule.table


async def test_refused_file_path_after_commit_is_named_for_the_operator(
    two, fake_copy, fake_revoke, tmp_path, monkeypatch
):
    from oncofiles import config

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    (tmp_path / "quarantine").mkdir()
    item = str(uuid.uuid4())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, tmp_path / "quarantine" / item)
    await two.db.execute(
        "UPDATE inbound_quarantine SET item_uuid = ?, file_count = 1 WHERE patient_id = ?",
        (item, A),
    )
    await two.db.commit()
    out = await patient_erasure.erase_patient(
        two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
    )
    assert out["status"] == "erased"  # the rows are gone (committed first)
    assert out["leftover_files"] == {"quarantine_items": [item], "held_files": []}
    assert (tmp_path / "quarantine" / item).is_symlink()  # nothing followed the link
    assert all(n == 0 for n in (await data_rights.count_rows(two, A)).values())


async def test_erasure_refuses_when_another_transaction_is_open(two, fake_copy, fake_revoke):
    await two.db.execute("UPDATE patients SET display_name = 'x' WHERE patient_id = ?", (B,))
    try:
        out = await patient_erasure.erase_patient(
            two, patient_id=A, confirm_slug=A_SLUG, dry_run=False, reason="r"
        )
        assert out == {"status": "refused", "code": "db_busy"}
    finally:
        await two.db.rollback()
    assert await two.get_patient(A) is not None


async def test_erase_patient_tool_is_admin_only_and_dry_by_default(two, fake_copy, fake_revoke):
    from oncofiles.persistent_oauth import _verified_caller_is_admin

    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": two, "files": MagicMock()}
    tok = _verified_caller_is_admin.set(False)
    try:
        with patch(
            "oncofiles.tools._helpers._require_admin_or_raise",
            side_effect=ValueError("admin only"),
        ):
            denied = json.loads(await patient_tools.erase_patient(ctx, A_SLUG))
    finally:
        _verified_caller_is_admin.reset(tok)
    assert "error" in denied
    with patch("oncofiles.tools._helpers._require_admin_or_raise", return_value=None):
        dry = json.loads(await patient_tools.erase_patient(ctx, A_SLUG))
        assert dry["status"] == "dry_run" and dry["rows"]["documents"] == 1
        real = json.loads(
            await patient_tools.erase_patient(
                ctx, A_SLUG, confirm_slug=A_SLUG, dry_run=False, reason="ticket 2027-01"
            )
        )
    assert real["status"] == "erased"
    assert await two.get_patient(B) is not None


# ── Drive disconnect ─────────────────────────────────────────────────────


def _request(db, *, body=None, patient_id=None):
    request = MagicMock()
    request.query_params = {"patient_id": patient_id} if patient_id else {}
    request.path_params = {}
    request.headers = {"authorization": "Bearer session:synthetic"}
    request.app.state.fastmcp_server._lifespan_result = {"db": db, "files": None}
    request.json = AsyncMock(return_value=body or {})
    return request


@contextmanager
def _caller(email, *, admin=False, bearer=False):
    unauthorized = JSONResponse({"error": "unauthorized"}, status_code=401)
    with (
        patch.object(server, "_get_dashboard_email", return_value=email),
        patch.object(server, "_check_bearer", return_value=None if bearer else unauthorized),
        patch.object(server, "_is_admin_email", return_value=admin),
    ):
        yield


async def _token(db, pid):
    return await db.get_oauth_token(pid)


async def test_disconnect_revokes_deletes_and_stops_curated(two, fake_revoke):
    await two.set_curated_nightly(patient_id=A, enabled=True, updated_by="test")
    server._patient_clients_cache[A] = ("drive", None, None, "folder", 0.0)
    with _caller(A_EMAIL):
        resp = await server.api_drive_disconnect(_request(two, body={"patient_id": A_SLUG}))
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data == {
        "status": "disconnected",
        "google_revoke": "revoked",
        "tokens_removed": 1,
        "curated_processing": "off",
    }
    assert fake_revoke == ["anna-oauth_tokens-refresh_token"]
    assert await _token(two, A) is None
    assert await _token(two, B) is not None  # the other patient keeps its link
    assert A not in await two.list_curated_nightly_patients()
    assert A not in server._patient_clients_cache
    async with two.db.execute(
        "SELECT input_summary, output_summary FROM activity_log "
        "WHERE tool_name = 'drive_disconnect' AND patient_id = ?",
        (A,),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1 and "@" not in json.dumps(rows)
    # documents and facts stay: a disconnect deletes nothing else
    assert (await data_rights.count_rows(two, A))["documents"] == 1
    # idempotent
    with _caller(A_EMAIL):
        again = json.loads(
            (await server.api_drive_disconnect(_request(two, body={"patient_id": A}))).body
        )
    assert again["status"] == "not_connected" and again["google_revoke"] == "no_token"


async def test_disconnect_refused_while_a_run_is_active(two, fake_revoke):
    curated._runs[A] = {"state": "running"}
    with _caller(A_EMAIL):
        resp = await server.api_drive_disconnect(_request(two, body={"patient_id": A}))
    assert resp.status_code == 409
    assert json.loads(resp.body)["code"] == "run_in_progress"
    assert await _token(two, A) is not None and fake_revoke == []


async def _fresh_link_rows(db):
    """Every token's granting account unknown (a link from before migration 104)."""
    await db.db.execute(
        "UPDATE oauth_tokens SET google_account_hash = NULL, google_account_hash_v = NULL"
    )
    await db.db.commit()


async def test_an_old_hash_version_counts_as_unknown(two, fake_revoke):
    await _link(two, A, "a" * 64, version=0)
    out = await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert out["google_revoke"] == "skipped_shared_or_unknown" and fake_revoke == []
    assert await _token(two, A) is None  # our own token is deleted anyway


async def test_another_patients_old_hash_version_blocks_the_revoke(two, fake_revoke):
    await _link(two, B, "b" * 64, version=0)
    out = await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert out["google_revoke"] == "skipped_shared_or_unknown" and fake_revoke == []


async def test_the_audit_log_never_decides_a_revoke(two, fake_revoke):
    """A google_link audit row naming a unique account does not make an unknown
    token revocable (the decision reads only oauth_tokens)."""
    await _fresh_link_rows(two)
    await two.db.execute(
        "INSERT INTO activity_log (session_id, agent_id, tool_name, input_summary, "
        "status, patient_id) VALUES ('google-link', 'dashboard', 'google_link', ?, 'ok', ?)",
        (json.dumps({"event": "link", "new_email_hash": "c" * 64}), A),
    )
    await two.db.commit()
    out = await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert out["google_revoke"] == "skipped_shared_or_unknown" and fake_revoke == []


def _id_token(email: str) -> str:
    import base64

    claims = base64.urlsafe_b64encode(
        json.dumps({"email": email, "email_verified": True}).encode()
    ).decode()
    return f"x.{claims.rstrip('=')}.y"


async def _callback_link(db, pid, email, monkeypatch, *, audit_fails=False):
    from oncofiles import google_link

    monkeypatch.setenv("AUDIT_EMAIL_HASH_SECRET", "synthetic-856-hash-key")

    async def _boom(entry):
        raise sqlite3.OperationalError("audit insert failed")

    tokens = {
        "access_token": f"access-{pid[-4:]}",
        "refresh_token": f"refresh-{pid[-4:]}",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/drive",
        "id_token": _id_token(email),
    }
    audit = patch.object(db, "insert_activity_log", _boom) if audit_fails else nullcontext()
    with audit:
        stored, reason = await google_link.store_google_link(
            db,
            patient_id=pid,
            tokens=tokens,
            service="drive",
            principal_kind="session",
            privileged=True,
            allowed_account_hashes={google_link.email_hash(email)},
            source="test",
        )
    return stored, reason


async def test_callback_stores_the_account_hash_with_the_token(two, monkeypatch, fake_revoke):
    from oncofiles import google_link

    await two.db.execute("DELETE FROM oauth_tokens WHERE patient_id IN (?, ?)", (A, B))
    await two.db.commit()
    stored, reason = await _callback_link(two, A, "granting-a@example.test", monkeypatch)
    assert stored, reason
    token = await two.get_oauth_token(A)
    assert token.google_account_hash == google_link.email_hash("granting-a@example.test")
    assert token.google_account_hash_v == google_link.GOOGLE_ACCOUNT_HASH_VERSION
    async with two.db.execute("SELECT * FROM oauth_tokens WHERE patient_id = ?", (A,)) as cur:
        row = dict(await cur.fetchone())
    assert "granting-a@example.test" not in json.dumps(row, default=str)  # hash only
    # B linked with another account: A's grant is provably unshared -> revoked
    assert (await _callback_link(two, B, "granting-b@example.test", monkeypatch))[0]
    out = await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert out["google_revoke"] == "revoked" and fake_revoke == [f"refresh-{A[-4:]}"]


async def test_relink_with_another_account_replaces_the_hash(two, monkeypatch, fake_revoke):
    """A later link with the SAME account as B makes A's grant shared."""
    await two.db.execute("DELETE FROM oauth_tokens WHERE patient_id IN (?, ?)", (A, B))
    await two.db.commit()
    await _callback_link(two, A, "granting-a@example.test", monkeypatch)
    await _callback_link(two, B, "family@example.test", monkeypatch)
    await _callback_link(two, A, "family@example.test", monkeypatch)
    out = await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert out["google_revoke"] == "skipped_shared_or_unknown" and fake_revoke == []


async def test_a_failed_audit_insert_does_not_change_the_decision(two, monkeypatch, fake_revoke):
    await two.db.execute("DELETE FROM oauth_tokens WHERE patient_id IN (?, ?)", (A, B))
    await two.db.commit()
    await _callback_link(two, B, "granting-b@example.test", monkeypatch)
    stored, _ = await _callback_link(
        two, A, "granting-a@example.test", monkeypatch, audit_fails=True
    )
    assert stored
    out = await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert out["google_revoke"] == "revoked" and len(fake_revoke) == 1


@pytest.mark.parametrize(
    "a_account, b_account, revoked",
    [
        ("a" * 64, "b" * 64, True),  # own account known and unique: revoke
        ("a" * 64, "a" * 64, False),  # the same Google account on another patient
        (None, "b" * 64, False),  # own granting account unknown
        ("a" * 64, None, False),  # another patient's account unknown: cannot prove
    ],
)
async def test_revoke_only_a_grant_proven_unshared(two, fake_revoke, a_account, b_account, revoked):
    await _fresh_link_rows(two)
    if a_account:
        await _link(two, A, a_account)
    if b_account:
        await _link(two, B, b_account)
    out = await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert out["status"] == "disconnected"
    if revoked:
        assert out["google_revoke"] == "revoked" and len(fake_revoke) == 1
    else:
        assert out["google_revoke"] == "skipped_shared_or_unknown" and fake_revoke == []
    # our own token row goes either way; the other patient's stays
    assert await _token(two, A) is None and await _token(two, B) is not None


async def test_owner_email_is_not_a_grant_identity(two, fake_revoke):
    """oauth_tokens.owner_email is the Drive folder owner, not the granting
    account: equal values must not make a grant look shared, nor different
    values make it look unshared."""
    await _fresh_link_rows(two)
    await two.db.execute(
        "UPDATE oauth_tokens SET owner_email = ? WHERE patient_id = ?", ("x@example.test", A)
    )
    await two.db.execute(
        "UPDATE oauth_tokens SET owner_email = ? WHERE patient_id = ?", ("y@example.test", B)
    )
    await two.db.commit()
    out = await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert out["google_revoke"] == "skipped_shared_or_unknown" and fake_revoke == []


async def test_same_refresh_token_on_another_patient_is_never_revoked(two, fake_revoke):
    await two.db.execute(
        "UPDATE oauth_tokens SET refresh_token = 'anna-oauth_tokens-refresh_token' "
        "WHERE patient_id = ?",
        (B,),
    )
    await two.db.commit()
    out = await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert out["google_revoke"] == "skipped_shared_or_unknown" and fake_revoke == []


async def test_disconnect_rechecks_curated_work_under_the_lock(two, fake_revoke, monkeypatch):
    calls = []

    def _reason(pid, *, lock_held_by_caller=False):
        calls.append(lock_held_by_caller)
        return "reocr_in_progress" if lock_held_by_caller else None

    monkeypatch.setattr(curated, "patient_work_reason", _reason)
    out = await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert out == {"status": "refused", "code": "reocr_in_progress"}
    assert calls == [False, True]
    assert await _token(two, A) is not None and fake_revoke == []


def test_lock_held_by_caller_is_not_a_reason():
    lock = curated.patient_lock(A)
    lock._locked = True  # simulate the caller holding it
    try:
        assert curated.patient_work_reason(A) == "patient_lock_held"
        assert curated.patient_work_reason(A, lock_held_by_caller=True) is None
    finally:
        lock._locked = False


async def test_disconnect_deletes_the_token_even_when_google_fails(two, monkeypatch):
    monkeypatch.setattr(oauth, "revoke_token", lambda token, **kw: "failed")
    out = await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert out["status"] == "disconnected" and out["google_revoke"] == "failed"
    assert await _token(two, A) is None


@pytest.mark.parametrize(
    "email, admin, status",
    [
        (B_EMAIL, False, 404),  # another patient's caregiver: the one not-found answer
        (ADMIN_EMAIL, True, 403),  # admin read-only view
    ],
)
async def test_disconnect_only_by_the_caregiver(two, fake_revoke, email, admin, status):
    with _caller(email, admin=admin):
        resp = await server.api_drive_disconnect(_request(two, body={"patient_id": A}))
    assert resp.status_code == status
    assert await _token(two, A) is not None and fake_revoke == []


async def test_disconnect_revokes_even_when_the_curated_switch_fails(two, fake_revoke):
    """C9: the stored token is gone, so the Google revoke runs in a finally:
    an error switching curated processing off never skips it."""
    with (
        patch.object(two, "set_curated_nightly", AsyncMock(side_effect=RuntimeError("db"))),
        pytest.raises(RuntimeError),
    ):
        await data_rights.disconnect_drive(two, A, caller_email=A_EMAIL)
    assert fake_revoke == ["anna-oauth_tokens-refresh_token"]
    assert await _token(two, A) is None


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.mark.parametrize(
    "answer, code",
    [
        (_Resp(200), "revoked"),
        (_Resp(400, {"error": "invalid_token"}), "already_revoked"),
        (_Resp(400, {"error": "invalid_request"}), "failed"),
        (_Resp(503), "failed"),
        (OSError("network down"), "failed"),
    ],
)
def test_revoke_token_codes(monkeypatch, answer, code):
    import httpx

    seen = {}

    def _post(url, data=None, headers=None, timeout=None):
        seen["url"] = url
        seen["data"] = data
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(httpx, "post", _post)
    assert oauth.revoke_token("synthetic-refresh") == code
    assert seen["url"] == "https://oauth2.googleapis.com/revoke"
    assert seen["data"] == {"token": "synthetic-refresh"}


# ── export ────────────────────────────────────────────────────────────────


async def _body(resp) -> bytes:
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


async def _export(db, pid, email=A_EMAIL, admin=False):
    with _caller(email, admin=admin):
        return await server.api_patient_export(_request(db, patient_id=pid))


async def test_export_holds_only_own_rows_and_no_secrets(two):
    resp = await _export(two, A_SLUG)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.media_type == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]
    raw = await _body(resp)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = set(zf.namelist())
    assert {"README.md", "manifest.json"} <= names
    readme = zf.read("README.md").decode()
    assert "Google Drive" in readme and "30" in readme
    manifest = json.loads(zf.read("manifest.json"))
    exported = {r.table for r in data_rights.REGISTRY if r.export}
    assert {f"tables/{t}.jsonl" for t in exported} <= names
    for rule in data_rights.REGISTRY:
        if not rule.export:
            assert f"tables/{rule.table}.jsonl" not in names
            assert rule.table in manifest["not_exported"]
    everything = raw.decode("latin-1")
    for table in exported:
        lines = zf.read(f"tables/{table}.jsonl").decode().splitlines()
        assert len(lines) == manifest["row_counts"][table] >= 1, table
        for line in lines:
            row = json.loads(line)
            assert "boris" not in line, (table, line)  # nothing of patient B
            for col in row:
                assert not data_rights.is_secret_column(col), (table, col)
            if "patient_id" in row:
                assert row["patient_id"] == A
    text = "".join(zf.read(n).decode("utf-8", "replace") for n in names)
    assert "anna-oauth_tokens-refresh_token" not in text
    assert "anna-patient_tokens-token_hash" not in text
    assert A_EMAIL not in text and B_EMAIL not in text
    assert "boris" not in everything
    # audit row with the caller's hash, no content
    async with two.db.execute(
        "SELECT COUNT(*) FROM activity_log WHERE tool_name = 'patient_export' AND patient_id = ?",
        (A,),
    ) as cur:
        assert (await cur.fetchone())[0] == 1


async def test_one_running_export_per_caller(two, monkeypatch):
    monkeypatch.setattr(server, "_exports_running", {})
    first = await _export(two, A_SLUG)  # stream not consumed yet: still running
    assert first.status_code == 200
    second = await _export(two, A_SLUG)
    assert second.status_code == 429
    assert json.loads(second.body)["error"] == "export_busy"
    assert second.headers["retry-after"] == "30"
    # another caller is not held back
    other = await _export(two, B_SLUG, email=ADMIN_EMAIL, admin=True)
    assert other.status_code == 200
    await _body(other)
    # the slot frees when the first stream ends
    await _body(first)
    third = await _export(two, A_SLUG)
    assert third.status_code == 200
    await _body(third)
    assert server._exports_running == {}


async def test_export_slot_frees_when_the_stream_is_cut(two, monkeypatch):
    monkeypatch.setattr(server, "_exports_running", {})
    resp = await _export(two, A_SLUG)
    iterator = resp.body_iterator
    await iterator.__anext__()
    await iterator.aclose()  # client went away mid-download
    assert server._exports_running == {}


async def test_a_stale_export_slot_is_ignored(two, monkeypatch):
    import time

    monkeypatch.setattr(
        server, "_exports_running", {A_EMAIL: time.monotonic() - server._EXPORT_STALE_S - 1}
    )
    resp = await _export(two, A_SLUG)
    assert resp.status_code == 200
    await _body(resp)


async def test_export_of_a_foreign_patient_is_the_one_not_found(two):
    resp = await _export(two, A_SLUG, email=B_EMAIL)
    assert resp.status_code == 404
    unknown = await _export(two, "no-such-patient", email=B_EMAIL)
    assert unknown.status_code == 404 and unknown.body == resp.body


async def test_admin_may_export_a_named_patient(two):
    resp = await _export(two, B_SLUG, email=ADMIN_EMAIL, admin=True)
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(await _body(resp)))
    assert "anna" not in zf.read("tables/documents.jsonl").decode()


async def test_export_pages_through_large_tables(two, monkeypatch):
    monkeypatch.setattr(data_rights, "EXPORT_PAGE_ROWS", 2)
    monkeypatch.setattr(data_rights, "EXPORT_CHUNK_BYTES", 64)
    for i in range(7):
        await two.db.execute(
            "INSERT INTO conversation_entries (entry_date, entry_type, title, content, "
            "participant, patient_id) VALUES ('2027-02-01', 'note', ?, 'invented', "
            "'claude.ai', ?)",
            (f"Entry {i}", A),
        )
    await two.db.commit()
    chunks = []
    async for chunk in data_rights.export_stream(two, A):
        chunks.append(chunk)
    assert len(chunks) > 3  # streamed, not one blob
    zf = zipfile.ZipFile(io.BytesIO(b"".join(chunks)))
    lines = zf.read("tables/conversation_entries.jsonl").decode().splitlines()
    assert len(lines) == 8 and len({json.loads(x)["id"] for x in lines}) == 8


async def test_export_includes_a_trials_held_originals(two, monkeypatch):
    from oncofiles import holding_store

    await two.db.execute(
        "UPDATE documents SET file_id = 'holding:00000000-0000-4000-8000-0000000008a6', "
        "filename = '20270203_Anna_Lab.pdf' WHERE patient_id = ?",
        (A,),
    )
    await two.db.commit()
    monkeypatch.setattr(holding_store, "read", lambda file_id, pid: b"%PDF-invented")
    raw = b"".join([c async for c in data_rights.export_stream(two, A)])
    zf = zipfile.ZipFile(io.BytesIO(raw))
    originals = [n for n in zf.namelist() if n.startswith("originals/")]
    assert len(originals) == 1 and zf.read(originals[0]) == b"%PDF-invented"
    assert json.loads(zf.read("manifest.json"))["originals"] == {"included": 1, "unavailable": 0}


# ── /privacy and /terms say what the code does ───────────────────────────


async def test_privacy_and_terms_describe_the_data_rights():
    server._PRIVACY_HTML = None
    server._TERMS_HTML = None
    privacy = " ".join((await server.privacy_policy(None)).body.decode().split())
    terms = " ".join((await server.terms_of_service(None)).body.decode().split())
    assert "download an export of everything Oncofiles stores about the patient" in privacy
    assert "can disconnect Google Drive" in privacy
    assert "asks Google to revoke it when no other patient uses the same Google account" in privacy
    assert "your Drive files are not changed" in privacy
    assert "handled by hand, within 30 days" in privacy  # maintainer decision 2026-11-19
    assert "the consent record (section 2)" in privacy  # erased with the patient
    assert "files in your Google Drive are never touched" in privacy
    assert "before deleting a patient on request" in privacy  # pre_store "erase"
    assert "only the 5 newest" in privacy and pre_store.KEEP == 5
    assert "encrypted backups for up to 6 months" in privacy
    assert '"Disconnect Google Drive" on the dashboard' in terms
    assert "within 30 days" in terms
    assert "Consent version 2026-11-27.2" in privacy and "Consent version 2026-11-27.2" in terms


# ── wiring ────────────────────────────────────────────────────────────────


def test_routes_and_tool_are_wired():
    from oncofiles.tool_annotations import DESTRUCTIVE_TOOLS
    from oncofiles.tool_surface import OPERATOR_TOOLS

    assert "erase_patient" in OPERATOR_TOOLS and "erase_patient" in DESTRUCTIVE_TOOLS
    js = (server.Path(server.__file__).parent / "dashboard.js").read_text()
    html = (server.Path(server.__file__).parent / "dashboard.html").read_text()
    assert "/api/drive-disconnect" in js and "/api/patient-export" in js
    assert 'data-action="disconnectDrive"' in html and 'data-action="downloadPatientExport"' in html
    assert "disconnectDrive: 1" in js and "downloadPatientExport: 1" in js  # demo blocks them


def test_disconnect_helpers_never_touch_drive_files():
    src = (server.Path(server.__file__).parent / "data_rights.py").read_text()
    src += (server.Path(server.__file__).parent / "patient_erasure.py").read_text()
    for forbidden in ("trash_file", "files().delete", "GDriveClient", "drive_ops"):
        assert forbidden not in src, forbidden


def test_sqlite_secure_delete_restored_after_erasure():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (patient_id TEXT)")
    with patch.object(
        data_rights, "REGISTRY", (data_rights.TableRule("t", "patient_id = :pid", False, "t"),)
    ):
        assert patient_erasure._delete_rows_sync(conn, "p") == {"t": 0}
    assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 0
    conn.close()


def test_no_event_loop_blocking_helpers_left():
    # the erasure's DB job runs on the connection's own thread, never on the loop
    assert asyncio.iscoroutinefunction(patient_erasure.erase_patient)
