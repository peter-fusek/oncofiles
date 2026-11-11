"""Direct DB query tool for monitoring and debugging.

Security contract (#486 / v5.15 sweep):

  * **Allow-list of tables**, not a block-list. Every table referenced in
    the query — including sub-queries, CTEs, and joins — must be in
    `ALLOWED_TABLES`. `patients`, `newsletter_subscribers`, credential
    tables, FTS shadow tables, `sqlite_master`, `pragma_*` are DENIED.
  * **AST-based patient_id filter**, parsed with sqlglot. The previous
    regex check passed `GROUP BY patient_id`, `WHERE patient_id LIKE '%'`,
    `IN (…)`, `IS NOT NULL`, and admin-sentinel short-circuits — all
    empirically bypassable (#484). We now require a literal equality
    `patient_id = '<caller_pid>'` on every patient-scoped table reference.
  * **No implicit admin scope.** Admin callers must pass `patient_slug=`
    to indicate whose rows they want. The previous `and pid` short-circuit
    silently allowed unfiltered queries when the caller's resolved pid was
    empty (sentinel / admin session).
  * CTEs (`WITH`) and `UNION` are rejected — they complicate the filter
    proof without a real use case in this codebase.

Engine-level enforcement (#738). The AST check above is now the SECOND
layer; the first is SQLite itself:

  * The query runs on a dedicated **read-only** connection to the same DB
    file (``file:<path>?mode=ro``), opened per call and closed afterwards.
    For an in-memory DB (tests) the shared connection is used, with every
    step below done atomically on aiosqlite's worker thread.
  * Per call, every patient-scoped table that exists gets two TEMP VIEWs:
    an inner one with an unguessable per-call name,
    ``_qdb_<nonce>_<table> = SELECT * FROM main.<table> WHERE patient_id =
    '<pid>'``, and an outer one named exactly like the table
    (``documents = SELECT * FROM temp._qdb_<nonce>_documents``). SQLite
    resolves an unqualified name to the temp schema first, so ``documents``
    in the user's SQL is the caller's rows only, whatever shape the query
    has (OR 1=1, aliases, sub-queries, CTEs, UNION, windows, json_each,
    outer joins).
  * ``sqlite3.set_authorizer`` then DENIES every action except SELECT,
    reads and functions on a safe list. A read of ``main.<patient table>``
    is allowed only when SQLite reports the inner per-call view as the
    accessing view — so ``main.documents`` written directly, or wrapped in
    a user CTE or view named ``documents``, is denied. PRAGMA (including
    ``pragma_*`` table-valued functions), ATTACH, DETACH, writes, DDL,
    transactions, ``sqlite_*`` tables and ``load_extension`` are denied;
    extension loading is also switched off on the connection.
  * Admin-only tables keep the admin rule at the engine too.
  * The pid comes from ``_resolve_patient_id`` and is interpolated into
    the view only after it validates as a UUID; anything else creates no
    views, so patient-scoped reads fail closed.

Driver note: the only driver is aiosqlite (stdlib ``sqlite3``) on a local
file; the Turso/libsql path is gone and ``server.py`` refuses to start with
any ``TURSO_*`` variable (#617). A connection that is not aiosqlite (so may
lack ``set_authorizer``) is refused, never run with the AST check alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
import sqlglot
from fastmcp import Context
from sqlglot import expressions as exp
from sqlglot.errors import ParseError

from oncofiles.tools._helpers import (
    _get_db,
    _is_admin_caller,
    _resolve_patient_id,
    _safe_error,
)

logger = logging.getLogger(__name__)

MAX_ROWS = 200
QUERY_TIMEOUT_S = 10

# Resource limits on the engine path (#738). The progress handler runs every
# PROGRESS_CHECK_EVERY SQLite VM instructions and interrupts the query once
# MAX_VM_STEPS instructions have run OR the wall clock passes
# QUERY_TIMEOUT_S, whichever comes first (~200M steps is roughly 1 s of pure
# VM work on the Mac mini; normal queries use well under 1M). No single
# instruction can run long on its own: strings/blobs are capped at
# MAX_VALUE_BYTES (so e.g. printf/replace cannot build a huge value), and
# the sorter only ever sorts rows the step budget let through.
PROGRESS_CHECK_EVERY = 10_000
MAX_VM_STEPS = 200_000_000
# Hard caps on what one call returns, whatever LIMIT the SQL says. Rows are
# fetched one at a time and fetching stops at either cap (truncated: true),
# so a cartesian result is never materialised.
MAX_RESULT_ROWS = 5000
MAX_RESULT_BYTES = 150_000
# Per-value and per-row memory bounds (#738 review). SQLite refuses any
# string/blob longer than MAX_VALUE_BYTES (also when reading a stored value
# that long) and any result set wider than MAX_RESULT_COLUMNS, so one
# fetched row is at most MAX_RESULT_COLUMNS * MAX_VALUE_BYTES (~9.6 MB) in
# Python before the byte cap judges it. The widest allow-listed table
# (clinical_records) has 50 columns, so SELECT * on one table still works.
MAX_VALUE_BYTES = MAX_RESULT_BYTES
MAX_RESULT_COLUMNS = 64

# Patient-scoped tables — readable iff the query filters by
# `patient_id = '<caller_pid>'` literal equality. Everything in this set
# has a `patient_id` column in the schema. Adding to this list is a
# security decision — prefer a dedicated MCP tool unless ad-hoc SQL is
# genuinely needed.
ALLOWED_TABLES_PATIENT_SCOPED: frozenset[str] = frozenset(
    {
        "documents",
        "activity_log",
        "conversation_entries",
        "treatment_events",
        "research_entries",
        "lab_values",
        "document_pages",
        "agent_state",
        "patient_context",
        "prompt_log",
        "email_entries",
        "calendar_entries",
        "document_cross_references",
        "clinical_records",
        "clinical_analyses",
        "clinical_record_notes",
    }
)

# System-wide tables (no patient_id column, or rows across all patients that
# are not filtered per caller). Admin-scope callers only — enforced in
# _validate_query (#541). Fails closed: a caller whose admin status cannot be
# established is treated as non-admin.
ALLOWED_TABLES_ADMIN_ONLY: frozenset[str] = frozenset(
    {
        "schema_migrations",
        "sync_history",
    }
)

ALLOWED_TABLES: frozenset[str] = ALLOWED_TABLES_PATIENT_SCOPED | ALLOWED_TABLES_ADMIN_ONLY

# Table-valued functions a query may read from. Their input is an
# expression, so they only ever see rows the views already filtered.
_SAFE_TABLE_VALUED: frozenset[str] = frozenset({"json_each", "json_tree"})

# SQL functions the authorizer permits (#738). Anything else is denied,
# notably: load_extension, readfile/writefile/edit (CLI-only builds),
# fts3_tokenizer, fts5/fts5_source_id, sqlar_compress/sqlar_uncompress,
# random/randomblob/zeroblob (no randomness that could serve as an oracle,
# no giant blobs), changes/last_insert_rowid/total_changes, sqlite_* and
# anything an extension could register. Virtual tables (zipfile, sqlar,
# dbstat, bytecode, tables_used, fts3tokenize, fts5vocab, ...) are reads of
# a table not on the read allow-list, so the table-read gate denies them;
# json_each/json_tree are allowed but only see what their argument reads,
# and a read of main.<patient table> inside that argument is denied too.
# Lower-case names, as SQLite reports them; operators that compile to
# functions are listed too.
SAFE_FUNCTIONS: frozenset[str] = frozenset(
    {
        # aggregates
        "avg", "count", "group_concat", "max", "min", "string_agg", "sum", "total",
        # window functions
        "cume_dist", "dense_rank", "first_value", "lag", "last_value", "lead",
        "nth_value", "ntile", "percent_rank", "rank", "row_number",
        # scalar core
        "abs", "char", "coalesce", "concat", "concat_ws", "format", "glob", "hex",
        "if", "ifnull", "iif", "instr", "length", "like", "likelihood", "likely",
        "lower", "ltrim", "nullif", "octet_length", "printf", "quote", "replace",
        "round", "rtrim", "sign", "soundex", "substr", "substring", "trim",
        "typeof", "unhex", "unicode", "unlikely", "upper",
        # date/time
        "date", "datetime", "julianday", "strftime", "time", "timediff", "unixepoch",
        # math
        "acos", "asin", "atan", "atan2", "ceil", "ceiling", "cos", "degrees", "exp",
        "floor", "ln", "log", "log10", "log2", "mod", "pi", "pow", "power",
        "radians", "sin", "sqrt", "tan", "trunc",
        # JSON (scalar + aggregate; json_each/json_tree are table-valued, above)
        "json", "json_array", "json_array_length", "json_error_position",
        "json_extract", "json_group_array", "json_group_object", "json_insert",
        "json_object", "json_patch", "json_quote", "json_remove", "json_replace",
        "json_set", "json_type", "json_valid", "jsonb", "jsonb_array",
        "jsonb_extract", "jsonb_object", "->", "->>",
    }
)  # fmt: skip

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_ALLOWED_ACTIONS_NO_CHECK = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE})

ENFORCEMENT_ENGINE = "engine"  # authorizer + temp views + AST check


def _validate_query(sql: str, caller_pid: str, *, is_admin: bool = False) -> str | None:
    """Validate a SQL query against the hardening contract.

    ``is_admin`` defaults to False so any caller that forgets to pass it is
    denied the admin-only tables (fail closed, #541).

    Returns None on success or an error string on rejection.
    """
    sql_stripped = sql.strip()
    if not sql_stripped:
        return "Empty query."

    # Parse with sqlglot (SQLite dialect; prod uses Turso/libsql which is SQLite-compatible).
    try:
        tree = sqlglot.parse_one(sql_stripped, dialect="sqlite")
    except ParseError as exc:
        return f"SQL parse error: {exc}"
    if tree is None:
        return "Empty parse tree."

    # Must be a SELECT-shaped top-level statement (Select, Union would have been caught below).
    if not isinstance(tree, (exp.Select, exp.Subquery)):
        return "Only SELECT queries are allowed."

    # Reject CTEs and UNIONs — they complicate the filter proof without real use in this codebase.
    if tree.find(exp.With) is not None:
        return "CTEs (WITH clauses) are not allowed."
    if tree.find(exp.Union) is not None:
        return "UNION queries are not allowed."

    # Reject any mutation nodes (defense-in-depth; parse should prevent them for SELECT-only trees).
    for forbidden in (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Alter,
        exp.Create,
        exp.Command,
        exp.Merge,
        exp.TruncateTable,
    ):
        if tree.find(forbidden) is not None:
            return f"{forbidden.__name__} statements are not allowed."

    # Collect all table references (sqlglot walks into sub-queries automatically).
    tables = list(tree.find_all(exp.Table))
    if not tables:
        return "No table references found."

    patient_scoped_refs: list[exp.Table] = []
    for t in tables:
        name = t.name.lower()
        # Reject schema-qualified references (e.g., `main.sqlite_master`, `temp.foo`).
        if t.db:
            return f"Schema-qualified table references not allowed: {t.db}.{name}"
        # Reject pragma_* virtual tables.
        if name.startswith("pragma_") or name.startswith("sqlite_"):
            return f"Access to system table {name!r} is not allowed."
        if name not in ALLOWED_TABLES:
            return (
                f"Table {name!r} is not in the query_db allow-list. "
                f"Allowed: {sorted(ALLOWED_TABLES)}"
            )
        if name in ALLOWED_TABLES_ADMIN_ONLY and not is_admin:
            return f"Table {name!r} is admin-only (#541)."
        if name in ALLOWED_TABLES_PATIENT_SCOPED:
            patient_scoped_refs.append(t)

    # If the query touches a patient-scoped table, require a literal
    # `patient_id = '<caller_pid>'` equality somewhere in the WHERE/JOIN-ON
    # conditions. Caller must have a resolved pid — admin callers must
    # pass patient_slug= explicitly.
    if patient_scoped_refs:
        if not caller_pid:
            return (
                "Patient-scoped tables require an authorized patient. "
                "Pass patient_slug=<slug> or authenticate as a caregiver. "
                "No implicit admin scope for patient-scoped reads."
            )
        # #590: the equality must constrain EVERY patient-scoped table
        # reference, as a top-level AND conjunct of the SELECT that owns it.
        # The old check accepted one `patient_id = '<pid>'` anywhere in the
        # tree, so `... WHERE patient_id = '<pid>' OR 1=1`, a second
        # unfiltered alias (`FROM documents a, documents b`) or a scalar
        # sub-query could still enumerate other patients' patient_ids.
        for t in patient_scoped_refs:
            if not _table_ref_is_pid_filtered(t, caller_pid):
                return (
                    "Patient-scoped tables require literal equality "
                    f"WHERE patient_id = '{caller_pid}' on every reference "
                    f"(table {t.name.lower()!r}, alias {t.alias_or_name!r}), as a top-level "
                    "AND condition. OR, LIKE, IN, IS NOT NULL, GROUP BY, RIGHT/FULL "
                    "joins, unfiltered joins/sub-queries and non-matching literals "
                    "are rejected."
                )

    return None


def _conjuncts(cond: exp.Expression | None) -> list[exp.Expression]:
    """Flatten a condition into its top-level AND conjuncts (parentheses unwrapped)."""
    if cond is None:
        return []
    while isinstance(cond, exp.Paren):
        cond = cond.this
    if isinstance(cond, exp.And):
        return _conjuncts(cond.left) + _conjuncts(cond.right)
    return [cond]


def _pid_equality_qualifier(cond: exp.Expression, caller_pid: str) -> str | None:
    """If ``cond`` is ``[q.]patient_id = '<caller_pid>'`` (either side), return ``q``.

    Returns ``""`` for an unqualified column and ``None`` when ``cond`` is
    not such an equality.
    """
    if not isinstance(cond, exp.EQ):
        return None
    for col, lit in ((cond.left, cond.right), (cond.right, cond.left)):
        if (
            isinstance(col, exp.Column)
            and col.name.lower() == "patient_id"
            and isinstance(lit, exp.Literal)
            and lit.is_string
            and lit.this == caller_pid
        ):
            return (col.table or "").lower()
    return None


def _table_ref_is_pid_filtered(table: exp.Table, caller_pid: str) -> bool:
    """True when ``table`` is restricted to ``caller_pid`` in its own SELECT.

    Accepted: a top-level AND conjunct of that SELECT's WHERE, or of the ON
    clause of the join that introduces ``table``, equal to
    ``<alias>.patient_id = '<pid>'`` — or unqualified ``patient_id = '<pid>'``
    when ``table`` is the only table in that SELECT. Everything else fails
    closed.
    """
    select = table.find_ancestor(exp.Select)
    if select is None:
        return False
    qualifier = table.alias_or_name.lower()
    siblings = [t for t in select.find_all(exp.Table) if t.find_ancestor(exp.Select) is select]

    # Outer joins: an ON-clause equality does not restrict the PRESERVED side,
    # e.g. `(SELECT 1) x RIGHT JOIN documents d ON d.patient_id = '<pid>'`
    # returns every patient's documents. RIGHT and FULL joins are rejected
    # outright in any SELECT that reads a patient-scoped table.
    joins = select.args.get("joins") or []
    if any((j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
        return False
    if any((j.kind or "").upper() == "OUTER" and not j.side for j in joins):
        return False

    where = select.args.get("where")
    conditions = _conjuncts(where.this if where is not None else None)
    join = table.parent
    # An ON filter counts only for the table that join introduces, and only
    # when that table is not preserved: INNER/CROSS (no side) or the
    # right-hand table of a LEFT join. The FROM base of a LEFT join is never
    # introduced by a Join node, so it always needs the WHERE filter.
    if isinstance(join, exp.Join) and (join.side or "").upper() in ("", "LEFT"):
        conditions += _conjuncts(join.args.get("on"))

    for cond in conditions:
        q = _pid_equality_qualifier(cond, caller_pid)
        if q is None:
            continue
        if q == qualifier or (q == "" and len(siblings) == 1):
            return True
    return False


async def query_db(
    ctx: Context,
    sql: str,
    limit: int = 50,
    patient_slug: str | None = None,
) -> str:
    """Run a read-only SQL query against the database.

    Security contract (#486):
      - Allow-list of tables (see ALLOWED_TABLES). patients, newsletter_subscribers,
        credential tables, FTS shadow tables, sqlite_master, pragma_* are DENIED.
      - Patient-scoped tables require literal WHERE patient_id = '<your uuid>'.
        LIKE, IN, IS NOT NULL, GROUP BY, non-matching literals are rejected.
      - CTEs (WITH) and UNION are rejected.
      - Admin callers must pass patient_slug= explicitly for patient-scoped
        tables; without it the query is rejected, never silently scoped to the
        admin's current/default patient (#607).
      - System-wide tables (ALLOWED_TABLES_ADMIN_ONLY) are admin-only (#541).
      - Engine-enforced (#738): the query runs on a read-only connection where
        each patient table name is a per-call view of your rows only; reads of
        the real tables, PRAGMA, ATTACH, writes and DDL are denied by SQLite.
      - document_pages, lab_values, document_cross_references and
        clinical_record_notes expose their parent's patient_id column.

    Args:
        sql: SELECT query. Tables: see ALLOWED_TABLES in this module.
        limit: Max rows to return (default 50, max 200).
        patient_slug: Explicit patient slug (#429). Required for admin callers
            reading patient-scoped tables (rejected without it, #607); for
            non-admin callers the bound patient is used when omitted.
    """
    is_admin = _is_admin_caller()

    # Resolve caller identity first — required for the filter check.
    #
    # #607: an admin caller WITHOUT patient_slug gets NO patient scope. The
    # middleware still binds admins to a default/selected patient, and the
    # resolver used to hand that pid back, so an admin query filtered on
    # that pid silently ran against "whichever patient was current" — the
    # implicit admin scope this tool's contract forbids. Admins must name
    # the patient; admin-only system tables stay readable without one.
    if is_admin and not patient_slug:
        caller_pid = ""
    else:
        try:
            caller_pid = await _resolve_patient_id(patient_slug, ctx, required=False)
        except ValueError as exc:
            return json.dumps({"error": f"patient lookup failed: {exc}"})

    # Validate (second layer since #738; the engine enforces the same contract).
    err = _validate_query(sql, caller_pid, is_admin=is_admin)
    if err is not None:
        return json.dumps({"error": err})

    # Enforce row limit.
    row_limit = min(max(1, limit), MAX_ROWS)
    query = sql.rstrip().rstrip(";")
    if "LIMIT" not in query.upper():
        query = f"{query} LIMIT {row_limit}"

    db = _get_db(ctx)
    try:
        result = await asyncio.wait_for(
            _execute_query(db, query, row_limit, caller_pid=caller_pid, is_admin=is_admin),
            # The engine path also interrupts itself at QUERY_TIMEOUT_S via a
            # progress handler, so the worker thread does not outlive the call.
            timeout=QUERY_TIMEOUT_S + 2,
        )
        return result
    except TimeoutError:
        return json.dumps({"error": f"Query timed out after {QUERY_TIMEOUT_S}s"})
    except sqlite3.DatabaseError as exc:
        msg = str(exc)
        if "not authorized" in msg or "prohibited" in msg:
            # SQLite's own text names the denied action/table, never row data.
            return json.dumps({"error": f"Denied by the database engine (#738): {msg}"})
        if "too many columns" in msg:
            return json.dumps(
                {"error": f"Too many result columns; the limit is {MAX_RESULT_COLUMNS} (#738)"}
            )
        if "too big" in msg:
            return json.dumps(
                {
                    "error": f"A value exceeds {MAX_VALUE_BYTES:,} bytes; select "
                    "substr(...) or length(...) instead (#738)"
                }
            )
        if "interrupted" in msg:
            return json.dumps(
                {
                    "error": f"Query stopped: over {MAX_VM_STEPS:,} SQLite steps "
                    f"or {QUERY_TIMEOUT_S}s (#738)"
                }
            )
        return json.dumps(_safe_error(exc, "query_db_execution_failed"))
    except TooManyColumnsError as exc:
        return json.dumps({"error": f"{exc} (#738)"})
    except Exception as exc:
        return json.dumps(_safe_error(exc, "query_db_execution_failed"))


# ── Engine-level enforcement (#738) ──────────────────────────────────


@dataclass
class _Scope:
    """Per-call temp views and which inner view may read which main table."""

    nonce: str
    # patient table -> inner per-call view name
    inner_views: dict[str, str] = field(default_factory=dict)
    # main table -> inner view names allowed to read it
    allowed_sources: dict[str, set[str]] = field(default_factory=dict)
    # every table/view name in the main schema (for column-less reads)
    main_names: frozenset[str] = frozenset()

    @property
    def temp_names(self) -> frozenset[str]:
        return frozenset(self.inner_views) | frozenset(self.inner_views.values())


# Patient-scoped tables without their own patient_id column, scoped through
# the parent row(s) they reference: table -> (parent table, FK columns). A row
# is the caller's only when EVERY referenced parent is (#517 for cross refs).
# The view exposes the parent's patient_id as `patient_id`, so the AST
# layer's `patient_id = '<pid>'` requirement is satisfiable on these tables.
_PARENT_SCOPED: dict[str, tuple[str, tuple[str, ...]]] = {
    "document_pages": ("documents", ("document_id",)),
    "lab_values": ("documents", ("document_id",)),
    "document_cross_references": ("documents", ("source_document_id", "target_document_id")),
    "clinical_record_notes": ("clinical_records", ("record_id",)),
}


def _valid_pid(pid: str | None) -> str | None:
    """The pid if it is a UUID (safe to interpolate into a view), else None."""
    if pid and _UUID_RE.match(pid):
        return pid
    return None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA main.table_info("{table}")')}


def _scope_body(table: str, pid: str, *, has_patient_id: bool) -> tuple[str, str] | None:
    """SELECT body that limits ``table`` to ``pid``, and the parent it reads (or "")."""
    if has_patient_id:
        return f"SELECT * FROM main.\"{table}\" WHERE patient_id = '{pid}'", ""
    if table in _PARENT_SCOPED:
        parent, fks = _PARENT_SCOPED[table]
        joins = " ".join(
            f'JOIN main."{parent}" p{i} ON p{i}.id = c."{fk}"' for i, fk in enumerate(fks)
        )
        where = " AND ".join(f"p{i}.patient_id = '{pid}'" for i in range(len(fks)))
        return (
            f'SELECT c.*, p0.patient_id AS patient_id FROM main."{table}" c {joins} WHERE {where}',
            parent,
        )
    return None  # no way to scope it: no view, reads are denied (fail closed)


def _create_scope(conn: sqlite3.Connection, caller_pid: str | None) -> _Scope:
    """Create the per-call shadowing temp views on ``conn`` (before the authorizer)."""
    rows = conn.execute("SELECT type, name FROM main.sqlite_master").fetchall()
    scope = _Scope(
        nonce=secrets.token_hex(16),
        main_names=frozenset(r[1].lower() for r in rows if r[0] in ("table", "view")),
    )
    pid = _valid_pid(caller_pid)
    if pid is None:
        # No authorized patient: no views, so every patient-scoped read hits
        # main.<table> directly and the authorizer denies it (fail closed).
        return scope
    existing = {r[1] for r in rows if r[0] == "table"}
    for table in sorted(ALLOWED_TABLES_PATIENT_SCOPED & existing):
        view = _scope_body(table, pid, has_patient_id="patient_id" in _columns(conn, table))
        if view is None:
            continue
        body, parent = view
        inner = f"_qdb_{scope.nonce}_{table}"
        conn.execute(f'DROP VIEW IF EXISTS temp."{table}"')
        conn.execute(f'CREATE TEMP VIEW "{inner}" AS {body}')
        conn.execute(f'CREATE TEMP VIEW "{table}" AS SELECT * FROM temp."{inner}"')
        scope.inner_views[table] = inner
        scope.allowed_sources.setdefault(table, set()).add(inner)
        if parent:
            scope.allowed_sources.setdefault(parent, set()).add(inner)
    return scope


def _drop_scope(conn: sqlite3.Connection, scope: _Scope) -> None:
    for table, inner in scope.inner_views.items():
        conn.execute(f'DROP VIEW IF EXISTS temp."{table}"')
        conn.execute(f'DROP VIEW IF EXISTS temp."{inner}"')


def _make_authorizer(scope: _Scope, *, is_admin: bool):
    """Build the sqlite3 authorizer callback for one call (#738)."""
    temp_names = scope.temp_names
    allowed_sources = scope.allowed_sources
    main_names = scope.main_names
    ok, deny = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY

    def _main_read(table: str, source: str | None) -> int:
        # Patient rows only through this call's inner views: `source` is
        # SQLite's inner-most view name, unguessable per call, so a user CTE
        # or a `main.<table>` reference never matches.
        if table in ALLOWED_TABLES_PATIENT_SCOPED or table in allowed_sources:
            return ok if source in allowed_sources.get(table, ()) else deny
        if table in ALLOWED_TABLES_ADMIN_ONLY:
            return ok if is_admin else deny
        return ok if table in _SAFE_TABLE_VALUED else deny

    def _authorize(action, arg1, arg2, db_name, source):
        if action in _ALLOWED_ACTIONS_NO_CHECK:
            return ok
        if action == sqlite3.SQLITE_FUNCTION:
            return ok if (arg2 or "").lower() in SAFE_FUNCTIONS else deny
        if action != sqlite3.SQLITE_READ:
            # PRAGMA, ATTACH, DETACH, INSERT/UPDATE/DELETE, every CREATE/DROP/
            # ALTER, TRANSACTION, SAVEPOINT, ANALYZE, REINDEX, ...
            return deny
        table = (arg1 or "").lower()
        if table.startswith("sqlite_"):
            return deny
        if db_name == "temp":
            return ok if table in temp_names else deny
        if db_name == "main":
            return _main_read(table, source)
        if db_name is None and arg2 == "":
            # A table referenced without reading any column (COUNT(*), a
            # join side, a CTE). SQLite omits the schema for unqualified
            # names. Our temp views win name resolution; any other name
            # that exists in main is judged as a main read (row counts of
            # e.g. `patients` stay hidden); anything else is a CTE.
            if table in temp_names:
                return ok
            if table in main_names or table in ALLOWED_TABLES:
                return _main_read(table, source)
            return ok
        return deny

    return _authorize


def _disable_extensions(conn: sqlite3.Connection) -> None:
    if hasattr(conn, "enable_load_extension"):
        conn.enable_load_extension(False)


def _to_record(columns: list[str], row) -> dict:
    record = {}
    for idx, col in enumerate(columns):
        # Turso returns dicts, sqlite3 returns tuples / Row objects
        val = row[col] if isinstance(row, dict) else row[idx]
        if isinstance(val, bytes):
            val = f"<bytes:{len(val)}>"
        elif val is not None and not isinstance(val, (str, int, float, bool)):
            val = str(val)
        record[col] = val
    return record


@dataclass
class _Result:
    columns: list[str]
    rows: list[dict]
    truncated: bool = False
    truncated_by: str | None = None  # "rows" | "bytes"


class TooManyColumnsError(ValueError):
    """The result set is wider than MAX_RESULT_COLUMNS."""


def _estimate_row_bytes(columns: list[str], row) -> int:
    """Cheap lower bound of the row's JSON size, from per-column lengths only."""
    size = 2
    for idx, col in enumerate(columns):
        val = row[col] if isinstance(row, dict) else row[idx]
        size += len(col) + 6
        if isinstance(val, str):
            size += len(val)
        elif isinstance(val, bytes):
            size += 16  # rendered as "<bytes:N>"
        else:
            size += 4
    return size


class _Collector:
    """Accumulates rows up to the row and byte caps; tells the caller to stop."""

    def __init__(self, columns: list[str], row_limit: int) -> None:
        if len(columns) > MAX_RESULT_COLUMNS:
            raise TooManyColumnsError(
                f"Result has {len(columns)} columns; the limit is {MAX_RESULT_COLUMNS}."
            )
        self.result = _Result(columns=columns, rows=[])
        self.row_limit = min(max(1, row_limit), MAX_RESULT_ROWS)
        self.bytes = 0

    def _stop(self, reason: str) -> bool:
        self.result.truncated, self.result.truncated_by = True, reason
        return False

    def add(self, row) -> bool:
        """Add one row; False when the caller must stop fetching."""
        if len(self.result.rows) >= self.row_limit:
            return self._stop("rows")
        remaining = MAX_RESULT_BYTES - self.bytes
        # Judge the row from its column lengths before building anything.
        if _estimate_row_bytes(self.result.columns, row) > remaining:
            return self._stop("bytes")
        record = _to_record(self.result.columns, row)
        size = len(json.dumps(record, default=str))  # exact; escaping can grow it
        if size > remaining:
            return self._stop("bytes")
        self.bytes += size
        self.result.rows.append(record)
        return True


def _run_engine_scoped(
    conn: sqlite3.Connection,
    query: str,
    row_limit: int,
    caller_pid: str | None,
    is_admin: bool,
) -> _Result:
    """Run ``query`` under temp-view scoping + authorizer. Synchronous; one thread."""
    _disable_extensions(conn)
    scope = _create_scope(conn, caller_pid)
    deadline = time.monotonic() + QUERY_TIMEOUT_S
    max_calls = MAX_VM_STEPS // PROGRESS_CHECK_EVERY
    calls = 0

    def _progress() -> int:
        nonlocal calls
        calls += 1
        return 1 if calls > max_calls or time.monotonic() > deadline else 0

    # Set after the views exist (they are created under the normal limits).
    old_length = conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    old_columns = conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_RESULT_COLUMNS)
    try:
        conn.set_progress_handler(_progress, PROGRESS_CHECK_EVERY)
        conn.set_authorizer(_make_authorizer(scope, is_admin=is_admin))
        try:
            cursor = conn.execute(query)
            try:
                columns = [d[0] for d in cursor.description] if cursor.description else []
                collector = _Collector(columns, row_limit)
                while (row := cursor.fetchone()) is not None:
                    if not collector.add(row):
                        break
            finally:
                cursor.close()
        finally:
            conn.set_authorizer(None)
            conn.set_progress_handler(None, 0)
    finally:
        conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, old_columns)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, old_length)
        _drop_scope(conn, scope)
    return collector.result


def _readonly_uri(path: str) -> str:
    return f"{Path(path).resolve().as_uri()}?mode=ro"


def _run_on_readonly_file(
    path: str, query: str, row_limit: int, caller_pid: str | None, is_admin: bool
) -> _Result:
    """Open a per-call read-only connection to ``path`` and run the scoped query."""
    conn = sqlite3.connect(_readonly_uri(path), uri=True, timeout=5.0, isolation_level=None)
    try:
        return _run_engine_scoped(conn, query, row_limit, caller_pid, is_admin)
    finally:
        conn.close()


def _is_memory_path(path: str) -> bool:
    return path in ("", ":memory:") or path.startswith("file::memory:") or "mode=memory" in path


def _enforcement_mode(db) -> str:
    """Engine enforcement needs aiosqlite's stdlib connection (``set_authorizer``).

    Any other driver is refused (fail closed, #617/#738): there is no
    AST-only fallback, because the AST check alone is not a security boundary.
    """
    if isinstance(db.db, aiosqlite.Connection):
        return ENFORCEMENT_ENGINE
    raise RuntimeError(
        f"query_db needs the local aiosqlite connection, got {type(db.db).__name__} (#617)"
    )


async def _execute_query(
    db,
    query: str,
    row_limit: int,
    *,
    caller_pid: str | None = None,
    is_admin: bool = False,
) -> str:
    """Execute a read-only query under engine-level scoping and return JSON."""
    raw = db.db
    _enforcement_mode(db)  # raises for anything but aiosqlite
    if _is_memory_path(str(db.path)):
        # Tests: one in-memory connection. Views, authorizer, query and
        # cleanup run as ONE job on aiosqlite's worker thread, so no other
        # coroutine's statement can interleave and see the temp views.
        result = await raw._execute(
            lambda: _run_engine_scoped(raw._conn, query, row_limit, caller_pid, is_admin)
        )
    else:
        result = await asyncio.to_thread(
            _run_on_readonly_file, str(db.path), query, row_limit, caller_pid, is_admin
        )

    payload = {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": len(result.rows),
        "truncated": result.truncated,
    }
    if result.truncated_by:
        payload["truncated_by"] = result.truncated_by
    return json.dumps(payload)


def register(mcp):
    mcp.tool()(query_db)
