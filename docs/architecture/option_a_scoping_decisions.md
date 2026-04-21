# Option A (#429) scoping decisions — admin tools

**Status:** Post-v5.11 day-1 audit, 2026-04-21.
**Context:** After the Option A rollout patched ~55 patient-scoped tools to accept `patient_slug`, six remain in admin modules (`activity.py`, `agent_state.py`) that deliberately stay cross-patient. This memo documents why, and the migration path for each if they ever *should* become patient-scoped.

## Summary

| Module | Tools | Current scope | Decision | Reason |
|---|---|---|---|---|
| `activity.py` | 3 | Cross-patient | **Keep cross-patient** | Audit trail for ALL agent tool calls across ALL patients. Used for ops debugging + `qa_analysis`. Patient scope would fragment the audit view. |
| `agent_state.py` | 3 | Cross-patient (scoped by `agent_id`) | **Needs a separate decision + migration** | The `agent_state` table has `agent_id` but no `patient_id` column. Latent risk: two patients' agents writing to the same key collide. See `#406 Finding 6` reference. Not a v5.11 ship. |
| `prompt_log.py` | 2 | Patient-scoped (ALREADY) | **Patched with `patient_slug` in v5.11** | Already filtered by `patient_id` in DB; Option A slug param added in commit `<next>`. |
| `db_query.py` | 1 | Hybrid (admin tool with patient-scope enforcement) | **Patched with `patient_slug` in v5.11** | Keeps cross-table admin visibility, but the enforcement check now sources its patient-hint from the resolved slug instead of the middleware ContextVar alone. |

## Per-tool analysis

### activity.py — 3 tools, all admin cross-patient

**`add_activity_log(session_id, agent_id, tool_name, ...)`**
Writes an audit row to `activity_log`. The table has columns for `session_id` + `agent_id` but **no `patient_id`**. That's intentional: a single Oncoteam agent session can span multiple patients (e.g. `list_patients` then `select_patient` then `analyze_labs`). Adding `patient_id` would force the caller to pick one patient per call, which doesn't match the cross-patient nature of agent sessions.

**`search_activity_log(...)`** and **`get_activity_stats(...)`**
Same profile. The `qa_analysis` tool (already in hygiene.py) reads these to detect recurring errors across the whole system. Patient-scoping would break that ops view.

**Decision:** keep cross-patient. Document in the tool docstrings that these are cross-patient by design (DONE in existing docstrings).

**If ever needed:** add a `patient_id` column to the `activity_log` table AND a new `patient_id` parameter to each tool, preserving the cross-patient default. Migration would need to NULL-out existing rows (they can't retroactively tie to a patient).

### agent_state.py — 3 tools, cross-patient by bug (not by design)

**`set_agent_state(key, value, agent_id='oncoteam')`**
Stores a per-agent key-value pair. The `agent_state` table has `agent_id` + `key` + `value` but **no `patient_id`**. This is the latent concern noted in the v5.8.0 commit bundle under `#406 Finding 6`:

> autonomous clinical tools — agent_state patient_id NOT individually ticketed, available in #406 closed thread

If Oncoteam sets `key='last_briefing_date'` during a session about Patient A, then switches to Patient B, the subsequent `get_agent_state('last_briefing_date')` call returns Patient A's date. That's a cross-patient data leak in a narrow case.

**`get_agent_state(key, agent_id)`** — same profile.
**`list_agent_states(agent_id)`** — same profile.

**Decision:** tracked as an **open architectural follow-up** for v5.12 or later. The fix is:
1. Migration 060 (or next free number): `ALTER TABLE agent_state ADD COLUMN patient_id TEXT` + drop/re-create UNIQUE constraint from `(agent_id, key)` to `(agent_id, key, patient_id)`.
2. Update `set_agent_state` / `get_agent_state` / `list_agent_states` to accept `patient_slug` and thread through.
3. One-time data migration: set `patient_id = default_patient` for any existing rows, or NULL them out under "cross-patient legacy" semantics.
4. Oncoteam-side update: propagate `patient_slug` in agent_state MCP calls.

Not attempted in v5.11 because the fix touches both Oncofiles and Oncoteam schemas and needs coordinated review.

### prompt_log.py — 2 tools, patient-scoped (patched v5.11)

Both tools already filtered by `patient_id` at the DB layer. The Option A patch added the `patient_slug` parameter to each (`get_prompt_log_entry`, `search_prompt_log`) and switched the pid-resolution path from `_get_patient_id()` to `_resolve_patient_id(patient_slug, ctx)`. Test coverage: existing `test_prompt_log.py` continues to exercise the patient-filter at the DB layer.

### db_query.py — 1 tool, hybrid admin

**`query_db(sql, limit, patient_slug)`** — admin-scoped SQL escape hatch. The pre-Option-A code already enforced "queries on patient-scoped tables must include a `patient_id = 'xxx'` filter", using `_get_patient_id()` to populate the hint in the error message. The patch:
- Adds `patient_slug` parameter so admins can target a specific patient's hint
- Switches the resolution call to `_resolve_patient_id(patient_slug, ctx, required=False)` — stays permissive (returns empty string if nothing resolves, same behavior as before)
- Does NOT change the enforcement logic itself — the SQL still needs the `patient_id = ?` filter to pass

Test `test_blocks_cross_patient_query` updated to mock `_resolve_patient_id` instead of the old module-level `_get_patient_id` import.

## Sprint accounting

**Patient-scoped tools with `patient_slug` after v5.11 day-1:** ~56
(Started the sprint at 9. Session 2 + Option A rollouts added 47.)

**Admin tools intentionally kept cross-patient:** 6
(`activity_log` x 3 + `agent_state` x 3 — until the agent_state schema change lands.)

**Other admin tools not needing `patient_slug`:** 3
(`list_tool_definitions`, `reassign_document`, `audit_patient_isolation` — all cross-patient by design.)

**Blocked on separate refactor:** 1
(`reconcile_gdrive` — needs `_get_gdrive(ctx, patient_slug)` middleware support.)

## Referenced tickets
- #429 — Option A rollout meta
- #406 Finding 6 — agent_state patient_id gap (open, not individually ticketed per closed #406 thread)
