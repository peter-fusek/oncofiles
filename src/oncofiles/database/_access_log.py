"""Dashboard access audit trail (#868, migration 090).

Operator log, not a patient-scoped table: one short INSERT per sign-in, ACL
refusal or admin cross-patient read. The caller is a keyed hash, never the
e-mail. Writers go through ``oncofiles.dashboard_audit.record``, which never
raises. Retention (maintainer decision 2026-11-16): rows older than 90 days
are deleted by the daily ``dashboard_access_log_retention`` job via
``dashboard_audit.purge_expired_access_log``, the only DELETE on this table
(allowlisted in ``tests/test_no_automatic_deletion.py``, #734).
"""

from __future__ import annotations

from ._base import DatabaseBase

# vocab domain ``dashboard_access_event`` (oncofiles.vocab).
DASHBOARD_ACCESS_EVENTS = ("sign_in", "patient_read", "acl_refused")


class DashboardAccessLogMixin(DatabaseBase):
    async def insert_dashboard_access(
        self,
        *,
        event: str,
        caller_hash: str | None,
        is_admin: bool,
        target_patient_id: str | None,
        route: str,
    ) -> int:
        """Append one audit row and commit. Raises ValueError on an unknown event."""
        if event not in DASHBOARD_ACCESS_EVENTS:
            raise ValueError(f"unknown dashboard access event {event!r}")
        async with self.db.execute(
            """INSERT INTO dashboard_access_log
               (event, caller_hash, is_admin, target_patient_id, route)
               VALUES (?, ?, ?, ?, ?)""",
            (event, caller_hash, 1 if is_admin else 0, target_patient_id, route[:120]),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id

    async def list_dashboard_access(self, *, limit: int = 100) -> list[dict]:
        """Newest rows first (operator / tests only, no MCP tool)."""
        async with self.db.execute(
            """SELECT id, event, caller_hash, is_admin, target_patient_id, route, created_at
               FROM dashboard_access_log ORDER BY id DESC LIMIT ?""",
            (limit,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]
