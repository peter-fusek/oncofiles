"""FHIR R4 read surface (#716 phase 3a, design §4.1). Read-only, patient-scoped.

Every tool resolves the patient through ``_resolve_patient_id`` (token binding
or ``patient_slug``; a non-admin caller is denied a foreign slug) and projects
only that patient's rows. An admin caller without ``patient_slug`` is refused
(no implicit admin scope, same rule as ``query_db`` #686 and
``export_manifest`` #696). Nothing here writes.

Tools: ``fhir_capabilities``, ``fhir_search``, ``fhir_read``,
``patient_everything``, ``export_patient_summary``. The last two are bulk
egress of a patient's record and are deliberately NOT ``readOnlyHint`` (see
``FHIR_EGRESS_TOOLS``), so clients keep asking for manual approval.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import Context

from oncofiles.fhir.search import (
    PatientIndex,
    SearchError,
    capability_statement,
    everything,
    operation_outcome,
    patient_summary,
    read,
    search,
)
from oncofiles.fhir.store import load_patient
from oncofiles.tools._helpers import _get_db, _is_admin_caller, _resolve_patient_id

# Annotations come from the shared classification in ``oncofiles.tool_annotations``
# (#690): the three lookups are in READ_ONLY_TOOLS; the two bulk exports of a
# patient's record are in EGRESS_TOOLS (no annotations, so MCP clients such as
# Claude.ai keep asking the user for approval, like export_manifest /
# export_document_package). Locked by tests/test_fhir_read.py and
# tests/test_tool_annotations.py.
FHIR_EGRESS_TOOLS = frozenset({"patient_everything", "export_patient_summary"})


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


async def _index(ctx: Context, patient_slug: str | None) -> PatientIndex:
    # Same rule as query_db / export_manifest (#607/#686/#696): no implicit admin
    # patient scope — an admin must name the patient.
    if _is_admin_caller() and not patient_slug:
        raise SearchError(
            "Admin callers must pass patient_slug=<slug>; there is no implicit admin "
            "patient scope.",
            code="security",
        )
    pid = await _resolve_patient_id(patient_slug, ctx)
    return PatientIndex(await load_patient(_get_db(ctx), pid))


async def fhir_capabilities(ctx: Context) -> str:
    """FHIR R4 CapabilityStatement of the read-only FHIR surface.

    Lists the resource types, search parameters per type, code systems
    (LOINC, UCUM, SNOMED CT, WHO ICD-10, ICD-O-3, ATC, MKCH-10) and how
    verification is exposed (meta.tag ``urn:oncofiles:verification``, AIAST
    security label on machine-derived data). No patient data. Read-only.
    """
    return _dump(capability_statement())


async def fhir_search(
    ctx: Context,
    type: str,
    params: dict[str, Any] | None = None,
    _count: int = 50,
    page_token: str | None = None,
    patient_slug: str | None = None,
) -> str:
    """Search one resource type for this patient → FHIR searchset Bundle (paged).

    type: Observation, Condition, Procedure, MedicationRequest,
    MedicationAdministration, MedicationStatement, CarePlan, DocumentReference,
    Encounter, Practitioner, Organization, Device, Provenance, Patient.
    params (FHIR names): ``category``, ``code`` (``system|code``, ``|code``,
    ``code``) or ``code:text`` (substring), ``date`` with prefixes
    (``ge2027-01-01``, ``le2027-12-31``; a list = AND, comma = OR),
    ``status``, ``_tag`` (verification, e.g. ``urn:oncofiles:verification|verified``),
    ``_id``, ``_revinclude=Provenance:target`` (source document, page, quote),
    ``_elements``. Per type extras: Condition ``clinical-status``,
    ``verification-status``, ``onset-date``; DocumentReference ``type``;
    Encounter ``class``; Provenance ``target``.
    Example: type="Observation", params={"code": "http://loinc.org|718-7",
    "date": ["ge2027-01-01"], "_revinclude": "Provenance:target"}.
    Paging: at most ``_count`` (≤200) matches and ~150 kB per page; continue
    with the ``page_token`` value from the Bundle's ``next`` link.
    Unverified values are included and tagged; never state them as facts.
    Read-only.
    """
    try:
        index = await _index(ctx, patient_slug)
        return _dump(search(index, type, params, count=_count, page_token=page_token))
    except SearchError as exc:
        return _dump(operation_outcome(str(exc), exc.code))


async def fhir_read(ctx: Context, type: str, id: str, patient_slug: str | None = None) -> str:
    """Read one FHIR resource of this patient by type and id (``GET [type]/[id]``).

    Ids are stable (the row's ``fhir_id``, else a deterministic UUIDv5).
    Another patient's id returns not-found. Read-only.
    """
    try:
        index = await _index(ctx, patient_slug)
        return _dump(read(index, type, id))
    except SearchError as exc:
        return _dump(operation_outcome(str(exc), exc.code))


async def patient_everything(
    ctx: Context,
    start: str | None = None,
    end: str | None = None,
    _type: list[str] | str | None = None,
    _count: int = 50,
    page_token: str | None = None,
    patient_slug: str | None = None,
) -> str:
    """``Patient/$everything`` for this patient → paged searchset Bundle.

    The Patient first, then every resource newest first (facts, documents,
    encounters, practitioners, organisations, devices, Provenance). ``start`` /
    ``end`` (ISO dates) limit dated resources; ``_type`` limits resource types
    (list or comma-separated). Continue with ``page_token`` from the ``next``
    link. Read-only.
    """
    try:
        index = await _index(ctx, patient_slug)
        return _dump(
            everything(
                index, start=start, end=end, types=_type, count=_count, page_token=page_token
            )  # fmt: skip
        )
    except SearchError as exc:
        return _dump(operation_outcome(str(exc), exc.code))


async def export_patient_summary(
    ctx: Context, include_unverified: bool = False, patient_slug: str | None = None
) -> str:
    """IPS-shaped patient summary: a FHIR document Bundle (Composition first).

    Sections Problems, Medication Summary, Allergies and Intolerances, Results
    (latest laboratory value per code); an empty section carries emptyReason
    ``unavailable``. Every fact keeps its Provenance (source document, page,
    quotation) and verification tag. Unverified facts are EXCLUDED unless
    ``include_unverified=true``. Read-only.
    """
    try:
        index = await _index(ctx, patient_slug)
    except SearchError as exc:
        return _dump(operation_outcome(str(exc), exc.code))
    return _dump(patient_summary(index, include_unverified=include_unverified))


def register(mcp):
    for fn in (
        fhir_capabilities,
        fhir_search,
        fhir_read,
        patient_everything,
        export_patient_summary,
    ):
        mcp.tool()(fn)
