"""Engine-level patient scoping for query_db (#738).

The AST check in `_validate_query` is the second layer. These tests call
`_execute_query` directly — i.e. WITHOUT the AST check — and prove that
SQLite itself (per-call temp views + `set_authorizer`, read-only connection)
returns only the caller's rows or denies the query, for every bypass shape
known from #484 / #590.

Two synthetic patients, synthetic documents, dates in 2027+. Each test runs
against both execution paths: the per-call read-only file connection
(production) and the shared in-memory connection (tests/dev).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.database import Database
from oncofiles.tools import db_query
from oncofiles.tools.db_query import (
    _PARENT_SCOPED,
    ALLOWED_TABLES_PATIENT_SCOPED,
    ENFORCEMENT_ENGINE,
    ENFORCEMENT_FALLBACK,
    _enforcement_mode,
    _execute_query,
    _readonly_uri,
    _shadow_with_ctes,
    query_db,
)
from tests.helpers import make_doc, make_treatment_event

PID_A = "00000000-0000-4000-8000-0000000007a1"
PID_B = "00000000-0000-4000-8000-0000000007b2"
N_A = 2
N_B = 3


async def _seed(db: Database) -> None:
    for pid, slug, name in ((PID_A, "synth-a", "Synth Alpha"), (PID_B, "synth-b", "Synth Beta")):
        await db.db.execute(
            "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
            "VALUES (?, ?, ?, ?)",
            (pid, slug, name, f"{slug}@example.invalid"),
        )
    await db.db.commit()
    for i in range(N_A):
        await db.insert_document(
            make_doc(
                file_id=f"synth_a_{i}",
                filename=f"2027010{i + 1}_SynthAlpha_SynthClinic_Labs_PanelA{i}.pdf",
                document_date=date(2027, 1, i + 1),
                institution="SynthClinic",
            ),
            patient_id=PID_A,
        )
    for i in range(N_B):
        await db.insert_document(
            make_doc(
                file_id=f"synth_b_{i}",
                filename=f"2027020{i + 1}_SynthBeta_SynthClinic_Labs_PanelB{i}.pdf",
                document_date=date(2027, 2, i + 1),
                institution="SynthClinic",
            ),
            patient_id=PID_B,
        )
    await db.insert_treatment_event(
        make_treatment_event(event_date=date(2027, 1, 5), title="Synth cycle A"),
        patient_id=PID_A,
    )
    await db.insert_treatment_event(
        make_treatment_event(event_date=date(2027, 2, 5), title="Synth cycle B"),
        patient_id=PID_B,
    )


@pytest.fixture(params=["file", "memory"])
async def two_db(request, tmp_path):
    path = tmp_path / "synth.db" if request.param == "file" else ":memory:"
    database = Database(path)
    await database.connect()
    await database.migrate()
    await _seed(database)
    yield database
    await database.close()


async def _run(db: Database, sql: str, *, pid: str | None = PID_A, is_admin: bool = False):
    return json.loads(await _execute_query(db, sql, 200, caller_pid=pid, is_admin=is_admin))


async def _main_doc_count(db: Database) -> int:
    async with db.db.execute("SELECT COUNT(*) FROM main.documents") as cur:
        return (await cur.fetchone())[0]


# ── Bypass shapes: only the caller's rows ─────────────────────────────

ONLY_CALLER_ROWS = [
    # OR 1=1 on the patient filter
    f"SELECT patient_id FROM documents WHERE patient_id = '{PID_A}' OR 1=1",
    "SELECT patient_id FROM documents",
    # second, unfiltered alias
    f"SELECT b.patient_id FROM documents a, documents b WHERE a.patient_id = '{PID_A}'",
    # CTE
    "WITH x AS (SELECT patient_id FROM documents) SELECT patient_id FROM x",
    "WITH RECURSIVE x(patient_id) AS (SELECT patient_id FROM documents) SELECT * FROM x",
    # UNION
    f"SELECT patient_id FROM documents WHERE patient_id = '{PID_A}' "
    "UNION ALL SELECT patient_id FROM documents",
    # window function
    "SELECT patient_id, row_number() OVER (ORDER BY id) AS rn FROM documents",
    # RIGHT JOIN with the filter only on the ON clause
    f"SELECT d.patient_id FROM (SELECT 1) x RIGHT JOIN documents d ON d.patient_id = '{PID_A}'",
    # LEFT JOIN, patient table on the preserved side
    f"SELECT d.patient_id FROM documents d LEFT JOIN (SELECT 1) x ON d.patient_id = '{PID_A}'",
    # FULL JOIN
    "SELECT d.patient_id FROM documents d FULL JOIN treatment_events t ON 0",
    # explicit temp schema is the scoped view itself
    "SELECT patient_id FROM temp.documents",
    # other patient-scoped table
    "SELECT patient_id FROM treatment_events",
]


@pytest.mark.parametrize("sql", ONLY_CALLER_ROWS)
async def test_bypass_shapes_return_only_caller_rows(two_db, sql):
    out = await _run(two_db, sql)
    assert "rows" in out, out
    pids = {r["patient_id"] for r in out["rows"] if r.get("patient_id") is not None}
    assert pids <= {PID_A}, (sql, pids)
    assert PID_B not in json.dumps(out)


async def test_scalar_subquery_sees_only_caller_rows(two_db):
    out = await _run(
        two_db,
        "SELECT (SELECT group_concat(patient_id) FROM documents) AS p, "
        "(SELECT COUNT(*) FROM documents) AS n",
    )
    row = out["rows"][0]
    assert row["n"] == N_A
    assert set(row["p"].split(",")) == {PID_A}


async def test_window_count_sees_only_caller_rows(two_db):
    out = await _run(two_db, "SELECT count(*) OVER () AS n FROM documents")
    assert {r["n"] for r in out["rows"]} == {N_A}


async def test_json_each_over_another_table_sees_only_caller_rows(two_db):
    out = await _run(
        two_db,
        "SELECT value AS patient_id FROM json_each("
        "(SELECT json_group_array(patient_id) FROM documents))",
    )
    assert [r["patient_id"] for r in out["rows"]] == [PID_A] * N_A


async def test_or_true_count(two_db):
    out = await _run(two_db, "SELECT COUNT(*) AS n FROM documents WHERE 1=1 OR patient_id != ''")
    assert out["rows"][0]["n"] == N_A


# ── Shapes the engine denies ───────────────────────────────────────────

DENIED = [
    # direct access to the real table
    "SELECT patient_id FROM main.documents",
    "SELECT COUNT(*) FROM main.treatment_events",
    "SELECT d.patient_id FROM documents a JOIN main.documents d ON 1",
    # a user CTE named like the table, reading main.<table>
    "WITH documents AS (SELECT * FROM main.documents) SELECT patient_id FROM documents",
    # system tables
    "SELECT name FROM sqlite_master",
    "SELECT name FROM sqlite_schema",
    "SELECT sql FROM sqlite_temp_master",
    "SELECT name FROM temp.sqlite_master",
    # pragma, also as table-valued functions
    "SELECT * FROM pragma_table_info('documents')",
    "SELECT * FROM pragma_database_list",
    "PRAGMA table_info(documents)",
    "PRAGMA writable_schema = 1",
    # attach / extensions / introspection functions
    "ATTACH DATABASE ':memory:' AS other",
    "DETACH DATABASE main",
    "SELECT load_extension('synthetic_ext')",
    "SELECT sqlite_version()",
    # tables outside the allow-list
    "SELECT * FROM patients",
    # column-less references (COUNT(*), join side) report no schema
    "SELECT COUNT(*) FROM patients",
    "SELECT COUNT(*) FROM sync_history",
    "SELECT 1 FROM main.documents",
    "SELECT d.patient_id FROM documents d JOIN patients p ON 1",
    "SELECT * FROM documents_fts",
]


@pytest.mark.parametrize("sql", DENIED)
async def test_engine_denies(two_db, sql):
    with pytest.raises(sqlite3.DatabaseError):
        await _run(two_db, sql)


WRITES = [
    "INSERT INTO main.documents (file_id, filename, patient_id) VALUES ('x', 'x', 'x')",
    "INSERT INTO documents (file_id, filename) VALUES ('x', 'x')",
    "UPDATE main.documents SET patient_id = 'x'",
    "DELETE FROM main.documents",
    "DELETE FROM documents",
    "CREATE TABLE synth_t (x)",
    "CREATE TEMP TABLE synth_t (x)",
    "CREATE TEMP VIEW synth_v AS SELECT * FROM main.documents",
    "DROP VIEW temp.documents",
    "DROP TABLE main.documents",
    "ALTER TABLE main.documents ADD COLUMN synth_c TEXT",
    "BEGIN",
    "SAVEPOINT s1",
    "ANALYZE",
    "REINDEX",
]


@pytest.mark.parametrize("sql", WRITES)
async def test_writes_and_ddl_denied(two_db, sql):
    before = await _main_doc_count(two_db)
    with pytest.raises(sqlite3.DatabaseError):
        await _run(two_db, sql)
    assert await _main_doc_count(two_db) == before == N_A + N_B


async def test_invalid_pid_creates_no_views_and_fails_closed(two_db):
    for pid in ("", None, "__no_patient_access__", f"{PID_A}' OR '1'='1"):
        with pytest.raises(sqlite3.DatabaseError):
            await _run(two_db, "SELECT patient_id FROM documents", pid=pid)


async def test_admin_only_tables_engine_rule(two_db):
    with pytest.raises(sqlite3.DatabaseError):
        await _run(two_db, "SELECT COUNT(*) AS n FROM schema_migrations")
    out = await _run(two_db, "SELECT COUNT(*) AS n FROM schema_migrations", is_admin=True)
    assert out["rows"][0]["n"] > 0
    # Admin status never unlocks main.<patient table>.
    with pytest.raises(sqlite3.DatabaseError):
        await _run(two_db, "SELECT COUNT(*) FROM main.documents", is_admin=True)


# ── Legitimate queries keep working ────────────────────────────────────


async def test_legitimate_queries(two_db):
    out = await _run(two_db, f"SELECT COUNT(*) AS n FROM documents WHERE patient_id = '{PID_A}'")
    assert out["rows"][0]["n"] == N_A

    out = await _run(
        two_db,
        "SELECT d.filename, t.title FROM documents d "
        "JOIN treatment_events t ON t.patient_id = d.patient_id "
        f"WHERE d.patient_id = '{PID_A}' ORDER BY d.id",
    )
    assert len(out["rows"]) == N_A
    assert {r["title"] for r in out["rows"]} == {"Synth cycle A"}

    out = await _run(
        two_db,
        "SELECT category, COUNT(*) AS n, MIN(document_date) AS first, "
        "strftime('%Y', MAX(document_date)) AS y, upper(institution) AS inst "
        "FROM documents GROUP BY category, institution",
    )
    assert out["rows"][0]["n"] == N_A
    assert out["rows"][0]["y"] == "2027"

    out = await _run(
        two_db,
        "SELECT a.id FROM documents a LEFT JOIN documents b ON b.id = a.id + 1 "
        "WHERE a.filename LIKE '%Synth%' AND ifnull(b.description, 'none') <> '' LIMIT 1",
    )
    assert len(out["rows"]) == 1


async def test_temp_views_are_dropped_after_call(two_db):
    await _run(two_db, "SELECT COUNT(*) FROM documents")
    with pytest.raises(sqlite3.DatabaseError):
        await _run(two_db, "SELECT nonsense FROM documents")
    # The shared in-memory connection is back to normal for app queries.
    async with two_db.db.execute("SELECT name FROM sqlite_temp_master") as cur:
        assert await cur.fetchall() == []
    async with two_db.db.execute("SELECT COUNT(*) FROM documents") as cur:
        assert (await cur.fetchone())[0] == N_A + N_B
    # And the app connection can still write (no authorizer left behind).
    await two_db.db.execute("UPDATE documents SET institution = institution")
    await two_db.db.commit()


async def test_timeout_interrupts_query(two_db, monkeypatch):
    monkeypatch.setattr(db_query, "QUERY_TIMEOUT_S", 0.2)
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        await _run(
            two_db,
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT COUNT(*) FROM c",
        )


# ── Read-only connection, driver detection, fallback ───────────────────


def test_readonly_uri_rejects_writes(tmp_path):
    path = tmp_path / "ro.db"
    sqlite3.connect(path).execute("CREATE TABLE t (x)").connection.close()
    uri = _readonly_uri(str(path))
    assert uri.startswith("file:") and uri.endswith("?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO t VALUES (1)")
        # Temp objects still work on a read-only connection (the views need them).
        conn.execute("CREATE TEMP VIEW v AS SELECT * FROM main.t")
    finally:
        conn.close()


async def test_file_db_uses_readonly_connection(two_db):
    if str(two_db.path) == ":memory:":
        pytest.skip("file path only")
    with patch.object(db_query, "_run_on_readonly_file", wraps=db_query._run_on_readonly_file) as m:
        await _run(two_db, "SELECT COUNT(*) FROM documents")
    assert m.call_count == 1


async def test_enforcement_mode_detection(two_db):
    assert _enforcement_mode(two_db) == ENFORCEMENT_ENGINE
    fake = MagicMock()
    fake.db = object()  # e.g. the libsql/Turso wrapper
    assert _enforcement_mode(fake) == ENFORCEMENT_FALLBACK


def test_libsql_has_no_authorizer():
    libsql = pytest.importorskip("libsql")
    conn = libsql.connect(":memory:")
    assert not hasattr(conn, "set_authorizer")


async def test_fallback_cte_shadowing_scopes_rows(tmp_path):
    """The libsql fallback: AST check + leading CTEs. Run the shadowed SQL on a
    real libsql connection (if installed) over the same synthetic DB."""
    path = tmp_path / "fallback.db"
    database = Database(path)
    await database.connect()
    await database.migrate()
    await _seed(database)
    await _seed_children(database)
    await database.close()

    try:
        import libsql

        conn = libsql.connect(str(path))
    except ImportError:
        conn = sqlite3.connect(path)
    try:
        for sql in (
            "SELECT patient_id FROM documents",
            f"SELECT patient_id FROM documents WHERE patient_id = '{PID_A}' OR 1=1",
            "SELECT d.patient_id FROM documents d LEFT JOIN treatment_events t ON 0",
        ):
            rows = conn.execute(_shadow_with_ctes(sql, PID_A)).fetchall()
            assert {r[0] for r in rows} == {PID_A}, sql
            assert len(rows) == N_A
        for table, expected in (("lab_values", N_A), ("document_cross_references", 1)):
            sql = f"SELECT patient_id FROM {table} WHERE patient_id = '{PID_A}' OR 1=1"
            rows = conn.execute(_shadow_with_ctes(sql, PID_A)).fetchall()
            assert [r[0] for r in rows] == [PID_A] * expected, table
        # Invalid pid: no shadowing (the AST layer already rejected the query).
        assert _shadow_with_ctes("SELECT 1", "not-a-uuid") == "SELECT 1"
    finally:
        conn.close()


# ── Schema lock + end-to-end through the tool ──────────────────────────


async def test_every_patient_scoped_table_can_be_scoped(two_db):
    """Each patient-scoped table has its own patient_id or a parent mapping."""
    for table in sorted(ALLOWED_TABLES_PATIENT_SCOPED):
        async with two_db.db.execute(f'PRAGMA table_info("{table}")') as cur:
            cols = {r[1] for r in await cur.fetchall()}
        if "patient_id" in cols:
            continue
        assert table in _PARENT_SCOPED, table
        parent, fks = _PARENT_SCOPED[table]
        assert set(fks) <= cols, table
        async with two_db.db.execute(f'PRAGMA table_info("{parent}")') as cur:
            assert "patient_id" in {r[1] for r in await cur.fetchall()}, parent


async def _seed_children(db: Database) -> None:
    """document_pages / lab_values / cross refs carry no patient_id column."""
    async with db.db.execute("SELECT id, patient_id FROM documents ORDER BY id") as cur:
        docs = [(r[0], r[1]) for r in await cur.fetchall()]
    a_ids = [i for i, p in docs if p == PID_A]
    b_ids = [i for i, p in docs if p == PID_B]
    for doc_id in a_ids + b_ids:
        await db.db.execute(
            "INSERT INTO document_pages (document_id, page_number, extracted_text) "
            "VALUES (?, 1, 'synthetic page')",
            (doc_id,),
        )
        await db.db.execute(
            "INSERT INTO lab_values (document_id, lab_date, parameter, value) "
            "VALUES (?, '2027-03-01', 'SYNTH', 1.0)",
            (doc_id,),
        )
    first_a, second_a = a_ids[:2]
    first_b, second_b = b_ids[:2]
    links = [(first_a, second_a), (first_a, first_b), (first_b, second_b)]
    for src, tgt in links:
        await db.db.execute(
            "INSERT INTO document_cross_references (source_document_id, target_document_id) "
            "VALUES (?, ?)",
            (src, tgt),
        )
    await db.db.commit()


@pytest.mark.parametrize(
    ("table", "expected"),
    [("document_pages", N_A), ("lab_values", N_A), ("document_cross_references", 1)],
)
async def test_parent_scoped_tables_engine(two_db, table, expected):
    await _seed_children(two_db)
    out = await _run(two_db, f"SELECT patient_id FROM {table}")
    assert [r["patient_id"] for r in out["rows"]] == [PID_A] * expected
    # The mixed A->B cross reference is never visible (#517 semantics).
    with pytest.raises(sqlite3.DatabaseError):
        await _run(two_db, f"SELECT COUNT(*) FROM main.{table}")


async def test_clinical_record_notes_view_is_queryable(two_db):
    out = await _run(two_db, "SELECT COUNT(*) AS n FROM clinical_record_notes")
    assert out["rows"][0]["n"] == 0


async def test_query_db_tool_end_to_end(two_db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": two_db, "files": MagicMock()}
    with patch("oncofiles.tools.db_query._resolve_patient_id", new=AsyncMock(return_value=PID_A)):
        ok = json.loads(
            await query_db(ctx, f"SELECT patient_id FROM documents WHERE patient_id = '{PID_A}'")
        )
        assert [r["patient_id"] for r in ok["rows"]] == [PID_A] * N_A

        # If the AST layer ever let a shape through, the engine still scopes it.
        with patch("oncofiles.tools.db_query._validate_query", return_value=None):
            leaked = json.loads(await query_db(ctx, "SELECT patient_id FROM documents"))
            assert {r["patient_id"] for r in leaked["rows"]} == {PID_A}
            denied = json.loads(await query_db(ctx, "SELECT * FROM main.documents"))
            assert "Denied by the database engine" in denied["error"]
