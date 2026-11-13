"""Curated-folder runs and nightly settings (#573). Every read is patient-scoped."""

from __future__ import annotations

import json

from ._base import DatabaseBase
from ._tenant import require_patient_id

# background jobs persisted in curated_runs that are not curated-folder runs
# (re-OCR, verify-only re-read, clinical extraction #726, extraction scorecard
# #773): never listed as runs and never satisfy the preview gate
_NOT_RUNS = "trigger NOT IN ('reocr', 'reread', 'clinical', 'scorecard')"


class CuratedMixin(DatabaseBase):
    async def insert_curated_run(self, *, patient_id: str, dry_run: bool, trigger: str) -> int:
        await self.db.execute(
            "INSERT INTO curated_runs (patient_id, dry_run, trigger) VALUES (?, ?, ?)",
            (patient_id, 1 if dry_run else 0, trigger),
        )
        await self.db.commit()
        async with self.db.execute(
            "SELECT id FROM curated_runs WHERE patient_id = ? ORDER BY id DESC LIMIT 1",
            (patient_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row["id"] if isinstance(row, dict) else row[0]

    async def finish_curated_run(
        self, run_row_id: int, *, patient_id: str, status: str, message: str, report: dict
    ) -> None:
        await self.db.execute(
            """UPDATE curated_runs
               SET status = ?, message = ?, run_id = ?, report_json = ?,
                   finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
               WHERE id = ? AND patient_id = ?""",
            (
                status,
                (message or "")[:1000],
                report.get("run_id"),
                json.dumps(report, ensure_ascii=False, default=str),
                run_row_id,
                patient_id,
            ),
        )
        await self.db.commit()

    async def mark_interrupted_curated_runs(self) -> int:
        """Startup housekeeping (#581): rows still 'running' belong to a dead process.

        Runs execute in-process and in-memory state dies with the process, so at
        startup no row can legitimately be running. Deliberately all patients.
        """
        cur = await self.db.execute(
            """UPDATE curated_runs
               SET status = 'interrupted',
                   message = 'process stopped during the run (deploy or restart)',
                   finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
               WHERE status = 'running'"""
        )
        await self.db.commit()
        return getattr(cur, "rowcount", 0) or 0

    async def list_curated_runs(self, *, patient_id: str, limit: int = 20) -> list[dict]:
        """Newest first, without report_json (cheap list)."""
        async with self.db.execute(
            """SELECT id, run_id, trigger, dry_run, status, message, started_at, finished_at
               FROM curated_runs WHERE patient_id = ? AND """
            + _NOT_RUNS
            + """
               ORDER BY id DESC LIMIT ?""",
            (patient_id, max(1, min(limit, 200))),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def get_curated_run(self, run_row_id: int, *, patient_id: str) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM curated_runs WHERE id = ? AND patient_id = ?",
            (run_row_id, patient_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        out = dict(row)
        out["report"] = json.loads(out.pop("report_json") or "null")
        return out

    async def get_last_curated_run(
        self, *, patient_id: str, dry_run: bool | None = None, finished: bool = True
    ) -> dict | None:
        where = ["patient_id = ?", _NOT_RUNS]
        params: list = [patient_id]
        if dry_run is not None:
            where.append("dry_run = ?")
            params.append(1 if dry_run else 0)
        if finished:
            where.append("finished_at IS NOT NULL")
        async with self.db.execute(
            f"SELECT id FROM curated_runs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 1",
            tuple(params),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        rid = row["id"] if isinstance(row, dict) else row[0]
        return await self.get_curated_run(rid, patient_id=patient_id)

    async def get_curated_nightly(self, *, patient_id: str) -> bool:
        async with self.db.execute(
            "SELECT nightly_enabled FROM curated_settings WHERE patient_id = ?", (patient_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return False
        return bool(row["nightly_enabled"] if isinstance(row, dict) else row[0])

    async def set_curated_nightly(self, *, patient_id: str, enabled: bool, updated_by: str) -> None:
        patient_id = require_patient_id(patient_id)
        await self.db.execute(
            """INSERT INTO curated_settings (patient_id, nightly_enabled, updated_by)
               VALUES (?, ?, ?)
               ON CONFLICT(patient_id) DO UPDATE SET
                 nightly_enabled = excluded.nightly_enabled,
                 updated_by = excluded.updated_by,
                 updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')""",
            (patient_id, 1 if enabled else 0, updated_by),
        )
        await self.db.commit()

    async def list_curated_nightly_patients(self) -> list[str]:
        """System-level (scheduler) read: ids only, no patient data."""
        async with self.db.execute(
            "SELECT patient_id FROM curated_settings WHERE nightly_enabled = 1"
        ) as cursor:
            rows = await cursor.fetchall()
        return [r["patient_id"] if isinstance(r, dict) else r[0] for r in rows]

    async def get_last_scorecard(
        self, *, patient_id: str, label_prefix: str | None = None, before_id: int | None = None
    ) -> dict | None:
        """The newest finished, ok extraction scorecard (#773) of one patient,
        optionally only one whose label starts with ``label_prefix`` and older
        than row ``before_id``. Its report holds aggregated counts only."""
        where = ["patient_id = ?", "trigger = 'scorecard'", "status = 'ok'",
                 "finished_at IS NOT NULL"]  # fmt: skip
        params: list = [patient_id]
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
        if label_prefix:
            escaped = label_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("json_extract(report_json, '$.label') LIKE ? ESCAPE '\\'")
            params.append(escaped + "%")
        async with self.db.execute(
            f"SELECT id FROM curated_runs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 1",
            tuple(params),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return await self.get_curated_run(
            row["id"] if isinstance(row, dict) else row[0], patient_id=patient_id
        )

    async def get_last_reocr_job(self, *, patient_id: str) -> dict | None:
        """The newest re-OCR or verify-only re-read job row (#667, #678) with its
        report: survives a restart, and a job cut by one is reported
        'interrupted' by the startup sweep. Both kinds share one job slot."""
        async with self.db.execute(
            "SELECT id FROM curated_runs WHERE patient_id = ? "
            "AND trigger IN ('reocr', 'reread') ORDER BY id DESC LIMIT 1",
            (patient_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return await self.get_curated_run(
            row["id"] if isinstance(row, dict) else row[0], patient_id=patient_id
        )
