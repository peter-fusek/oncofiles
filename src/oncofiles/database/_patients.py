"""Patient management mixin — CRUD for patients and token-to-patient resolution."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime
from typing import Any

from oncofiles.models import Patient

logger = logging.getLogger(__name__)


def _row_to_patient(row: Any) -> Patient:
    """Convert a database row to a Patient model."""
    d = dict(row)
    return Patient(
        patient_id=row["patient_id"],
        slug=d.get("slug", ""),
        display_name=row["display_name"],
        caregiver_email=row["caregiver_email"],
        diagnosis_summary=row["diagnosis_summary"],
        is_active=bool(row["is_active"]),
        preferred_lang=row["preferred_lang"] or "sk",
        created_at=(datetime.fromisoformat(row["created_at"]) if row["created_at"] else None),
        updated_at=(datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None),
    )


def _hash_token(token: str) -> str:
    """SHA-256 hash of a bearer token. Never store tokens in plaintext."""
    return hashlib.sha256(token.encode()).hexdigest()


class PatientsMixin:
    """Database mixin for patient management and token resolution."""

    async def get_patient(self, patient_id: str) -> Patient | None:
        """Get a patient by ID."""
        async with self.db.execute(
            "SELECT * FROM patients WHERE patient_id = ?", (patient_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_patient(row) if row else None

    async def list_patients(self, active_only: bool = True) -> list[Patient]:
        """List all patients, optionally filtered to active only."""
        sql = "SELECT * FROM patients"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY created_at"
        async with self.db.execute(sql) as cursor:
            return [_row_to_patient(r) for r in await cursor.fetchall()]

    async def get_patient_by_slug(self, slug: str) -> Patient | None:
        """Get a patient by human-readable slug (e.g. 'q1b')."""
        async with self.db.execute("SELECT * FROM patients WHERE slug = ?", (slug,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_patient(row) if row else None

    async def resolve_patient_id(self, value: str) -> str | None:
        """Resolve a patient identifier (UUID or slug) to a UUID patient_id.

        If *value* looks like a UUID (36 chars with hyphens), look up by patient_id.
        Otherwise, treat it as a slug.  Returns the UUID string or None.
        """
        import re as _re

        _uuid_re = _re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", _re.I
        )
        if _uuid_re.match(value):
            p = await self.get_patient(value)
            return p.patient_id if p else None
        p = await self.get_patient_by_slug(value)
        return p.patient_id if p else None

    async def resolve_default_patient(self) -> str:
        """Return the UUID of the first active patient (fallback for tokenless sessions)."""
        patients = await self.list_patients(active_only=True)
        return patients[0].patient_id if patients else ""

    async def insert_patient(self, patient: Patient) -> Patient:
        """Create a new patient.

        If patient_id is not a UUID, it is treated as a slug and a UUID is generated.
        If slug is empty, the original patient_id value is used as the slug.
        """
        import re as _re
        import uuid as _uuid

        _uuid_re = _re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", _re.I
        )
        pid = patient.patient_id
        slug = patient.slug

        if not _uuid_re.match(pid):
            # Old-style slug passed as patient_id — generate a UUID
            if not slug:
                slug = pid
            pid = str(_uuid.uuid4())
        elif not slug:
            slug = pid  # fallback: use UUID as slug (not ideal but safe)

        async with self.db.execute(
            """INSERT INTO patients
               (patient_id, slug, display_name, caregiver_email, diagnosis_summary,
                is_active, preferred_lang)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                pid,
                slug,
                patient.display_name,
                patient.caregiver_email,
                patient.diagnosis_summary,
                int(patient.is_active),
                patient.preferred_lang,
            ),
        ):
            await self.db.commit()
        return (await self.get_patient(pid)) or patient

    async def update_patient(
        self,
        patient_id: str,
        *,
        display_name: str | None = None,
        caregiver_email: str | None = None,
        diagnosis_summary: str | None = None,
        is_active: bool | None = None,
        preferred_lang: str | None = None,
    ) -> Patient | None:
        """Update patient fields. Only non-None values are updated."""
        updates: list[str] = []
        params: list[Any] = []
        if display_name is not None:
            updates.append("display_name = ?")
            params.append(display_name)
        if caregiver_email is not None:
            updates.append("caregiver_email = ?")
            params.append(caregiver_email)
        if diagnosis_summary is not None:
            updates.append("diagnosis_summary = ?")
            params.append(diagnosis_summary)
        if is_active is not None:
            updates.append("is_active = ?")
            params.append(int(is_active))
        if preferred_lang is not None:
            updates.append("preferred_lang = ?")
            params.append(preferred_lang)
        if not updates:
            return await self.get_patient(patient_id)
        updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
        params.append(patient_id)
        sql = f"UPDATE patients SET {', '.join(updates)} WHERE patient_id = ?"
        async with self.db.execute(sql, params):
            await self.db.commit()
        return await self.get_patient(patient_id)

    # ── Token management ─────────────────────────────────────────────────

    async def resolve_patient_from_token(self, token: str) -> str | None:
        """Resolve a bearer token to a patient_id via SHA-256 hash lookup.

        Returns patient_id if found and active, None otherwise.
        """
        token_hash = _hash_token(token)
        async with self.db.execute(
            """SELECT pt.patient_id FROM patient_tokens pt
               JOIN patients p ON p.patient_id = pt.patient_id
               WHERE pt.token_hash = ? AND pt.is_active = 1 AND p.is_active = 1""",
            (token_hash,),
        ) as cursor:
            row = await cursor.fetchone()
            return row["patient_id"] if row else None

    async def create_patient_token(self, patient_id: str, label: str = "") -> str:
        """Generate a new bearer token for a patient.

        Returns the plaintext token (shown once to the caller).
        Only the SHA-256 hash is stored in the database.
        """
        token = f"onco_{secrets.token_urlsafe(32)}"
        token_hash = _hash_token(token)
        async with self.db.execute(
            """INSERT INTO patient_tokens (patient_id, token_hash, label)
               VALUES (?, ?, ?)""",
            (patient_id, token_hash, label),
        ):
            await self.db.commit()
        logger.info(
            "Created token for patient %s (label=%s, hash=%s...)",
            patient_id,
            label,
            token_hash[:12],
        )
        return token

    async def revoke_patient_token(self, token: str) -> bool:
        """Revoke a token by marking it inactive. Returns True if found."""
        token_hash = _hash_token(token)
        async with self.db.execute(
            "UPDATE patient_tokens SET is_active = 0 WHERE token_hash = ?",
            (token_hash,),
        ) as cursor:
            await self.db.commit()
            return cursor.rowcount > 0

    async def list_patient_tokens(self, patient_id: str) -> list[dict]:
        """List active tokens for a patient (hashes only, never plaintext)."""
        async with self.db.execute(
            """SELECT id, token_hash, label, created_at
               FROM patient_tokens
               WHERE patient_id = ? AND is_active = 1
               ORDER BY created_at""",
            (patient_id,),
        ) as cursor:
            return [
                {
                    "id": r["id"],
                    "token_hash_prefix": r["token_hash"][:12] + "...",
                    "label": r["label"],
                    "created_at": r["created_at"],
                }
                for r in await cursor.fetchall()
            ]

    # ── Patient selection for OAuth (#291) ─────────────────────────────────

    async def get_patient_selection(self, owner_email: str) -> str | None:
        """Get the selected patient_id for an OAuth user, or None."""
        try:
            async with self.db.execute(
                "SELECT patient_id FROM patient_selection WHERE owner_email = ?",
                (owner_email,),
            ) as cursor:
                row = await cursor.fetchone()
                return row["patient_id"] if row else None
        except Exception:
            return None

    async def set_patient_selection(self, owner_email: str, patient_id: str) -> None:
        """Store the OAuth user's preferred patient."""
        await self.db.execute(
            """INSERT INTO patient_selection (owner_email, patient_id, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(owner_email) DO UPDATE SET
                   patient_id = excluded.patient_id,
                   updated_at = excluded.updated_at""",
            (owner_email, patient_id),
        )
        await self.db.commit()

    async def get_patients_for_email(self, owner_email: str) -> list:
        """Get all active patients associated with an owner email."""
        patients = await self.list_patients(active_only=True)
        # Check which patients have OAuth tokens for this email
        patient_ids_for_email: set[str] = set()
        try:
            async with self.db.execute(
                "SELECT patient_id FROM oauth_tokens WHERE owner_email = ?",
                (owner_email,),
            ) as cursor:
                for row in await cursor.fetchall():
                    patient_ids_for_email.add(row["patient_id"])
        except Exception:
            pass
        # Return all active patients (owner has access to all their patients)
        # If we have OAuth mapping, prefer those; otherwise return all active
        if patient_ids_for_email:
            return [p for p in patients if p.patient_id in patient_ids_for_email]
        return patients
