"""The MCP OAuth token cleanup predicate, shared by production and the dry run.

``persistent_oauth.delete_expired_oauth_tokens`` (run daily by the
``oauth_token_cleanup`` loop, ``oncofiles.oauth_cleanup_loop``) deletes the
``mcp_oauth_tokens`` rows matched by :data:`EXPIRED_WHERE` with the cutoff of
:func:`cutoff`; ``oncofiles.job_dryrun oauth_token_cleanup`` (the #999 "check
before" tool) counts the SAME rows on a read-only copy of the production DB,
so its numbers cannot drift from what the job deletes.

``expires_at`` is INTEGER epoch seconds; rows without an expiry are kept. A
row goes once it expired more than :data:`EXPIRED_TOKEN_GRACE_S` ago.

This module imports nothing from ``oncofiles`` (no config, no DB driver): the
dry run loads it without touching the environment.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Expired MCP OAuth rows are kept this long before the cleanup removes them.
EXPIRED_TOKEN_GRACE_S = 30 * 86400
# Rows that expired before the cutoff (param: the integer cutoff).
EXPIRED_WHERE = "expires_at IS NOT NULL AND expires_at < ?"


def cutoff(now: datetime) -> int:
    """The integer epoch cutoff for ``now``: expired more than the grace ago."""
    return int(now.astimezone(UTC).timestamp()) - EXPIRED_TOKEN_GRACE_S
