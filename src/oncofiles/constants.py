"""Shared constants used across modules that would otherwise create import cycles.

Keep this module dependency-free — no imports of other oncofiles modules.
"""

from __future__ import annotations

NO_PATIENT_ACCESS_SENTINEL = "__no_patient_access__"
"""Sentinel returned when a caller has no authorized patient.

Non-empty so it passes truthy ``if patient_id:`` filters in DB helpers —
but not a valid UUID, so ``WHERE patient_id = ?`` matches zero rows. This
closes cross-patient data leaks where new/unauthorized users would see all
patients' data when the resolution fell back to an unscoped default.

Defined here rather than in server.py so persistent_oauth.py can import
it without the server → persistent_oauth → server circular.

History:
  - #476 (2026-04-23): dashboard caller path (`_get_dashboard_patient_id`).
  - Michal Gašparík report (2026-04-24): MCP OAuth caller path
    (`_resolve_oauth_patient`) — same leak class, same fix.
"""

SYSTEM_NO_PATIENT = "__system_no_patient__"
"""Explicit "no patient" value for system rows in operational logs.

Used by prompt_log (AI calls outside a request), activity_log (tool calls
that resolved no single patient, maintenance rows) and agent_state (system
aggregates). Truthy and non-UUID, so a patient-scoped ``WHERE patient_id = ?``
never matches it. The tenant write guard (#791) rejects ``''`` and the legacy
column defaults; this marker is the only way to write a row without a patient,
and only to those three tables.
"""
