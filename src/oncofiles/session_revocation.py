"""Server-side revocation for dashboard session tokens (#510).

Each issued session token now carries a random `tid` (token id, 16 hex
chars) in its payload. POST /api/logout adds the tid here; the
_verify_session_token path rejects any tid present in the in-memory set.

Persistence layer (Turso `session_revocations` table) survives restarts —
on startup `load_from_db()` rehydrates the in-memory set from rows whose
expires_at is still in the future. Stale rows (past expiry) are pruned
opportunistically by `purge_expired()` so the set doesn't grow without
bound.

Lookup is in-memory + sync so `_verify_session_token` (called on every
authenticated dashboard request) doesn't pay a DB round-trip.
"""

from __future__ import annotations

import logging
import secrets
import time

logger = logging.getLogger(__name__)

# Module-global set of revoked tids. Populated at startup from the DB and
# updated whenever /api/logout is called. Thread-safety: writes happen on
# the asyncio event loop only (no other reader/writer races).
_REVOKED: dict[str, int] = {}  # tid -> expires_at


def make_tid() -> str:
    """Generate a fresh 16-hex-char token id for inclusion in a session token."""
    return secrets.token_hex(8)


def is_revoked(tid: str) -> bool:
    """Synchronous check — used inside _verify_session_token's hot path."""
    return tid in _REVOKED


def _add_to_memory(tid: str, expires_at: int) -> None:
    _REVOKED[tid] = expires_at


def _purge_expired_inplace() -> int:
    now = int(time.time())
    expired = [t for t, exp in _REVOKED.items() if exp <= now]
    for t in expired:
        del _REVOKED[t]
    return len(expired)


async def revoke(db, tid: str, expires_at: int) -> None:
    """Persist a tid revocation to Turso and add it to the in-memory set.

    `expires_at` is the original session token's expiry (UNIX seconds) — we
    keep the row at least until that point so even after a restart the
    revocation survives, then a background purge removes it.
    """
    if not tid:
        return
    _add_to_memory(tid, expires_at)
    await db.execute(
        """
        INSERT OR REPLACE INTO session_revocations (tid, revoked_at, expires_at)
        VALUES (?, ?, ?)
        """,
        (tid, int(time.time()), int(expires_at)),
    )
    await db.commit()


async def load_from_db(db) -> int:
    """Rehydrate the in-memory revocation set from Turso at startup.

    Returns the number of rows loaded. Stale rows (expires_at < now) are
    skipped — they cannot block any still-valid token.
    """
    now = int(time.time())
    async with db.execute(
        "SELECT tid, expires_at FROM session_revocations WHERE expires_at > ?",
        (now,),
    ) as cursor:
        rows = await cursor.fetchall()
    _REVOKED.clear()
    for row in rows:
        tid = row["tid"] if isinstance(row, dict) else row[0]
        exp = row["expires_at"] if isinstance(row, dict) else row[1]
        _REVOKED[tid] = int(exp)
    logger.info("Loaded %d active session revocations from DB", len(_REVOKED))
    return len(_REVOKED)


async def purge_expired(db) -> int:
    """Delete revocation rows past their natural session expiry.

    Returns rows deleted. Safe to call on every startup or via a periodic
    sweep — once a tid's HMAC-expiry has passed, the natural rejection in
    _verify_session_token already kicks in, so the row is no longer
    load-bearing.
    """
    in_memory = _purge_expired_inplace()
    now = int(time.time())
    await db.execute(
        "DELETE FROM session_revocations WHERE expires_at <= ?",
        (now,),
    )
    await db.commit()
    return in_memory


def _reset_for_tests() -> None:
    """Clear in-memory state. Used by test fixtures only."""
    _REVOKED.clear()
