"""Which MCP tools a connector sees: user vs operator surface (#887).

The maintainer (2026-11-17): operator tools do not belong in Claude.ai.
Every registered tool is classified here, exactly once:

* ``USER_TOOLS`` — what a patient or caregiver uses in a chat: documents,
  search, view, timeline, facts and fact review, patient overview,
  session diary, export / FHIR, clinical records, treatment events and the
  Google connect flows. Served on the public ``/mcp`` endpoint.
* ``OPERATOR_TOOLS`` — maintenance: curated runs, re-OCR, compare, promote,
  re-read, extraction previews and writes, scorecards, backfills, audits,
  repairs, legacy Drive sync/setup, token admin, reassign, system internals,
  raw SQL, tools that compute clinical interpretation in code or read legacy
  tables curated patients do not have (#928), plus every deprecated tool
  (Oncoteam sunset #721, legacy lab readers). Served only on ``/ops/mcp``.

How it is enforced (native FastMCP ``Transform``, the same mechanism as
``tool_annotations``): ``ToolSurfaceTransform`` drops operator tools from
``tools/list`` and makes ``get_tool`` return ``None`` for them (so
``tools/call`` answers "unknown tool") unless the current HTTP request came
through ``OpsGate``. ``OpsGate`` wraps the same streamable-http handler as
``/mcp`` (same process, same lifespan, same in-memory job registries and
locks), answers the app's plain 404 to any peer that is not loopback
(direct TCP peer only; forwarding headers are never consulted, so Traefik
traffic is always refused), accepts only the static operator bearer
(constant-time compare), and marks the ASGI scope. The marker lives in the server-side scope, so a
client cannot set it. Unclassified tools are hidden on ``/mcp`` (fail
closed); ``tests/test_tool_surface.py`` fails when a tool is unclassified.

Outside an HTTP request (stdio transport, in-process calls, tests) there is
no connector to protect and the full surface is served.

After a deploy that changes this list, a Claude.ai connector shows the new
(shorter) list only after Settings → Connectors → Oncofiles → ``...`` →
**Refresh tools list**.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Sequence

from fastmcp.server.transforms import Transform
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

OPS_MCP_PATH = "/ops/mcp"
# Set by OpsGate on the server-side ASGI scope; never derived from client input.
OPS_SCOPE_KEY = "oncofiles.ops_surface"

USER_TOOLS: frozenset[str] = frozenset(
    {
        # documents
        "delete_document",
        "get_document",
        "get_document_by_id",
        "get_document_group",
        "get_document_versions",
        "get_related_documents",
        "list_documents",
        "list_trash",
        "restore_document",
        "search_documents",
        "update_document_category",
        "upload_document",
        "view_document",
        # preventive care: records next to labelled guideline intervals (#928)
        "get_preventive_care_status",
        # facts model (#603, #726) and fact review (#742)
        "correct_fact",
        "dashboard_feed",
        "document_detail",
        "encounter_detail",
        "fact_catalog",
        "fact_series",
        "patient_overview",
        "people_and_places",
        "timeline",
        "unverify_fact",
        "verify_fact",
        # clinical records
        "add_clinical_record",
        "add_clinical_record_note",
        "delete_clinical_record",
        "get_clinical_record",
        "get_record_audit",
        "list_clinical_record_notes",
        "list_clinical_records",
        "restore_clinical_record",
        "search_notes",
        "update_clinical_record",
        # treatment events
        "add_treatment_event",
        "delete_treatment_event",
        "get_treatment_event",
        "restore_treatment_event",
        "update_treatment_event",
        # session diary (kept 2026-11-11)
        "delete_conversation",
        "diary_add",
        "get_conversation",
        "get_conversation_stats",
        "get_journey_timeline",
        "log_conversation",
        "restore_conversation",
        "search_conversations",
        # patient
        "get_patient_context",
        "list_patients",
        "select_patient",
        "update_patient_context",
        "whoami",
        # export / FHIR
        "export_document_package",
        "export_manifest",
        "export_patient_summary",
        "fhir_capabilities",
        "fhir_read",
        "fhir_search",
        "patient_everything",
        # curated folder: read-only state of the patient's own folder
        "curated_folder_status",
        # Google connect flows and stored mail/calendar
        "calendar_auth_enable",
        "gdrive_auth_callback",
        "gdrive_auth_status",
        "gdrive_auth_url",
        "get_calendar_event",
        "get_email",
        "gmail_auth_enable",
        "integration_status",
        "search_calendar_events",
        "search_emails",
    }
)

OPERATOR_TOOLS: frozenset[str] = frozenset(
    {
        # curated pipeline: runs, index, re-OCR, re-read, extraction (#562 ff.)
        "curated_clinical_extract",
        "curated_clinical_extract_preview",
        "curated_clinical_retract",
        "curated_extraction_scorecard",
        "curated_folder_run",
        "curated_index",
        "curated_lab_extract",
        "curated_lab_retract",
        "curated_llm_extract_preview",
        "curated_promote_candidates",
        "curated_reocr",
        "curated_reocr_compare",
        "curated_reocr_status",
        "curated_reread_verify",
        "curated_restore_sidecar",
        # AI enrichment / backfills (cloud AI is off by default)
        "backfill_activity_log_attribution",
        "backfill_ai_classification",
        "backfill_lab_values_from_metadata",
        "backfill_orphan_prompt_logs",
        "detect_and_clone_vaccinations",
        "detect_and_consolidate_documents",
        "detect_and_split_documents",
        "enhance_documents",
        "extract_all_metadata",
        "extract_document_metadata",
        "store_lab_values",
        # audits / repairs / hygiene
        "audit_document_pipeline",
        "audit_gdrive_folder_hygiene",
        "audit_patient_isolation",
        "consolidate_external_gdrive_files",
        "find_duplicates",
        "reassign_document",
        "redact_activity_log_inputs",
        "repair_broken_groups",
        "repair_gdrive_folder_hygiene",
        "unblock_stuck_documents",
        "validate_categories",
        # legacy Drive sync / setup (curated folders are skipped anyway)
        "gdrive_fix_permissions",
        "gdrive_set_folder",
        "gdrive_sync",
        "gdrive_sync_status",
        "reconcile_gdrive",
        "rename_documents_to_standard",
        "setup_gdrive",
        "sync_from_gdrive",
        "sync_to_gdrive",
        # system internals
        "get_document_status_matrix",
        "get_pipeline_status",
        "get_prompt_log_entry",
        "list_tool_definitions",
        "qa_analysis",
        "query_db",
        "search_prompt_log",
        "system_health",
        # token admin (#824)
        "list_patient_tokens",
        "revoke_patient_tokens",
        # deprecated: Oncoteam sunset (#721)
        "add_activity_log",
        "add_research_entry",
        "get_activity_stats",
        "get_agent_state",
        "list_agent_states",
        "list_research_entries",
        "search_activity_log",
        "search_research",
        "set_agent_state",
        # clinical interpretation in code / legacy tables curated patients lack
        # (#928, design #711 §4.4): hardcoded protocol thresholds and checklists,
        # lab analysis prompts, Oncoteam-era trial search and analysis store
        "add_clinical_analysis",
        "analyze_labs",
        "compare_labs",
        "fetch_clinical_trials",
        "get_lab_safety_check",
        "get_precycle_checklist",
        # deprecated: legacy lab readers / treatment list (removed after 2027-01-10)
        "compare_lab_panels",
        "get_lab_summary",
        "get_lab_time_series",
        "get_lab_trends",
        "list_treatment_events",
    }
)


def ops_surface_active() -> bool:
    """True when the full (operator) surface may be served.

    Inside an HTTP request: only when ``OpsGate`` admitted it. Outside one
    (stdio, in-process): always.
    """
    from fastmcp.server.dependencies import get_http_request

    try:
        request = get_http_request()
    except RuntimeError:
        return True
    return bool(request.scope.get(OPS_SCOPE_KEY))


def visible_on_user_surface(name: str) -> bool:
    return name in USER_TOOLS


class ToolSurfaceTransform(Transform):
    """Hide operator (and unclassified) tools from every non-ops request."""

    async def list_tools(self, tools: Sequence) -> Sequence:
        if ops_surface_active():
            return tools
        return [t for t in tools if visible_on_user_surface(t.name)]

    async def get_tool(self, name, call_next, *, version=None):
        tool = await call_next(name, version=version)
        if tool is None or ops_surface_active() or visible_on_user_surface(tool.name):
            return tool
        return None


# ── /ops/mcp endpoint ────────────────────────────────────────────────────────


def _bearer_from_scope(scope: Scope) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == b"authorization":
            raw = value.decode("latin-1").strip()
            if raw[:7].lower() == "bearer ":
                return raw[7:].strip()
            return ""
    return ""


def _operator_token() -> str:
    from oncofiles import config

    return config.MCP_BEARER_TOKEN or ""


LOOPBACK_CLIENTS: frozenset[str] = frozenset({"127.0.0.1", "::1"})


def _is_loopback_client(scope: Scope) -> bool:
    """The direct TCP peer only (``scope["client"]``). X-Forwarded-For,
    Forwarded and X-Real-IP are never consulted."""
    client = scope.get("client")
    if not client:
        return False
    return str(client[0]) in LOOPBACK_CLIENTS


class OpsGate:
    """ASGI wrapper for ``/ops/mcp``: loopback peer AND static operator bearer.

    1. A peer that is not 127.0.0.1 / ::1 (or no peer at all) gets the app's
       ordinary 404, before any auth check, so the endpoint's existence is
       not revealed. Traefik traffic arrives from the container network and
       is always refused; ``docker exec … python -m oncofiles.ops`` is
       loopback.
    2. On loopback, anything but the static operator bearer (no header, a
       patient ``onco_*`` token, an OAuth token, a wrong value, or no operator
       token configured) → 401 before the MCP handler runs.

    The token is never logged.
    """

    def __init__(self, app: ASGIApp, token_getter: Callable[[], str] = _operator_token):
        self.app = app
        self._token_getter = token_getter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_loopback_client(scope):
            if scope["type"] == "websocket":
                await WebSocketClose()(scope, receive, send)
                return
            # The same response as any unknown path (the app's 404 handling).
            raise HTTPException(status_code=404)
        expected = self._token_getter() or ""
        presented = _bearer_from_scope(scope)
        if not (
            expected and presented and hmac.compare_digest(presented.encode(), expected.encode())
        ):
            response = JSONResponse(
                {"error": "unauthorized", "detail": "operator bearer required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="oncofiles-ops"'},
            )
            await response(scope, receive, send)
            return
        scope[OPS_SCOPE_KEY] = True
        await self.app(scope, receive, send)


def install_ops_route(app, mcp_path: str) -> bool:
    """Add ``/ops/mcp`` to a FastMCP streamable-http app, next to ``mcp_path``.

    Reuses the ``/mcp`` route's endpoint (FastMCP's auth wrapper around the
    one streamable-http handler), so both paths share the session manager,
    lifespan and process. Returns False when there is no such route (SSE).
    """
    routes = app.router.routes
    if any(getattr(r, "path", None) == OPS_MCP_PATH for r in routes):
        return True
    for route in routes:
        if isinstance(route, Route) and route.path == mcp_path:
            ops = Route(
                OPS_MCP_PATH,
                endpoint=OpsGate(route.app),
                methods=sorted(route.methods - {"HEAD"}) if route.methods else None,
            )
            routes.insert(0, ops)
            return True
    return False
