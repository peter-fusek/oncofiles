"""Direct DB query tool for monitoring and debugging."""

from __future__ import annotations

import json
import logging
import re

from fastmcp import Context

from oncofiles.tools._helpers import _get_db

logger = logging.getLogger(__name__)

# Only allow read-only SQL
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|ATTACH|DETACH"
    r"|PRAGMA\s+\w+\s*=|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)

MAX_ROWS = 200


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
    # Block mutations
    if _FORBIDDEN_KEYWORDS.search(sql):
        return json.dumps({"error": "Only read-only (SELECT) queries are allowed."})

    # Enforce limit
    row_limit = min(max(1, limit), MAX_ROWS)
    query = sql.rstrip().rstrip(";")
    if "LIMIT" not in query.upper():
        query = f"{query} LIMIT {row_limit}"

    db = _get_db(ctx)
    try:
        async with db.db.execute(query) as cursor:
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = await cursor.fetchall()

        # Convert to list of dicts
        results = []
        for row in rows[:row_limit]:
            record = {}
            for i, col in enumerate(columns):
                val = row[i]
                # Handle non-JSON-serializable types
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

    except Exception as e:
        return json.dumps({"error": str(e)})


def register(mcp):
    mcp.tool()(query_db)
