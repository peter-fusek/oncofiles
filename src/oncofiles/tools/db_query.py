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
"""

from __future__ import annotations

import asyncio
import json
import logging

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
      - Admin callers must pass patient_slug= explicitly; no implicit admin bypass.
      - System-wide tables (ALLOWED_TABLES_ADMIN_ONLY) are admin-only (#541).

    Args:
        sql: SELECT query. Tables: see ALLOWED_TABLES in this module.
        limit: Max rows to return (default 50, max 200).
        patient_slug: Explicit patient slug (#429). Required for admin callers
            reading patient-scoped tables; otherwise the caller's resolved
            patient is used.
    """
    # Resolve caller identity first — required for the filter check.
    try:
        caller_pid = await _resolve_patient_id(patient_slug, ctx, required=False)
    except ValueError as exc:
        return json.dumps({"error": f"patient lookup failed: {exc}"})

    # Validate.
    err = _validate_query(sql, caller_pid, is_admin=_is_admin_caller())
    if err is not None:
        return json.dumps({"error": err})

    # Enforce row limit.
    row_limit = min(max(1, limit), MAX_ROWS)
    query = sql.rstrip().rstrip(";")
    if "LIMIT" not in query.upper():
        query = f"{query} LIMIT {row_limit}"

    db = _get_db(ctx)
    try:
        await db.reconnect_if_stale(timeout=5.0)
        result = await asyncio.wait_for(
            _execute_query(db, query, row_limit),
            timeout=QUERY_TIMEOUT_S,
        )
        return result
    except TimeoutError:
        return json.dumps({"error": f"Query timed out after {QUERY_TIMEOUT_S}s"})
    except Exception as exc:
        return json.dumps(_safe_error(exc, "query_db_execution_failed"))


async def _execute_query(db, query: str, row_limit: int) -> str:
    """Execute a read-only query and return JSON result."""
    async with db.db.execute(query) as cursor:
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = await cursor.fetchall()

    results = []
    for row in rows[:row_limit]:
        record = {}
        for idx, col in enumerate(columns):
            # Turso returns dicts, aiosqlite returns Row objects
            val = row[col] if isinstance(row, dict) else row[idx]
            if isinstance(val, bytes):
                val = f"<bytes:{len(val)}>"
            elif val is not None and not isinstance(val, (str, int, float, bool)):
                val = str(val)
            record[col] = val
        results.append(record)

    return json.dumps(
        {
            "columns": columns,
            "rows": results,
            "row_count": len(results),
            "truncated": len(rows) > row_limit,
        }
    )


def register(mcp):
    mcp.tool()(query_db)
