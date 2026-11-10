"""MCP tool annotations: which tools only read (#608).

Claude.ai and other MCP clients use ``ToolAnnotations`` to decide whether a
tool call needs a manual "Allow". Without annotations the spec defaults apply
(``readOnlyHint=false``, ``destructiveHint=true``), so every read — list,
search, labs, status — asked for approval.

This module is the single declarative classification:

* ``READ_ONLY_TOOLS`` — the tool never changes stored data, Drive, or
  external state. Verified per tool against its body (only DB getters /
  SELECTs, Drive listings). Deliberately NOT here:
    - ``view_document``, ``analyze_labs``, ``compare_labs``: they OCR on a
      cache miss, store ``document_pages`` and may call a model;
    - ``select_patient``: changes the connection's patient;
    - ``gdrive_auth_url`` / ``gmail_auth_enable`` / ``calendar_auth_enable``:
      start an OAuth flow;
    - every curated_* tool (owned by the curated work stream).
  The audit middleware logs every call to ``activity_log``; that is
  server bookkeeping, not an effect of the tool, and does not change this.
* ``DESTRUCTIVE_TOOLS`` — removes data or moves it out of a patient's
  scope. Marked explicitly (``destructiveHint=true``).
* ``WRITE_TOOLS`` — everything else that writes. Left without annotations
  so clients keep the conservative spec defaults and keep asking.

Tools that set their own annotations at registration (``clinical_facts``)
are left untouched. ``tests/test_tool_annotations.py`` fails when a new tool
is registered without being classified here — adding a tool to
``READ_ONLY_TOOLS`` is a reviewable decision: only if it truly never writes.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastmcp.server.transforms import Transform
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        # activity
        "get_activity_stats",
        "search_activity_log",
        # agent_state
        "get_agent_state",
        "list_agent_states",
        # clinical_records
        "get_clinical_record",
        "get_record_audit",
        "list_clinical_record_notes",
        "list_clinical_records",
        "search_notes",
        # conversations
        "get_conversation",
        "get_conversation_stats",
        "get_journey_timeline",
        "search_conversations",
        # db_query (SELECT-only by AST validation)
        "query_db",
        # documents
        "audit_patient_isolation",
        "find_duplicates",
        "get_document",
        "get_document_by_id",
        "get_document_group",
        "get_document_versions",
        "get_related_documents",
        "list_documents",
        "list_trash",
        "search_documents",
        # export / gdrive
        "export_document_package",
        "export_manifest",
        "gdrive_auth_status",
        "gdrive_sync_status",
        # hygiene
        "audit_document_pipeline",
        "audit_gdrive_folder_hygiene",
        "get_document_status_matrix",
        "get_pipeline_status",
        "list_tool_definitions",
        "qa_analysis",
        "system_health",
        # integrations
        "get_calendar_event",
        "get_email",
        "integration_status",
        "search_calendar_events",
        "search_emails",
        # lab_trends
        "compare_lab_panels",
        "get_lab_safety_check",
        "get_lab_summary",
        "get_lab_time_series",
        "get_lab_trends",
        "get_precycle_checklist",
        "get_preventive_care_status",
        # patient
        "get_patient_context",
        "list_patients",
        # prompt_log
        "get_prompt_log_entry",
        "search_prompt_log",
        # research
        "list_research_entries",
        "search_research",
        # treatment
        "get_treatment_event",
        "list_treatment_events",
    }
)

DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {
        "delete_document",
        "delete_clinical_record",
        "delete_treatment_event",
        "reassign_document",
    }
)

# Writes that keep the spec defaults (not read-only, possibly destructive).
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "add_activity_log",
        "add_clinical_analysis",
        "add_clinical_record",
        "add_clinical_record_note",
        "add_research_entry",
        "add_treatment_event",
        "analyze_labs",
        "backfill_activity_log_attribution",
        "backfill_ai_classification",
        "backfill_lab_values_from_metadata",
        "backfill_orphan_prompt_logs",
        "calendar_auth_enable",
        "compare_labs",
        "consolidate_external_gdrive_files",
        "detect_and_clone_vaccinations",
        "detect_and_consolidate_documents",
        "detect_and_split_documents",
        "enhance_documents",
        "extract_all_metadata",
        "extract_document_metadata",
        "fetch_clinical_trials",
        "gdrive_auth_callback",
        "gdrive_auth_url",
        "gdrive_fix_permissions",
        "gdrive_set_folder",
        "gdrive_sync",
        "gmail_auth_enable",
        "log_conversation",
        "reconcile_gdrive",
        "rename_documents_to_standard",
        "repair_broken_groups",
        "repair_gdrive_folder_hygiene",
        "restore_clinical_record",
        "restore_document",
        "select_patient",
        "set_agent_state",
        "setup_gdrive",
        "store_lab_values",
        "sync_from_gdrive",
        "sync_to_gdrive",
        "unblock_stuck_documents",
        "update_clinical_record",
        "update_document_category",
        "update_patient_context",
        "update_treatment_event",
        "upload_document",
        "validate_categories",
        "view_document",
    }
)


def annotation_for(name: str) -> ToolAnnotations | None:
    if name in READ_ONLY_TOOLS:
        return READ_ONLY
    if name in DESTRUCTIVE_TOOLS:
        return DESTRUCTIVE
    return None


def _annotate(tool):
    # Respect annotations a module set itself (e.g. clinical_facts).
    if tool is not None and tool.annotations is None:
        ann = annotation_for(tool.name)
        if ann is not None:
            tool.annotations = ann
    return tool


class ToolAnnotationTransform(Transform):
    """Server-level transform that applies the classification above to every
    tool listing and lookup."""

    async def list_tools(self, tools: Sequence) -> Sequence:
        return [_annotate(t) for t in tools]

    async def get_tool(self, name, call_next, *, version=None):
        return _annotate(await call_next(name, version=version))
