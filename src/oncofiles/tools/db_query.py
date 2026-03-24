"""Direct DB query tool for monitoring and debugging."""

from __future__ import annotations

import asyncio
import json
import logging
import re

from fastmcp import Context

from oncofiles.tools._helpers import _get_db

logger = logging.getLogger(__name__)

# Only allow read-only SQL — whitelist approach
_ALLOWED_PREFIX = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)

# Block known mutation keywords as defense-in-depth
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|ATTACH|DETACH"
    r"|PRAGMA\s+\w+\s*=|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)

MAX_ROWS = 200
QUERY_TIMEOUT_S = 10

# Tables containing secrets or credentials — block from query_db
_BLOCKED_TABLES = re.compile(
    r"\b(oauth_tokens|mcp_oauth_clients|mcp_oauth_tokens|patient_tokens)\b",
    re.IGNORECASE,
)


async def query_db(
    ctx: Context,
    sql: str,
    limit: int = 50,
) -> str:
    """Run a read-only SQL query against the production database.

    Use this for monitoring, debugging, and ad-hoc analysis.
    Only SELECT/WITH queries are allowed — mutations are blocked.

    Args:
        sql: SQL query (SELECT only). Tables: documents, activity_log,
             conversations, treatment_events, research_entries, lab_values,
             document_pages, agent_state, patient_context, schema_migrations.
        limit: Max rows to return (default 50, max 200).
    """
    # Whitelist: must start with SELECT or WITH
    if not _ALLOWED_PREFIX.match(sql):
        return json.dumps({"error": "Only SELECT/WITH queries are allowed."})

    # Defense-in-depth: block mutation keywords anywhere in query
    if _FORBIDDEN_KEYWORDS.search(sql):
        return json.dumps({"error": "Only read-only (SELECT) queries are allowed."})

    # Block access to tables containing secrets/credentials
    if _BLOCKED_TABLES.search(sql):
        return json.dumps({"error": "Access to credential tables is not allowed."})

    # Enforce limit
    row_limit = min(max(1, limit), MAX_ROWS)
    query = sql.rstrip().rstrip(";")
    if "LIMIT" not in query.upper():
        query = f"{query} LIMIT {row_limit}"

    db = _get_db(ctx)
    try:
        result = await asyncio.wait_for(
            _execute_query(db, query, row_limit),
            timeout=QUERY_TIMEOUT_S,
        )
        return result
    except TimeoutError:
        return json.dumps({"error": f"Query timed out after {QUERY_TIMEOUT_S}s"})
    except Exception as e:
        logger.warning("query_db error: %s", e)
        return json.dumps({"error": str(e)})


async def _execute_query(db, query: str, row_limit: int) -> str:
    """Execute a read-only query and return JSON result."""
    async with db.db.execute(query) as cursor:
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = await cursor.fetchall()

    results = []
    for row in rows[:row_limit]:
        record = {}
        for i, col in enumerate(columns):
            val = row[i]
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
