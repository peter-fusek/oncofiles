"""Write-side tenant guard for tables whose ``patient_id`` has a DEFAULT (#791).

Twelve tables declare ``patient_id`` with a column DEFAULT (``'erika'``,
``'default'`` or ``''``), and ``curated_settings.patient_id`` is a nullable
``TEXT PRIMARY KEY``. A writer that forgets the patient would silently store
the row under a real-looking or empty tenant instead of failing: the same bug
class as the empty-string leak (#476).

Until the schema drops those defaults (#791 step 2, a maintenance-window
table rebuild), every write path to these tables calls
:func:`require_patient_id` first. ``tests/test_patient_id_guard.py`` locks
both the guard and the list of write sites.
"""

from __future__ import annotations

from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL, SYSTEM_NO_PATIENT

# The table list lives in tests/test_patient_id_guard.py (GUARDED_TABLES),
# checked against the migrated schema there, so this module never names a
# table (the session-diary lock test scans src/ for its table name).

# Values the schema defaults ever produced: a row carrying one of them was
# written without a patient. Never accepted from a writer.
LEGACY_DEFAULT_PATIENT_IDS: frozenset[str] = frozenset({"", "erika", "default"})


def require_patient_id(patient_id: object, *, allow_system: bool = False) -> str:
    """Return ``patient_id`` if it names a tenant, else raise ``ValueError``.

    Rejected: ``None``, a non-string, an empty or whitespace-only string, any
    legacy column default (``''``, ``'erika'``, ``'default'``) and
    ``NO_PATIENT_ACCESS_SENTINEL``. ``SYSTEM_NO_PATIENT`` (the explicit
    "no patient" value of system rows) is accepted only with
    ``allow_system=True``, which only the operational logs use
    (activity_log, prompt_log, agent_state).
    """
    if not isinstance(patient_id, str):
        raise ValueError(f"patient_id is required for this write (got {type(patient_id).__name__})")
    if not patient_id.strip() or patient_id in LEGACY_DEFAULT_PATIENT_IDS:
        raise ValueError(
            f"patient_id is required for this write; {patient_id!r} is a legacy "
            "column default, not a patient (#791)"
        )
    if patient_id != patient_id.strip():
        raise ValueError("patient_id must not carry surrounding whitespace (#791)")
    if patient_id == NO_PATIENT_ACCESS_SENTINEL:
        raise ValueError("cannot write rows for the no-access sentinel (#476/#791)")
    if patient_id == SYSTEM_NO_PATIENT and not allow_system:
        raise ValueError("the system no-patient marker is only valid for operational logs (#791)")
    return patient_id
