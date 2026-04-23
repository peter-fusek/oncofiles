"""Operational: agent state, activity log, OAuth tokens."""

from __future__ import annotations

from datetime import date

from oncofiles.models import (
    ActivityLogEntry,
    ActivityLogQuery,
    AgentState,
    OAuthToken,
)

from ._converters import _row_to_activity_log, _row_to_agent_state, _row_to_oauth_token

# "key" is a reserved word — always quote it in SQL and use aliased SELECT
_AGENT_STATE_SELECT = (
    'SELECT id, agent_id, "key" AS state_key, value, patient_id,'
    " created_at, updated_at FROM agent_state"
)


class OperationalMixin:
    """Agent state, activity log, and OAuth token operations."""

    # ── Agent state (#32) ────────────────────────────────────────────────

    async def set_agent_state(self, state: AgentState) -> AgentState:
        """Upsert an agent state key-value pair. Returns the saved state."""
        from oncofiles.database._base import retry_on_hrana_conflict

        pid = getattr(state, "patient_id", "") or ""

        async def _do_upsert():
            await self.db.execute(
                """
                INSERT INTO agent_state (patient_id, agent_id, "key", value, updated_at)
                VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                ON CONFLICT(patient_id, agent_id, "key") DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (pid, state.agent_id, state.key, state.value),
            )
            await self.db.commit()

        await retry_on_hrana_conflict(_do_upsert, label="set_agent_state")

        async with self.db.execute(
            _AGENT_STATE_SELECT + ' WHERE patient_id = ? AND agent_id = ? AND "key" = ?',
            (pid, state.agent_id, state.key),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_agent_state(row)

    async def get_agent_state(
        self, key: str, agent_id: str = "oncoteam", *, patient_id: str = ""
    ) -> AgentState | None:
        """Get a single agent state value by key, scoped by patient_id."""
        async with self.db.execute(
            _AGENT_STATE_SELECT + ' WHERE patient_id = ? AND agent_id = ? AND "key" = ?',
            (patient_id, agent_id, key),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_agent_state(row) if row else None

    async def list_agent_states(
        self, agent_id: str = "oncoteam", *, patient_id: str = ""
    ) -> list[AgentState]:
        """List all state keys for an agent, scoped by patient_id."""
        async with self.db.execute(
            _AGENT_STATE_SELECT + ' WHERE patient_id = ? AND agent_id = ? ORDER BY "key"',
            (patient_id, agent_id),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_agent_state(r) for r in rows]

    # ── Activity log (#38) ──────────────────────────────────────────────

    async def insert_activity_log(self, entry: ActivityLogEntry) -> ActivityLogEntry:
        """Append an activity log entry (immutable)."""
        from oncofiles.database._base import retry_on_hrana_conflict

        _last_rowid: list[int] = []

        async def _do_insert():
            cursor = await self.db.execute(
                """
                INSERT INTO activity_log
                    (session_id, agent_id, tool_name, input_summary, output_summary,
                     duration_ms, status, error_message, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.session_id,
                    entry.agent_id,
                    entry.tool_name,
                    entry.input_summary,
                    entry.output_summary,
                    entry.duration_ms,
                    entry.status,
                    entry.error_message,
                    entry.tags,
                ),
            )
            await self.db.commit()
            _last_rowid.clear()
            _last_rowid.append(cursor.lastrowid)

        await retry_on_hrana_conflict(_do_insert, label="insert_activity_log")
        entry.id = _last_rowid[0] if _last_rowid else None
        return entry

    async def search_activity_log(self, query: ActivityLogQuery) -> list[ActivityLogEntry]:
        """Search activity log with filters."""
        conditions: list[str] = []
        params: list[str | int] = []

        if query.session_id:
            conditions.append("session_id = ?")
            params.append(query.session_id)
        if query.agent_id:
            conditions.append("agent_id = ?")
            params.append(query.agent_id)
        if query.tool_name:
            conditions.append("tool_name = ?")
            params.append(query.tool_name)
        if query.status:
            conditions.append("status = ?")
            params.append(query.status)
        if query.date_from:
            conditions.append("created_at >= ?")
            params.append(query.date_from.isoformat())
        if query.date_to:
            conditions.append("created_at <= ?")
            params.append(query.date_to.isoformat() + "T23:59:59Z")
        if query.text:
            like_param = f"%{query.text}%"
            conditions.append("(input_summary LIKE ? OR output_summary LIKE ?)")
            params.extend([like_param, like_param])

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM activity_log WHERE {where} ORDER BY created_at DESC LIMIT ?"
        params.append(query.limit)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_activity_log(r) for r in rows]

    async def get_activity_stats(
        self,
        session_id: str | None = None,
        agent_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[dict]:
        """Get aggregated activity counts grouped by tool_name and status."""
        conditions: list[str] = []
        params: list[str | int] = []

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from.isoformat())
        if date_to:
            conditions.append("created_at <= ?")
            params.append(date_to.isoformat() + "T23:59:59Z")

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = (
            f"SELECT tool_name, status, COUNT(*) as count, "
            f"AVG(duration_ms) as avg_duration_ms "
            f"FROM activity_log WHERE {where} "
            f"GROUP BY tool_name, status ORDER BY count DESC"
        )

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_activity_timeline(self, hours: int = 24) -> list[ActivityLogEntry]:
        """Get recent activity log entries (last N hours)."""
        async with self.db.execute(
            """
            SELECT * FROM activity_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            ORDER BY created_at DESC LIMIT 200
            """,
            (f"-{hours} hours",),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_activity_log(r) for r in rows]

    # ── OAuth tokens (#12) ─────────────────────────────────────────────

    async def upsert_oauth_token(self, token: OAuthToken) -> OAuthToken:
        """Insert or update OAuth tokens for a user/provider pair."""
        await self.db.execute(
            """
            INSERT INTO oauth_tokens
                (patient_id, provider, access_token, refresh_token, token_expiry,
                 gdrive_folder_id, gdrive_folder_name, owner_email, granted_scopes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(patient_id, provider) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                token_expiry = excluded.token_expiry,
                gdrive_folder_id = excluded.gdrive_folder_id,
                gdrive_folder_name = COALESCE(
                    excluded.gdrive_folder_name, oauth_tokens.gdrive_folder_name),
                owner_email = COALESCE(excluded.owner_email, oauth_tokens.owner_email),
                granted_scopes = excluded.granted_scopes,
                updated_at = excluded.updated_at
            """,
            (
                token.patient_id,
                token.provider,
                token.access_token,
                token.refresh_token,
                token.token_expiry.isoformat() if token.token_expiry else None,
                token.gdrive_folder_id,
                token.gdrive_folder_name,
                token.owner_email,
                token.granted_scopes,
            ),
        )
        await self.db.commit()
        return await self.get_oauth_token(token.patient_id, token.provider)

    async def get_oauth_token(self, patient_id: str, provider: str = "google") -> OAuthToken | None:
        """Get OAuth tokens for a user/provider pair."""
        async with self.db.execute(
            "SELECT * FROM oauth_tokens WHERE patient_id = ? AND provider = ?",
            (patient_id, provider),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_oauth_token(row) if row else None

    async def update_oauth_folder(
        self,
        patient_id: str,
        provider: str,
        folder_id: str,
        folder_name: str | None = None,
    ) -> None:
        """Set the GDrive folder ID (and optionally the display name) for a user's OAuth token."""
        await self.db.execute(
            "UPDATE oauth_tokens SET gdrive_folder_id = ?, gdrive_folder_name = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE patient_id = ? AND provider = ?",
            (folder_id, folder_name, patient_id, provider),
        )
        await self.db.commit()

    async def update_oauth_owner_email(
        self, patient_id: str, provider: str, owner_email: str
    ) -> None:
        """Store the GDrive folder owner's email for permission sharing."""
        await self.db.execute(
            "UPDATE oauth_tokens SET owner_email = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE patient_id = ? AND provider = ?",
            (owner_email, patient_id, provider),
        )
        await self.db.commit()

    # ── Sync history ──────────────────────────────────────────────────────

    async def close_stale_syncs(self, *, stale_after_minutes: int = 10) -> int:
        """Mark truly-stuck 'running' sync records as timed out.

        Only rows whose started_at is older than stale_after_minutes are closed.
        Prevents a healthy concurrent sync from being flipped to 'timeout' just
        because a new sync started (#437).
        """
        cursor = await self.db.execute(
            """
            UPDATE sync_history SET
                status = 'timeout',
                finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                error_message = 'Marked as timed out (stale running record)'
            WHERE status = 'running'
              AND started_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            """,
            (f"-{stale_after_minutes} minutes",),
        )
        await self.db.commit()
        return cursor.rowcount

    async def insert_sync_history(self, trigger: str = "scheduled", *, patient_id: str = "") -> int:
        """Start a sync history record. Returns the row ID."""
        # Clean up any stale 'running' records first (isolated — don't block insert)
        import logging

        try:
            stale = await self.close_stale_syncs()
            if stale:
                logging.getLogger(__name__).info(
                    "sync_history: closed %d stale running records", stale
                )
        except Exception:
            logging.getLogger(__name__).warning(
                "sync_history: failed to close stale records", exc_info=True
            )
        await self.db.execute(
            """
            INSERT INTO sync_history (started_at, sync_trigger, status, patient_id)
            VALUES (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?, 'running', ?)
            """,
            (trigger, patient_id),
        )
        await self.db.commit()
        # lastrowid is unreliable on Turso — fetch the ID explicitly
        async with self.db.execute(
            "SELECT id FROM sync_history ORDER BY id DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return row["id"] if isinstance(row, dict) else row[0]

    async def complete_sync_history(
        self,
        sync_id: int,
        *,
        status: str = "completed",
        duration_s: float = 0.0,
        from_new: int = 0,
        from_updated: int = 0,
        from_errors: int = 0,
        to_exported: int = 0,
        to_organized: int = 0,
        to_renamed: int = 0,
        to_errors: int = 0,
        error_message: str | None = None,
        stats_json: str | None = None,
    ) -> None:
        """Complete a sync history record with results."""
        await self.db.execute(
            """
            UPDATE sync_history SET
                finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                status = ?,
                duration_s = ?,
                from_gdrive_new = ?,
                from_gdrive_updated = ?,
                from_gdrive_errors = ?,
                to_gdrive_exported = ?,
                to_gdrive_organized = ?,
                to_gdrive_renamed = ?,
                to_gdrive_errors = ?,
                error_message = ?,
                stats_json = ?
            WHERE id = ?
            """,
            (
                status,
                duration_s,
                from_new,
                from_updated,
                from_errors,
                to_exported,
                to_organized,
                to_renamed,
                to_errors,
                error_message,
                stats_json,
                sync_id,
            ),
        )
        await self.db.commit()

    async def get_sync_history(
        self, limit: int = 20, *, patient_id: str | None = None
    ) -> list[dict]:
        """Get recent sync history entries, optionally filtered by patient.

        patient_id semantics (#476 hardened): None = unscoped (admin/audit);
        any string (incl. "") = scoped — empty string matches 0 rows
        rather than silently bleeding cross-patient.
        """
        if patient_id is not None:
            async with self.db.execute(
                "SELECT * FROM sync_history WHERE patient_id = ? ORDER BY started_at DESC LIMIT ?",
                (patient_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self.db.execute(
                "SELECT * FROM sync_history ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_sync_stats_summary(self, *, patient_id: str | None = None) -> dict:
        """Get aggregate sync statistics for system_health.

        patient_id semantics (#476 hardened): None = unscoped (admin/audit);
        any string (incl. "") = scoped — empty string matches 0 rows.
        """
        pid_clause = "AND patient_id = ?" if patient_id is not None else ""
        params = [patient_id] if patient_id is not None else []
        async with self.db.execute(
            f"""
            SELECT
                COUNT(*) as total_syncs,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as successful,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                ROUND(AVG(duration_s), 1) as avg_duration_s,
                MAX(started_at) as last_sync_at,
                SUM(from_gdrive_new) as total_imported,
                SUM(from_gdrive_errors + to_gdrive_errors) as total_errors
            FROM sync_history
            WHERE started_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')
            {pid_clause}
            """,
            params,
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}
