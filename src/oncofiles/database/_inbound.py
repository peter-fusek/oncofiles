"""Patient document e-mail addresses (#950 phase 1, migration 095).

One active row per patient: ``token_hash`` = sha256 hex of the derived token,
never the token or the address. Writers and readers live in
``oncofiles.inbound_email``; every helper takes a keyword-only ``patient_id``
except the webhook's lookup by hash, which is how a mail finds its patient.
"""

from __future__ import annotations

from ._base import DatabaseBase
from ._tenant import require_patient_id


class InboundAddressMixin(DatabaseBase):
    async def get_active_inbound_address(self, *, patient_id: str) -> dict | None:
        """The patient's active row (id, version, created_at), or None."""
        async with self.db.execute(
            """SELECT id, patient_id, version, active, created_at FROM inbound_addresses
               WHERE patient_id = ? AND active = 1""",
            (patient_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def max_inbound_address_version(self, *, patient_id: str) -> int:
        async with self.db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM inbound_addresses WHERE patient_id = ?",
            (patient_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] or 0)

    async def activate_inbound_address(
        self, *, patient_id: str, version: int, token_hash: str
    ) -> None:
        """Make ``version`` the patient's only active address (one transaction).

        Any earlier active row is set inactive first, then the new row is
        inserted; a failure rolls both back, so a patient never ends up with
        two active addresses or none half-written.
        """
        pid = require_patient_id(patient_id)
        try:
            await self.db.execute(
                "UPDATE inbound_addresses SET active = 0 WHERE patient_id = ? AND active = 1",
                (pid,),
            )
            await self.db.execute(
                """INSERT INTO inbound_addresses (patient_id, token_hash, version, active)
                   VALUES (?, ?, ?, 1)""",
                (pid, token_hash, int(version)),
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise

    async def find_inbound_address(self, token_hash: str) -> dict | None:
        """The row whose ``token_hash`` matches (active or not), or None.

        The only lookup without a patient filter: the token IS the routing key.
        """
        async with self.db.execute(
            """SELECT id, patient_id, version, active FROM inbound_addresses
               WHERE token_hash = ?""",
            (token_hash,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def document_md5_exists(self, md5: str, *, patient_id: str) -> bool:
        """True when an active document of the patient has this Drive md5."""
        async with self.db.execute(
            """SELECT 1 FROM documents
               WHERE patient_id = ? AND gdrive_md5 = ? AND deleted_at IS NULL LIMIT 1""",
            (patient_id, md5),
        ) as cursor:
            return await cursor.fetchone() is not None
