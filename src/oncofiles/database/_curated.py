"""Curated-folder runs and nightly settings (#573). Every read is patient-scoped."""

from __future__ import annotations

import json

from ._base import DatabaseBase
from ._tenant import require_patient_id

# background jobs persisted in curated_runs that are not curated-folder runs
# (re-OCR, verify-only re-read, clinical extraction #726, LLM lab write #714, scorecard
# #773): never listed as runs and never satisfy the preview gate
_NOT_RUNS = "trigger NOT IN ('reocr', 'reread', 'clinical', 'scorecard', 'lab')"


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

    async def list_curated_run_activity(self, *, patient_id: str, limit: int = 30) -> list[dict]:
        """Real curated runs of one patient, newest first, as counts only.

        For the dashboard activity log: no dry runs or previews and none of the
        non-run jobs (``_NOT_RUNS``). The counts are the ones
        ``server._run_summary`` derives from a report (documents, filed,
        review, not_processed), computed inside SQLite, so neither the report
        (file names, reasons) nor the run message ever leaves the database.
        A row without a readable report (still running, interrupted) counts 0.
        """
        report = "CASE WHEN json_valid(report_json) THEN report_json END"
        # CASE keeps json_extract away from an entry that is not an object.
        result = "CASE WHEN d.type = 'object' THEN json_extract(d.value, '$.result') END"
        async with self.db.execute(
            f"""SELECT id, trigger, status, started_at, finished_at,
                   COALESCE(json_array_length({report}, '$.documents'), 0) AS documents,
                   (SELECT COUNT(*) FROM json_each({report}, '$.documents') AS d
                     WHERE {result} = 'filed') AS filed,
                   (SELECT COUNT(*) FROM json_each({report}, '$.documents') AS d
                     WHERE {result} = 'review_needed') AS review,
                   COALESCE(json_array_length({report}, '$.not_processed'), 0) AS not_processed
               FROM curated_runs
               WHERE patient_id = ? AND dry_run = 0 AND {_NOT_RUNS}
               ORDER BY id DESC LIMIT ?""",
            (patient_id, max(1, min(limit, 100))),
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
        report = json.loads(out.pop("report_json") or "null")
        # #805: re-OCR rows written before the fix hold "CODE value" strings in
        # documents[*].lost; every reader (MCP tools, admin routes) gets codes only
        from oncofiles.curated.reocr import redact_report

        out["report"] = redact_report(report)
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

    async def curated_import_state(self, *, patient_id: str) -> dict:
        """First-import state for the fair scheduler (#855). Ids/flags only.

        ``started``: any real run exists. ``done``: a real run ended ok with
        nothing left for a later run (``inbox_drained``; rows written before
        #855 lack it and count when their ``not_processed`` list is empty).
        ``enrolled_at``: ``curated_settings.updated_at``, the FIFO key.
        """
        runs = "patient_id = ? AND dry_run = 0 AND " + _NOT_RUNS
        async with self.db.execute(
            f"""SELECT
                 EXISTS(SELECT 1 FROM curated_runs WHERE {runs}) AS started,
                 EXISTS(SELECT 1 FROM curated_runs WHERE {runs}
                        AND status = 'ok' AND finished_at IS NOT NULL
                        AND COALESCE(json_extract(report_json, '$.inbox_drained'),
                                     json_array_length(report_json, '$.not_processed') = 0)
                            = 1) AS done,
                 (SELECT updated_at FROM curated_settings WHERE patient_id = ?) AS enrolled_at""",
            (patient_id, patient_id, patient_id),
        ) as cursor:
            row = dict(await cursor.fetchone())
        return {
            "started": bool(row["started"]),
            "done": bool(row["done"]),
            "enrolled_at": row["enrolled_at"],
        }

    async def curated_model_pages_since(self, *, patient_id: str, since: str) -> int:
        """Pages the local model OCR'd in this patient's runs (dry and real)
        started at or after ``since`` (UTC, ``%Y-%m-%dT%H:%M:%SZ``) — #855."""
        async with self.db.execute(
            "SELECT COALESCE(SUM(json_extract(report_json, '$.model_pages')), 0) "
            "FROM curated_runs WHERE patient_id = ? AND started_at >= ? AND " + _NOT_RUNS,
            (patient_id, since),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] or 0)

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
