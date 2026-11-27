"""The predicates of the #950 holding jobs, shared by production and the dry run.

The holding mover (``holding_move``), the reader (``holding_extract``) and the
"ready" e-mail (``trial_funnel``) select their work with the SQL below, through
the ``database._trials`` helpers. ``oncofiles.job_dryrun`` (the #999 "check
before" tool) runs the SAME statements on a read-only copy of the production DB,
so its counts cannot drift from what the jobs do.

This module imports nothing from ``oncofiles`` (no config, no DB driver): the
dry run loads it without touching the environment or creating any directory.
Plain ``?`` parameters, valid for ``sqlite3`` and ``aiosqlite`` alike.
"""

from __future__ import annotations

# Trials not converted yet with a connected Drive folder (the mover's work list).
TRIALS_TO_CONVERT_SQL = (
    "SELECT t.patient_id FROM trial_accounts t "
    "WHERE t.converted_at IS NULL AND EXISTS (SELECT 1 FROM oauth_tokens o "
    "  WHERE o.patient_id = t.patient_id AND o.provider = 'google' "
    "  AND COALESCE(o.gdrive_folder_id, '') != '') "
    "ORDER BY t.id"
)

# One patient's active held rows without a verified Drive copy (param: patient_id).
HOLDING_ROWS_TO_MOVE_SQL = (
    "SELECT id, file_id, filename, original_filename, mime_type, structured_metadata "
    "FROM documents WHERE patient_id = ? AND file_id LIKE 'holding:%' "
    "AND deleted_at IS NULL AND json_valid(structured_metadata) "
    "AND json_extract(structured_metadata, '$.holding.moved_to_drive') IS NULL "
    "ORDER BY id"
)

# One patient's active held rows that already have a Drive copy (param: patient_id).
HOLDING_MOVED_COUNT_SQL = (
    "SELECT COUNT(*) FROM documents WHERE patient_id = ? AND file_id LIKE 'holding:%' "
    "AND deleted_at IS NULL AND json_valid(structured_metadata) "
    "AND json_extract(structured_metadata, '$.holding.moved_to_drive') IS NOT NULL"
)

# FROM + WHERE of a held document the reader may read now
# (params: now, stale_before). Callers append the column list / ORDER BY.
HOLDING_DUE_FOR_READING_FROM = (
    "FROM documents d JOIN trial_accounts t ON t.patient_id = d.patient_id "
    "WHERE d.file_id LIKE 'holding:%' AND d.deleted_at IS NULL "
    "AND t.trial_ends_at > ? AND json_valid(d.structured_metadata) "
    # #950 phase 3: a document already copied into Drive is read by the
    # curated pipeline there, never twice on the shared model
    "AND json_extract(d.structured_metadata, '$.holding.moved_to_drive') IS NULL "
    "AND (json_extract(d.structured_metadata, '$.holding.extract') = 'pending' "
    "  OR (json_extract(d.structured_metadata, '$.holding.extract') = 'reading' "
    "      AND COALESCE(json_extract(d.structured_metadata, '$.holding.claimed_at'), '')"
    "          < ?))"
)

# Moved held rows whose curated mirror row now exists: (held id, patient id,
# mirror id). The mover flags them ``holding.superseded_by``.
SUPERSEDED_CANDIDATES_SQL = (
    "SELECT h.id, h.patient_id, MIN(m.id) FROM documents h JOIN documents m "
    "  ON m.patient_id = h.patient_id AND m.deleted_at IS NULL "
    "  AND m.file_id LIKE 'gdrive:%' AND m.id != h.id "
    "  AND (m.file_id = 'gdrive:' || json_extract(h.structured_metadata, "
    "         '$.holding.moved_to_drive.drive_id') "
    "       OR (json_valid(m.structured_metadata) "
    "           AND json_extract(m.structured_metadata, '$.curated.source_md5') "
    "               = json_extract(h.structured_metadata, '$.holding.md5'))) "
    "WHERE h.file_id LIKE 'holding:%' AND h.deleted_at IS NULL "
    "AND json_valid(h.structured_metadata) "
    "AND json_extract(h.structured_metadata, '$.holding.moved_to_drive') IS NOT NULL "
    "AND json_extract(h.structured_metadata, '$.holding.superseded_by') IS NULL "
    "GROUP BY h.id, h.patient_id"
)

# Trials due for the one "ready" e-mail: (patient id, done count).
TRIALS_AWAITING_READY_MAIL_SQL = (
    "SELECT t.patient_id, "
    "  SUM(CASE WHEN json_extract(d.structured_metadata, '$.holding.extract') = 'done' "
    "      THEN 1 ELSE 0 END) AS done, "
    "  SUM(CASE WHEN json_extract(d.structured_metadata, '$.holding.extract') "
    "      IN ('pending', 'reading') "
    "      AND json_extract(d.structured_metadata, '$.holding.moved_to_drive') IS NULL "
    "      THEN 1 ELSE 0 END) AS open "
    "FROM trial_accounts t JOIN documents d ON d.patient_id = t.patient_id "
    "  AND d.file_id LIKE 'holding:%' AND d.deleted_at IS NULL "
    "  AND json_valid(d.structured_metadata) "
    "WHERE t.ready_mail_sent_at IS NULL "
    # a mail still being filed belongs to the same first batch
    "AND NOT EXISTS (SELECT 1 FROM inbound_jobs j WHERE j.patient_id = t.patient_id "
    "  AND j.state IN ('pending', 'processing')) "
    "GROUP BY t.patient_id HAVING done > 0 AND open = 0 ORDER BY MIN(t.id)"
)

# The patient's first caregiver address column (the ready mail's recipient).
PATIENT_CAREGIVER_SQL = "SELECT caregiver_email FROM patients WHERE patient_id = ?"

# Curated mirror rows of a patient (``db.curated_index_state``; param: patient_id).
CURATED_MIRROR_ROWS_SQL = (
    "SELECT id, gdrive_id, structured_metadata, deleted_at FROM documents "
    "WHERE patient_id = ? AND file_id LIKE 'gdrive:%'"
)


def is_foreign_mirror(meta: dict) -> bool:
    """#777: a Drive-only row of the legacy sync/upload path shares the
    ``gdrive:`` prefix but is not a curated mirror row. An interrupted curated
    insert has no metadata yet, so it still counts as the indexer's own."""
    return bool(meta) and "curated" not in meta and meta.get("source") != "curated"


def filed_md5s(state: dict[str, dict]) -> dict[str, str]:
    """``{source md5: Drive id}`` of the filed tree from
    ``db.curated_index_state`` entries: the md5s the mover counts as already
    present (never a deleted or foreign row)."""
    out: dict[str, str] = {}
    for drive_id, s in state.items():
        md5 = s.get("source_md5")
        if md5 and not s.get("deleted") and not s.get("foreign"):
            out.setdefault(md5, drive_id)
    return out


def first_recipient(raw: str | None) -> str:
    """The first address of a ``caregiver_email`` value (the ready mail's ``to``)."""
    return str(raw or "").split(",", 1)[0].strip()
