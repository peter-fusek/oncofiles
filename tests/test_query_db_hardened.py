"""Security regression tests for the hardened query_db (#486 / v5.15).

Lock down the exact bypass corpus that was empirically exploitable before
the sqlglot-AST allow-list check. Every row below corresponds to a query
that DID return data against prod on 2026-04-24 and must now 4xx.

Tests target the pure _validate_query() helper to avoid DB dependencies —
the full tool_call → DB round-trip is covered by integration tests.
"""

from __future__ import annotations

import pytest

from oncofiles.tools.db_query import (
    ALLOWED_TABLES,
    ALLOWED_TABLES_ADMIN_ONLY,
    ALLOWED_TABLES_PATIENT_SCOPED,
    _validate_query,
)

# Fixed caller identity for tests.
CALLER_PID = "00000000-0000-4000-8000-000000000001"
OTHER_PID = "ffffffff-ffff-4fff-8fff-ffffffffffff"


# ── Denied tables ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        # The table that matched Michal's screenshot.
        "SELECT * FROM patients",
        "SELECT slug, display_name, caregiver_email FROM patients",
        # Other PII tables excluded from the allow-list.
        "SELECT * FROM newsletter_subscribers",
        "SELECT * FROM mcp_oauth_tokens",
        "SELECT * FROM oauth_tokens",
        "SELECT * FROM patient_tokens",
        "SELECT * FROM patient_selection",
        # FTS shadow tables.
        "SELECT * FROM documents_fts",
        "SELECT * FROM conversation_entries_fts",
    ],
)
def test_denied_tables(sql):
    err = _validate_query(sql, CALLER_PID)
    assert err is not None
    assert "allow-list" in err or "not allowed" in err


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM sqlite_master",
        "SELECT name FROM sqlite_master WHERE type='table'",
        "SELECT * FROM sqlite_sequence",
        "SELECT name FROM pragma_table_info('patients')",
        "SELECT * FROM pragma_database_list",
    ],
)
def test_system_table_denied(sql):
    err = _validate_query(sql, CALLER_PID)
    assert err is not None
    assert "system table" in err.lower() or "not in the query_db allow-list" in err


def test_schema_qualified_table_denied():
    err = _validate_query("SELECT * FROM main.sqlite_master", CALLER_PID)
    assert err is not None


# ── Bypass corpus for the old regex scoping ──────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        # Original Michal-era bypasses on an allow-listed table.
        "SELECT * FROM documents",  # no WHERE
        "SELECT * FROM documents WHERE patient_id LIKE '%'",
        "SELECT * FROM documents WHERE patient_id IS NOT NULL",
        "SELECT * FROM documents WHERE patient_id IN ('any')",
        # Equality vs wrong literal.
        f"SELECT * FROM documents WHERE patient_id = '{OTHER_PID}'",
        # GROUP BY alone no longer satisfies the filter.
        "SELECT patient_id, COUNT(*) FROM activity_log GROUP BY patient_id",
        # Table alias but no WHERE equality.
        "SELECT d.filename FROM documents AS d",
        # Equality against column, not literal.
        "SELECT * FROM documents WHERE patient_id = filename",
    ],
)
def test_patient_scope_bypasses_rejected(sql):
    err = _validate_query(sql, CALLER_PID)
    assert err is not None, f"query should have been rejected: {sql}"


# ── CTE and UNION rejected ───────────────────────────────────────────


def test_cte_rejected():
    err = _validate_query(
        f"WITH x AS (SELECT * FROM documents WHERE patient_id = '{CALLER_PID}') SELECT * FROM x",
        CALLER_PID,
    )
    assert err is not None
    assert "CTE" in err or "not in the query_db allow-list" in err


def test_cte_hiding_denied_table_rejected():
    """CTEs can't be used to smuggle a reference to patients table."""
    err = _validate_query(
        "WITH x AS (SELECT * FROM patients) SELECT * FROM x",
        CALLER_PID,
    )
    assert err is not None


def test_union_rejected():
    err = _validate_query(
        f"SELECT filename FROM documents WHERE patient_id = '{CALLER_PID}' "
        "UNION SELECT display_name FROM patients",
        CALLER_PID,
    )
    assert err is not None


# ── Positive cases ──────────────────────────────────────────────────


def test_valid_simple_query():
    err = _validate_query(
        f"SELECT filename FROM documents WHERE patient_id = '{CALLER_PID}'",
        CALLER_PID,
    )
    assert err is None


def test_valid_query_with_table_alias():
    err = _validate_query(
        f"SELECT d.filename FROM documents d WHERE d.patient_id = '{CALLER_PID}'",
        CALLER_PID,
    )
    assert err is None


def test_valid_query_with_additional_filters():
    err = _validate_query(
        f"SELECT * FROM documents WHERE patient_id = '{CALLER_PID}' "
        "AND filename LIKE '%.pdf' ORDER BY created_at DESC",
        CALLER_PID,
    )
    assert err is None


def test_valid_query_with_literal_on_left_side():
    """SQL allows `'uuid' = patient_id` just as well as the reverse; accept both."""
    err = _validate_query(
        f"SELECT * FROM documents WHERE '{CALLER_PID}' = patient_id",
        CALLER_PID,
    )
    assert err is None


def test_admin_only_table_no_filter_required():
    """schema_migrations / sync_history have no patient_id column; allowed without filter."""
    err = _validate_query("SELECT * FROM schema_migrations", CALLER_PID)
    assert err is None
    err = _validate_query("SELECT * FROM sync_history", CALLER_PID)
    assert err is None


# ── No implicit admin scope ──────────────────────────────────────────


def test_empty_pid_cannot_read_patient_scoped_table():
    """Admin / sentinel caller (empty pid) must pass patient_slug=; no implicit read."""
    err = _validate_query(
        "SELECT * FROM documents WHERE patient_id = 'something'",
        "",  # empty pid
    )
    assert err is not None
    assert "authorized patient" in err or "implicit admin" in err


def test_empty_pid_can_still_read_admin_only_table():
    """Admin-only tables don't trigger the per-patient check."""
    err = _validate_query("SELECT * FROM schema_migrations", "")
    assert err is None


# ── Mutation defense-in-depth ────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM documents WHERE 1=1",
        "UPDATE documents SET filename='x' WHERE 1=1",
        "INSERT INTO documents (filename) VALUES ('x')",
        "DROP TABLE documents",
        "CREATE TABLE foo (id INTEGER)",
        "ALTER TABLE documents ADD COLUMN x TEXT",
    ],
)
def test_mutations_rejected(sql):
    err = _validate_query(sql, CALLER_PID)
    assert err is not None


# ── Structural invariants ────────────────────────────────────────────


def test_denied_tables_never_overlap_with_allowed():
    denied = {
        "patients",
        "newsletter_subscribers",
        "mcp_oauth_tokens",
        "mcp_oauth_clients",
        "oauth_tokens",
        "patient_tokens",
        "patient_selection",
        "documents_fts",
        "conversation_entries_fts",
    }
    assert denied.isdisjoint(ALLOWED_TABLES), (
        f"security regression — denied table in allow-list: {denied & ALLOWED_TABLES}"
    )


def test_allowed_sets_are_frozen():
    assert isinstance(ALLOWED_TABLES, frozenset)
    assert isinstance(ALLOWED_TABLES_PATIENT_SCOPED, frozenset)
    assert isinstance(ALLOWED_TABLES_ADMIN_ONLY, frozenset)
    assert ALLOWED_TABLES == ALLOWED_TABLES_PATIENT_SCOPED | ALLOWED_TABLES_ADMIN_ONLY
