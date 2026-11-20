"""The named ``patient_id`` column default of the single-patient era (#704).

Migration 029 added ``patient_id`` to the first tables with a real given name
as the column DEFAULT. Rows written without a patient still carry that value,
so code has to know it (the write guard rejects it, the activity-log backfill
matches it), but this public repo should not repeat it as a literal. It is
read from the applied migration instead, once, at import.

Only stdlib and ``_base.MIGRATIONS_DIR`` are imported here, so any module of
the package can import this one without a cycle.
"""

from __future__ import annotations

import re

from ._base import MIGRATIONS_DIR

MIGRATION_029 = "029_patient_id_columns.sql"
_DEFAULT_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+patient_id\b[^;]*?\bDEFAULT\s+'([^']*)'",
    re.IGNORECASE,
)


def migration_029_patient_id_default() -> str:
    """The ``patient_id`` column default every table got in migration 029.

    Raises ``RuntimeError`` when the migration cannot be read, when it does
    not give ``activity_log`` a default, or when its tables do not all share
    one non-empty default: a silent fallback would make the write guard
    accept the value and the activity-log backfill match nothing.
    """
    path = MIGRATIONS_DIR / MIGRATION_029
    try:
        sql = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read migration {MIGRATION_029}: {exc}") from exc
    by_table = {table.lower(): default for table, default in _DEFAULT_RE.findall(sql)}
    found = set(by_table.values())
    if len(found) != 1 or not next(iter(found)).strip():
        raise RuntimeError(
            f"expected exactly one non-empty patient_id DEFAULT in {MIGRATION_029}, "
            f"found {len(found)}"
        )
    if "activity_log" not in by_table:
        raise RuntimeError(f"{MIGRATION_029} gives activity_log.patient_id no DEFAULT")
    return found.pop()


# Parsed once at import. No patient has this id.
LEGACY_NAMED_DEFAULT: str = migration_029_patient_id_default()
