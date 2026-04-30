"""Onboarding event trail mixin (#468) — funnel signals for new caregivers.

v5.19 Session 2 ships only `insert_onboarding_event` + `get_onboarding_event`
(plus light `list_onboarding_events_for_patient` for tests). The
`fetch_unnotified` / `mark_notified` helpers used by the daily digest
dispatcher land in v5.20 alongside the T1-T4 hooks.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ._base import DatabaseBase

logger = logging.getLogger(__name__)


class OnboardingEventsMixin(DatabaseBase):
    async def insert_onboarding_event(
        self,
        patient_id: str,
        event_type: str,
        meta: dict[str, Any] | None = None,
    ) -> int | None:
        """Record an onboarding event for a patient.

        Uses INSERT OR IGNORE — for one-time event types
        (created/oauth_ok/folder_set/first_sync/first_ai) a duplicate insert
        for the same (patient_id, event_type) is silently dropped via the
        partial UNIQUE index (migration 070). For repeatable event types
        (stuck_24h/oauth_failure/doc_limit_hit) every call inserts a new row.

        Returns the new row id, or None if an existing row dedupe'd the
        insert. Never raises on dedupe — the caller's hot path
        (api_create_patient, oauth_callback, etc.) must not fail because of
        a notification side-effect.
        """
        meta_json = json.dumps(meta, separators=(",", ":")) if meta else None
        async with self.db.execute(
            """INSERT OR IGNORE INTO onboarding_events
               (patient_id, event_type, meta_json)
               VALUES (?, ?, ?)""",
            (patient_id, event_type, meta_json),
        ) as cursor:
            await self.db.commit()
            if cursor.rowcount == 0:
                return None
            return cursor.lastrowid

    async def get_onboarding_event(
        self,
        patient_id: str,
        event_type: str,
    ) -> dict | None:
        """Return the most-recent onboarding event for a (patient, type) tuple.

        For one-time events there is at most one row; for repeatable events
        this returns the latest by occurred_at.
        """
        async with self.db.execute(
            """SELECT id, patient_id, event_type, occurred_at, meta_json,
                      admin_notified_at
               FROM onboarding_events
               WHERE patient_id = ? AND event_type = ?
               ORDER BY occurred_at DESC
               LIMIT 1""",
            (patient_id, event_type),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_onboarding_events_for_patient(
        self,
        patient_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """Return all onboarding events for a patient (newest first)."""
        async with self.db.execute(
            """SELECT id, patient_id, event_type, occurred_at, meta_json,
                      admin_notified_at
               FROM onboarding_events
               WHERE patient_id = ?
               ORDER BY occurred_at DESC
               LIMIT ?""",
            (patient_id, limit),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]
