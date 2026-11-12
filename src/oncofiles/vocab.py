"""Closed vocabularies: ONE registry of the TEXT codes stored in enum-like columns (#789).

Rule 4 of ``docs/architecture/data_model_conventions.md``: a closed vocabulary
(category, kind, status, verification …) is a TEXT code — stable, never renamed —
registered here with a Slovak and an English label. An entity with attributes
(organisation, department, doctor) is an FK id instead, never a copied text code.

Three lists classify every enum-like column (``tests/test_schema_conventions.py``
finds them by name and fails on an unclassified one):

* ``COLUMN_DOMAINS`` — (table, column) → domain of this registry;
* ``REFERENCE_CODE_COLUMNS`` — a code used as a key into a reference table or an
  external code system that is not enumerated here (fact codes, ICD/LOINC codes
  in ``codings``, IANA media types);
* ``FREE_TEXT_BY_DESIGN`` — free text on purpose (printed values, caller-chosen
  provenance tags, sunset tables), each with its reason.

Codes are not copied: where an allowlist, enum or ``Literal`` already exists, the
domain loads it from there (``source`` names it). Only vocabularies that had no
constant anywhere are defined in this module. Loading is lazy (``registry()``),
so importing this module stays cheap and cannot create an import cycle.

Phase 1 is documentation and lint only: nothing validates writes against the
registry yet, and there is no ``ref_codes`` view yet (``rows()`` is its future
content). ``deprecated`` codes are valid in stored rows but no longer written.
"""

from __future__ import annotations

import functools
import importlib
import typing
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Code:
    code: str
    label_sk: str
    label_en: str


@dataclass(frozen=True)
class Domain:
    name: str
    codes: tuple[Code, ...]
    source: str  # where the codes are defined ("oncofiles.vocab" = here)
    standard: str | None = None  # external standard whose spelling the codes keep
    deprecated: frozenset[str] = frozenset()

    @property
    def code_set(self) -> frozenset[str]:
        return frozenset(c.code for c in self.codes)

    def __contains__(self, value: object) -> bool:
        return value in self.code_set

    def label(self, code: str, lang: str = "sk") -> str | None:
        for c in self.codes:
            if c.code == code:
                return c.label_sk if lang == "sk" else c.label_en
        return None


Loader = Callable[[], Any]


@dataclass(frozen=True)
class _Spec:
    source: str
    load: Loader
    standard: str | None = None
    labels: Callable[[], dict[str, tuple[str, str]]] | None = None
    deprecated: Callable[[], Iterable[str]] | tuple[str, ...] = ()


# ── loaders (imports happen on first use of the registry) ─────────────────


def _obj(module: str, attr: str) -> Any:
    return getattr(importlib.import_module(module), attr)


def _from(module: str, attr: str, pick: Callable[[Any], Any] | None = None) -> _Spec:
    """A domain whose codes live in ``module.attr`` (optionally picked from it)."""

    def load():
        obj = _obj(module, attr)
        return pick(obj) if pick else obj

    return _Spec(source=f"{module}.{attr}", load=load)


def _literal_args(annotation: Any) -> list[str]:
    """Flatten ``Literal[...]`` (also inside ``X | None``) into its string values."""
    if typing.get_origin(annotation) is typing.Literal:
        return [a for a in typing.get_args(annotation) if isinstance(a, str)]
    return [v for arg in typing.get_args(annotation) for v in _literal_args(arg)]


def _model_literal(model: str, field: str) -> _Spec:
    def load():
        return _literal_args(_obj("oncofiles.models", model).model_fields[field].annotation)

    return _Spec(source=f"oncofiles.models.{model}.{field}", load=load)


def _here(codes: tuple[str, ...], written_by: str) -> _Spec:
    """A vocabulary that had no constant anywhere: defined here, ``written_by`` says where."""
    return _Spec(source=f"oncofiles.vocab (written by {written_by})", load=lambda: codes)


def _with(spec: _Spec, **changes: Any) -> _Spec:
    return replace(spec, **changes)


def _values(obj: Any) -> list[str]:
    return [m.value for m in obj]


def _category_labels() -> dict[str, tuple[str, str]]:
    folder_sk = _obj("oncofiles.gdrive_folders", "FOLDER_SK")
    merges = _obj("oncofiles.gdrive_folders", "CATEGORY_MERGES")
    out = {}
    for code, en in _CATEGORY_EN.items():
        sk = folder_sk.get(merges.get(code, code), "")
        sk = sk[:1].upper() + sk[1:]
        out[code] = (f"{sk} (starý alias)" if code in merges else sk, en)
    return out


def _verification_labels() -> dict[str, tuple[str, str]]:
    label = _obj("oncofiles.fact_labels", "verification_label")
    return {
        code: (label(code, None)["sk"], label(code, None)["en"])
        for code in ("machine", "unverified", "verified", "corrected")
    }


def _interpretation_labels() -> dict[str, tuple[str, str]]:
    display = _obj("oncofiles.fhir.terminology", "INTERPRETATION_DISPLAY")
    return {code: (_INTERPRETATION_SK.get(code, ""), en) for code, en in display.items()}


def _identifier_system_labels() -> dict[str, tuple[str, str]]:
    m = importlib.import_module("oncofiles.database._hard_facts")
    return {
        m.ID_SYSTEM_UDZS_PROVIDER: ("Kód poskytovateľa ÚDZS", "ÚDZS provider code"),
        m.ID_SYSTEM_UDZS_DOCTOR: ("Kód lekára ÚDZS", "ÚDZS doctor code"),
        m.ID_SYSTEM_ICO: ("IČO", "Organisation registry number (IČO)"),
        m.ID_SYSTEM_ACCESSION: ("Číslo vzorky alebo dokumentu", "Accession number"),
    }


def _entity_labels() -> dict[str, tuple[str, str]]:
    return _ENTITY_LABELS


_FHIR = "HL7 FHIR R4"

# domain name → where its codes come from. Order inside a domain follows the
# source (sets are sorted). Keep domain names stable: they are the future
# ``ref_codes.domain`` values.
_SPECS: dict[str, _Spec] = {
    # documents
    "document_category": _with(
        _from("oncofiles.models", "DocumentCategory", _values),
        labels=_category_labels,
        deprecated=lambda: _obj("oncofiles.gdrive_folders", "CATEGORY_MERGES"),
    ),
    "sync_state": _here(
        ("synced", "pending", "conflict", "deleted_remote"),
        "models.Document.sync_state, sync.py",
    ),
    "cross_reference_relationship": _here(
        ("same_visit", "follow_up", "supersedes", "related", "contradicts"),
        "doc_analysis.RELATIONSHIPS_SYSTEM_PROMPT",
    ),
    "document_reference_kind": _here(("vaccination",), "tools/enhance_tools.py"),
    # facts (clinical_records + hard-data tables)
    "verification": _with(
        _Spec(
            source="oncofiles.models.Verification",
            load=lambda: typing.get_args(_obj("oncofiles.models", "Verification")),
        ),
        labels=_verification_labels,
    ),
    "clinical_record_type": _Spec(
        source="oncofiles.audit_redaction.ENUM_VALUES['record_type'] "
        "| oncofiles.fhir.terminology.OBSERVATION_RECORD_TYPES",
        load=lambda: (
            frozenset(_obj("oncofiles.audit_redaction", "ENUM_VALUES")["record_type"])
            | frozenset(_obj("oncofiles.fhir.terminology", "OBSERVATION_RECORD_TYPES"))
        ),
    ),
    "clinical_record_status": _here(
        ("active", "resolved", "suspected", "confirmed", "normal", "abnormal", "high", "low"),
        "tools/clinical_records.add_clinical_record (documented, never validated)",
    ),
    "observation_interpretation": _with(
        _from("oncofiles.fhir.terminology", "INTERPRETATION_DISPLAY", list),
        standard="HL7 v3 ObservationInterpretation (= v2 abnormal flags)",
        labels=_interpretation_labels,
    ),
    "observation_status": _with(
        _from("oncofiles.fhir.terminology", "OBSERVATION_STATUSES"), standard=_FHIR
    ),
    "value_comparator": _with(
        _here(("<", "<=", ">", ">="), "nothing yet (#711 phase 1 column)"),
        standard=f"{_FHIR} QuantityComparator",
    ),
    "date_precision": _here(
        ("year", "month", "day", "datetime"), "curated.verifier_clinical.parse_any_date"
    ),
    "extraction_method": _here(("rule", "llm"), "tools/curated.index_patient, _hard_extract"),
    # polymorphic references (rule 3: a <x>_type + <x>_id pair with an allowlist)
    "coding_owner_type": _with(
        _from("oncofiles.database._hard_facts", "CODING_OWNER_TYPES"), labels=_entity_labels
    ),
    "identifier_owner_type": _with(
        _from("oncofiles.database._hard_facts", "IDENTIFIER_OWNER_TYPES"), labels=_entity_labels
    ),
    "provenance_target_type": _with(
        _from("oncofiles.database._hard_facts", "PROVENANCE_TARGET_TYPES"), labels=_entity_labels
    ),
    "link_entity_type": _with(
        _from("oncofiles.database._facts", "_LINK_TABLES", list), labels=_entity_labels
    ),
    "provenance_activity": _from("oncofiles.database._hard_facts", "PROVENANCE_ACTIVITIES"),
    "provenance_agent_role": _from("oncofiles.database._hard_facts", "PROVENANCE_AGENT_ROLES"),
    "identifier_system": _with(
        _from(
            "oncofiles.database._hard_facts",
            "IDENTIFIER_SYSTEMS_BY_OWNER",
            lambda by_owner: frozenset().union(*by_owner.values()),
        ),
        standard="urn:oncofiles:id (our URNs)",
        labels=_identifier_system_labels,
    ),
    "concept_map_equivalence": _with(
        _here(
            (
                "relatedto",
                "equivalent",
                "equal",
                "wider",
                "subsumes",
                "narrower",
                "specializes",
                "inexact",
                "unmatched",
                "disjoint",
            ),
            "nothing yet (#711 phase 1 column)",
        ),
        standard=f"{_FHIR} ConceptMapEquivalence",
    ),  # fmt: skip
    # conditions / stages
    "condition_category": _with(_model_literal("Condition", "category"), standard=_FHIR),
    "condition_clinical_status": _with(
        _from("oncofiles.fhir.terminology", "CONDITION_CLINICAL_CODES"), standard=_FHIR
    ),
    "condition_verification_status": _with(
        _from("oncofiles.fhir.terminology", "CONDITION_VER_CODES"), standard=_FHIR
    ),
    "cancer_stage_system": _with(
        _here(("UICC", "AJCC"), "curated.verifier_clinical.parse_stage"), standard="TNM"
    ),
    "tnm_prefix": _with(
        _here(("c", "p", "yc", "yp", "r"), "curated.verifier_clinical.parse_stage"),
        standard="TNM",
    ),
    # treatment
    "treatment_intent": _from(
        "oncofiles.curated.verifier_clinical",
        "_INTENTS",
        lambda pairs: list(dict.fromkeys(intent for _, intent in pairs)),
    ),
    "careplan_status": _with(
        _from("oncofiles.fhir.terminology", "CAREPLAN_STATUSES"), standard=_FHIR
    ),
    "treatment_cycle_status": _from("oncofiles.curated.clinical_store", "CYCLE_STATUSES"),
    "medication_event_kind": _model_literal("MedicationEvent", "kind"),
    "medication_status": _with(
        _Spec(
            source="oncofiles.fhir.terminology.MEDREQ_STATUSES | MEDADMIN_STATUSES "
            "| MEDSTATEMENT_STATUSES",
            load=lambda: frozenset().union(
                *(
                    _obj("oncofiles.fhir.terminology", name)
                    for name in ("MEDREQ_STATUSES", "MEDADMIN_STATUSES", "MEDSTATEMENT_STATUSES")
                )
            ),
        ),
        standard=_FHIR,
    ),
    "dose_normalization_basis": _here(
        ("per_m2", "per_kg", "flat"), "curated.verifier_clinical.dose_unit"
    ),
    "procedure_kind": _from(
        "oncofiles.curated.llm_extract_clinical",
        "ENUMS",
        lambda enums: enums[("procedures", "type")],
    ),
    "report_status": _with(_model_literal("Report", "status"), standard=_FHIR),
    # encounters
    "encounter_kind": _Spec(
        source="'visit' (column default) | oncofiles.tools.curated._ENCOUNTER_KIND "
        "| oncofiles.database._hard_extract.PLANNED_ENCOUNTER_KINDS",
        load=lambda: list(
            dict.fromkeys(
                (
                    "visit",
                    *_obj("oncofiles.tools.curated", "_ENCOUNTER_KIND").values(),
                    *_obj("oncofiles.database._hard_extract", "PLANNED_ENCOUNTER_KINDS"),
                )
            )
        ),
    ),
    "encounter_status": _here(("planned",), "database._hard_extract.insert_planned_encounter"),
    # curated pipeline and sync
    "curated_run_trigger": _here(
        (
            "manual",
            "mcp",
            "dashboard",
            "auto",
            "auto-preview",
            "nightly",
            "nightly-preview",
            "reocr",
            "reread",
            "clinical",
            "scorecard",
        ),
        "tools/curated.py (insert_curated_run call sites)",
    ),  # fmt: skip
    "curated_run_status": _here(
        ("running", "ok", "partial", "aborted", "failed", "interrupted"),
        "database._curated, tools/curated.py, curated.runner.RunReport",
    ),
    "sync_trigger": _here(
        ("scheduled", "manual", "nightly", "startup", "curated"),
        "database._operational.insert_sync_history call sites",
    ),
    "sync_status": _here(("running", "completed", "failed", "timeout"), "database._operational"),
    # session diary
    "diary_entry_type": _from("oncofiles.models", "EntryType", _values),
    "diary_participant": _with(
        _Spec(
            source="oncofiles.tools.conversations.DIARY_PARTICIPANTS (+ deprecated 'oncoteam')",
            load=lambda: (*_obj("oncofiles.tools.conversations", "DIARY_PARTICIPANTS"), "oncoteam"),
        ),
        deprecated=("oncoteam",),
    ),
    "diary_source": _here(("live", "import"), "tools/conversations.py, import_transcripts.py"),
    "diary_session_type": _here(("patient", "technical"), "tools/conversations.py"),
    # operations and accounts
    "auth_method": _here(
        ("oauth", "patient_bearer", "static_bearer", "stdio", "none"),
        "persistent_oauth.caller_auth_method",
    ),
    "tool_call_status": _here(("ok", "error"), "audit_middleware.py"),
    "prompt_call_type": _from("oncofiles.models", "PromptCallType", _values),
    "prompt_status": _here(("ok", "error", "dedup_hit"), "prompt_logger, prompt_dedup"),
    "audit_action": _here(
        ("create", "update", "delete", "restore"), "database._clinical_records, _facts"
    ),
    "patient_tier": _from("oncofiles.budget", "TIER_CAPS_EUR", list),
    "patient_type": _here(("oncology", "general"), "patient_context (patient_type)"),
    "language": _from("oncofiles.i18n", "STRINGS", list),
    "newsletter_status": _here(
        ("pending", "verified", "unsubscribed"), "migration 055 (column default + timestamps)"
    ),
    "oauth_provider": _here(("google",), "models.OAuthToken.provider"),
    "mcp_token_type": _here(("access", "refresh"), "persistent_oauth.py"),
    "onboarding_event_type": _here(
        (
            "created",
            "oauth_ok",
            "folder_set",
            "first_sync",
            "first_ai",
            "stuck_24h",
            "oauth_failure",
            "doc_limit_hit",
        ),
        "migration 070 / server.py",
    ),  # fmt: skip
    "calendar_event_status": _with(
        _here(("confirmed", "tentative", "cancelled"), "calendar_sync.py (Google's value)"),
        standard="Google Calendar API Event.status",
    ),
    "device_kind": _here(("llm-extractor",), "curated.clinical_store.device_id_for"),
}

# (table, column) → domain. Every enum-like column is here, in
# REFERENCE_CODE_COLUMNS or in FREE_TEXT_BY_DESIGN (lint: test_schema_conventions).
_FACT_TABLES = (
    "clinical_records", "conditions", "cancer_stages", "encounters", "medication_events",
    "procedures", "reports", "specimens", "treatment_courses", "treatment_cycles",
)  # fmt: skip
COLUMN_DOMAINS: dict[tuple[str, str], str] = {
    **{(t, "verification"): "verification" for t in _FACT_TABLES},
    ("documents", "category"): "document_category",
    ("documents", "sync_state"): "sync_state",
    ("document_cross_references", "relationship"): "cross_reference_relationship",
    ("document_references", "kind"): "document_reference_kind",
    ("clinical_records", "record_type"): "clinical_record_type",
    ("fact_codes", "category"): "clinical_record_type",
    ("clinical_records", "status"): "clinical_record_status",
    ("clinical_records", "flag"): "observation_interpretation",
    ("clinical_records", "interpretation_code"): "observation_interpretation",
    ("lab_values", "flag"): "observation_interpretation",
    ("clinical_records", "result_status"): "observation_status",
    ("clinical_records", "value_comparator"): "value_comparator",
    ("clinical_records", "effective_precision"): "date_precision",
    ("encounters", "source"): "extraction_method",
    ("links", "source"): "extraction_method",
    ("codings", "owner_type"): "coding_owner_type",
    ("identifiers", "owner_type"): "identifier_owner_type",
    ("provenance", "target_type"): "provenance_target_type",
    ("links", "from_type"): "link_entity_type",
    ("links", "to_type"): "link_entity_type",
    ("provenance", "activity"): "provenance_activity",
    ("provenance", "agent_role"): "provenance_agent_role",
    ("identifiers", "system"): "identifier_system",
    ("codings", "equivalence"): "concept_map_equivalence",
    ("conditions", "category"): "condition_category",
    ("conditions", "clinical_status"): "condition_clinical_status",
    ("conditions", "clinical_certainty"): "condition_verification_status",
    ("cancer_stages", "system"): "cancer_stage_system",
    ("cancer_stages", "prefix"): "tnm_prefix",
    ("treatment_courses", "intent"): "treatment_intent",
    ("treatment_courses", "status"): "careplan_status",
    ("treatment_cycles", "status"): "treatment_cycle_status",
    ("medication_events", "kind"): "medication_event_kind",
    ("medication_events", "status"): "medication_status",
    ("medication_events", "normalization_basis"): "dose_normalization_basis",
    ("procedures", "kind"): "procedure_kind",
    ("reports", "status"): "report_status",
    ("encounters", "kind"): "encounter_kind",
    ("encounters", "status"): "encounter_status",
    ("curated_runs", "trigger"): "curated_run_trigger",
    ("curated_runs", "status"): "curated_run_status",
    ("sync_history", "sync_trigger"): "sync_trigger",
    ("sync_history", "status"): "sync_status",
    ("conversation_entries", "entry_type"): "diary_entry_type",
    ("conversation_entries", "participant"): "diary_participant",
    ("conversation_entries", "source"): "diary_source",
    ("conversation_entries", "session_type"): "diary_session_type",
    ("activity_log", "auth_method"): "auth_method",
    ("activity_log", "status"): "tool_call_status",
    ("prompt_log", "call_type"): "prompt_call_type",
    ("prompt_log", "status"): "prompt_status",
    ("clinical_record_audit", "action"): "audit_action",
    ("patients", "tier"): "patient_tier",
    ("patients", "profile"): "patient_type",
    ("patients", "preferred_lang"): "language",
    ("newsletter_subscribers", "status"): "newsletter_status",
    ("oauth_tokens", "provider"): "oauth_provider",
    ("mcp_oauth_tokens", "token_type"): "mcp_token_type",
    ("onboarding_events", "event_type"): "onboarding_event_type",
    ("calendar_entries", "status"): "calendar_event_status",
    ("devices", "kind"): "device_kind",
}

# Codes used as keys into a reference table or an external code system that this
# registry does not enumerate (rule 4: fine when stable and controlled).
REFERENCE_CODE_COLUMNS: dict[tuple[str, str], str] = {
    ("fact_codes", "code"): "primary key of the fact-code reference table (seeded by migrations)",
    ("clinical_records", "code"): "fact_codes.code (no FK yet; legacy, see conventions doc)",
    ("codings", "code"): "a code in the system named by codings.system (ICD-10, LOINC, HGVS …)",
    ("codings", "system"): "code-system URI / OID (FHIR Coding.system); open set, "
    "export filtered by fhir.terminology.allowed_system",
    ("organizations", "code"): "natural key of a shared organisation (UNIQUE)",
    ("departments", "code"): "natural key of a department within its organisation (UNIQUE)",
    ("documents", "mime_type"): "IANA media type",
}

# Enum-like by name, free text on purpose. A column holding a printed value
# keeps it verbatim; a coded value goes to codings or a *_code column.
FREE_TEXT_BY_DESIGN: dict[tuple[str, str], str] = {
    ("clinical_records", "source"): "caller's provenance tag; open family "
    "(manual, ai-extract, curated-extract, mcp-*, import-*)",
    ("clinical_record_notes", "source"): "caller's provenance tag (dashboard, mcp-*, …)",
    ("clinical_record_audit", "source"): "provenance tag of the writer (copied from the fact "
    "or the review tool)",
    ("documents", "source"): "added in migration 026 for the import channel, never written",
    ("newsletter_subscribers", "source"): "landing-page placement sent by the browser (≤ 50 chars)",
    ("research_entries", "source"): "external source name; Oncoteam sunset table (#721), read only",
    ("treatment_events", "event_type"): "caller-chosen label in the deprecated table "
    "(replaced by treatment_courses / cycles, #711)",
    ("clinical_analyses", "analysis_type"): "caller-chosen label, Oncoteam sunset table (#721)",
    ("clinical_analyses", "produced_by"): "caller-chosen producer, Oncoteam sunset table (#721)",
    ("fact_rereads", "engine"): "OCR engine / model identifier string (open set, like "
    "prompt_log.model)",
    ("codings", "mapped_by"): "name of the device or person that mapped the code",
    ("medication_events", "route"): "route AS PRINTED ('i.v.', 'p.o.'); the coded EDQM route "
    "belongs in codings (legacy name, rule 6 would say route_printed)",
    ("conditions", "laterality"): "printed laterality, shown next to body_site",
    ("procedures", "laterality"): "printed laterality, shown next to body_site",
    ("procedures", "outcome"): "printed outcome (FHIR Procedure.outcome is a CodeableConcept)",
    ("treatment_courses", "setting"): "no writer yet; register a domain before coding it",
    ("reports", "category"): "no writer yet; FHIR DiagnosticReport.category is a "
    "CodeableConcept, register a domain before coding it",
    ("practitioners", "role"): "no writer yet; printed role of the doctor",
    ("organizations", "kind"): "no writer yet",
    ("links", "relation"): "no production writer yet (#603); register a domain with the first one",
}


# ── labels ───────────────────────────────────────────────────────────────

_CATEGORY_EN = {
    "labs": "Lab results",
    "report": "Medical reports",
    "imaging": "Imaging",
    "pathology": "Pathology",
    "genetics": "Genetic tests",
    "hereditary_genetics": "Hereditary genetics",
    "surgery": "Surgery",
    "consultation": "Consultations",
    "prescription": "Prescriptions",
    "referral": "Referrals",
    "discharge": "Discharge reports",
    "chemo_sheet": "Chemotherapy protocols",
    "reference": "Reference material",
    "advocate": "Patient advocate notes",
    "other": "Other",
    "vaccination": "Vaccination",
    "dental": "Dental records",
    "preventive": "Preventive check-ups",
    "surgical_report": "Surgery (legacy alias)",
    "discharge_summary": "Discharge reports (legacy alias)",
}
_INTERPRETATION_SK = {
    "H": "Vysoká hodnota",
    "L": "Nízka hodnota",
    "HH": "Kriticky vysoká hodnota",
    "LL": "Kriticky nízka hodnota",
    "A": "Abnormálna hodnota",
    "AA": "Kriticky abnormálna hodnota",
    "N": "V norme",
}
_ENTITY_LABELS: dict[str, tuple[str, str]] = {
    "condition": ("Diagnóza", "Condition"),
    "cancer_stage": ("Štádium nádoru", "Cancer stage"),
    "treatment_course": ("Liečebný režim", "Treatment course"),
    "treatment_cycle": ("Cyklus liečby", "Treatment cycle"),
    "medication_event": ("Predpis alebo podanie lieku", "Medication event"),
    "procedure": ("Výkon", "Procedure"),
    "specimen": ("Vzorka", "Specimen"),
    "report": ("Správa z vyšetrenia", "Diagnostic report"),
    "fact": ("Klinický údaj", "Clinical fact"),
    "document": ("Dokument", "Document"),
    "encounter": ("Návšteva alebo pobyt", "Encounter"),
    "practitioner": ("Lekár", "Practitioner"),
    "organization": ("Zdravotnícke zariadenie", "Organisation"),
    "department": ("Oddelenie", "Department"),
    "device": ("Nástroj (extraktor)", "Device (extractor)"),
    "fact_code": ("Kód údaja", "Fact code"),
    "treatment_event": ("Liečebná udalosť", "Treatment event"),
    "conversation": ("Záznam v denníku", "Diary entry"),
}

# domain → code → (label_sk, label_en), for domains without a label source elsewhere
_LABELS: dict[str, dict[str, tuple[str, str]]] = {
    "sync_state": {
        "synced": ("Synchronizovaný", "Synced"),
        "pending": ("Čaká na synchronizáciu", "Pending"),
        "conflict": ("Konflikt", "Conflict"),
        "deleted_remote": ("Zmazaný na Drive", "Deleted on Drive"),
    },
    "cross_reference_relationship": {
        "same_visit": ("Tá istá návšteva", "Same visit"),
        "follow_up": ("Nadväzuje", "Follow-up"),
        "supersedes": ("Nahrádza", "Supersedes"),
        "related": ("Súvisí", "Related"),
        "contradicts": ("V rozpore", "Contradicts"),
    },
    "document_reference_kind": {"vaccination": ("Očkovanie", "Vaccination")},
    "clinical_record_type": {
        "lab": ("Laboratórna hodnota", "Lab value"),
        "finding": ("Nález", "Finding"),
        "medication": ("Liek", "Medication"),
        "treatment_event": ("Liečebná udalosť", "Treatment event"),
        "biomarker": ("Biomarker", "Biomarker"),
        "condition": ("Diagnóza", "Condition"),
        "procedure": ("Výkon", "Procedure"),
        "imaging": ("Zobrazovacie vyšetrenie", "Imaging"),
        "pathology": ("Patológia", "Pathology"),
        "genetic_variant": ("Genetický variant", "Genetic variant"),
        "allergy": ("Alergia", "Allergy"),
        "vital": ("Vitálna funkcia", "Vital sign"),
        "tumor_marker": ("Nádorový marker", "Tumour marker"),
    },
    "clinical_record_status": {
        "active": ("Aktívne", "Active"),
        "resolved": ("Vyriešené", "Resolved"),
        "suspected": ("Podozrenie", "Suspected"),
        "confirmed": ("Potvrdené", "Confirmed"),
        "normal": ("V norme", "Normal"),
        "abnormal": ("Abnormálne", "Abnormal"),
        "high": ("Zvýšené", "High"),
        "low": ("Znížené", "Low"),
    },
    "observation_status": {
        "registered": ("Zaregistrovaný", "Registered"),
        "preliminary": ("Predbežný", "Preliminary"),
        "final": ("Konečný", "Final"),
        "amended": ("Pozmenený", "Amended"),
        "corrected": ("Opravený", "Corrected"),
        "cancelled": ("Zrušený", "Cancelled"),
        "entered-in-error": ("Zadaný omylom", "Entered in error"),
        "unknown": ("Neznámy", "Unknown"),
    },
    "value_comparator": {
        "<": ("Menej ako", "Less than"),
        "<=": ("Menej alebo rovné", "Less than or equal to"),
        ">": ("Viac ako", "Greater than"),
        ">=": ("Viac alebo rovné", "Greater than or equal to"),
    },
    "date_precision": {
        "year": ("Rok", "Year"),
        "month": ("Mesiac", "Month"),
        "day": ("Deň", "Day"),
        "datetime": ("Dátum a čas", "Date and time"),
    },
    "extraction_method": {
        "rule": ("Pravidlový parser", "Rule parser"),
        "llm": ("Lokálny jazykový model", "Local language model"),
    },
    "provenance_activity": {
        "extract": ("Extrakcia", "Extraction"),
        "reread": ("Opätovné prečítanie", "Re-read"),
        "verify": ("Overenie", "Verification"),
        "correct": ("Oprava", "Correction"),
        "import": ("Import", "Import"),
        "manual": ("Ručné zadanie", "Manual entry"),
    },
    "provenance_agent_role": {
        "assembler": ("Zostavovateľ", "Assembler"),
        "verifier": ("Overovateľ", "Verifier"),
        "author": ("Autor", "Author"),
        "enterer": ("Zapisovateľ", "Enterer"),
    },
    "concept_map_equivalence": {
        "relatedto": ("Súvisí", "Related to"),
        "equivalent": ("Ekvivalentný", "Equivalent"),
        "equal": ("Rovnaký", "Equal"),
        "wider": ("Širší", "Wider"),
        "subsumes": ("Zahŕňa", "Subsumes"),
        "narrower": ("Užší", "Narrower"),
        "specializes": ("Špecializuje", "Specializes"),
        "inexact": ("Nepresný", "Inexact"),
        "unmatched": ("Bez zhody", "Unmatched"),
        "disjoint": ("Disjunktný", "Disjoint"),
    },
    "condition_category": {
        "problem-list-item": ("Položka zoznamu problémov", "Problem list item"),
        "encounter-diagnosis": ("Diagnóza pri návšteve", "Encounter diagnosis"),
    },
    "condition_clinical_status": {
        "active": ("Aktívna", "Active"),
        "recurrence": ("Recidíva", "Recurrence"),
        "relapse": ("Relaps", "Relapse"),
        "inactive": ("Neaktívna", "Inactive"),
        "remission": ("Remisia", "Remission"),
        "resolved": ("Vyliečená", "Resolved"),
    },
    "condition_verification_status": {
        "unconfirmed": ("Nepotvrdená", "Unconfirmed"),
        "provisional": ("Predbežná", "Provisional"),
        "differential": ("Diferenciálna", "Differential"),
        "confirmed": ("Potvrdená", "Confirmed"),
        "refuted": ("Vyvrátená", "Refuted"),
        "entered-in-error": ("Zadaná omylom", "Entered in error"),
    },
    "cancer_stage_system": {"UICC": ("UICC", "UICC"), "AJCC": ("AJCC", "AJCC")},
    "tnm_prefix": {
        "c": ("Klinické (c)", "Clinical (c)"),
        "p": ("Patologické (p)", "Pathological (p)"),
        "yc": ("Klinické po liečbe (yc)", "Clinical after therapy (yc)"),
        "yp": ("Patologické po liečbe (yp)", "Pathological after therapy (yp)"),
        "r": ("Recidíva (r)", "Recurrence (r)"),
    },
    "treatment_intent": {
        "neoadjuvant": ("Neoadjuvantná", "Neoadjuvant"),
        "adjuvant": ("Adjuvantná", "Adjuvant"),
        "palliative": ("Paliatívna", "Palliative"),
        "curative": ("Kuratívna", "Curative"),
    },
    "careplan_status": {
        "draft": ("Návrh", "Draft"),
        "active": ("Prebieha", "Active"),
        "on-hold": ("Pozastavená", "On hold"),
        "revoked": ("Zrušená", "Revoked"),
        "completed": ("Ukončená", "Completed"),
        "entered-in-error": ("Zadaná omylom", "Entered in error"),
        "unknown": ("Neznámy stav", "Unknown"),
    },
    "treatment_cycle_status": {
        "planned": ("Plánovaný", "Planned"),
        "administered": ("Podaný", "Administered"),
        "postponed": ("Odložený", "Postponed"),
        "cancelled": ("Zrušený", "Cancelled"),
    },
    "medication_event_kind": {
        "request": ("Predpis", "Request"),
        "administration": ("Podanie", "Administration"),
        "statement": ("Záznam o užívaní", "Statement"),
        "dispense": ("Výdaj", "Dispense"),
    },
    "medication_status": {
        "active": ("Aktívny", "Active"),
        "on-hold": ("Pozastavený", "On hold"),
        "cancelled": ("Zrušený", "Cancelled"),
        "completed": ("Ukončený", "Completed"),
        "entered-in-error": ("Zadaný omylom", "Entered in error"),
        "stopped": ("Vysadený", "Stopped"),
        "draft": ("Návrh", "Draft"),
        "unknown": ("Neznámy", "Unknown"),
        "in-progress": ("Prebieha", "In progress"),
        "not-done": ("Nepodaný", "Not done"),
        "intended": ("Zamýšľaný", "Intended"),
        "not-taken": ("Neužitý", "Not taken"),
    },
    "dose_normalization_basis": {
        "per_m2": ("Na m² povrchu tela", "Per m² body surface area"),
        "per_kg": ("Na kg hmotnosti", "Per kg body weight"),
        "flat": ("Fixná dávka", "Flat dose"),
    },
    "procedure_kind": {
        "surgery": ("Operácia", "Surgery"),
        "biopsy": ("Biopsia", "Biopsy"),
        "imaging": ("Zobrazovacie vyšetrenie", "Imaging"),
        "radiotherapy": ("Rádioterapia", "Radiotherapy"),
        "endoscopy": ("Endoskopia", "Endoscopy"),
        "other": ("Iný výkon", "Other"),
    },
    "report_status": {
        "registered": ("Zaregistrovaná", "Registered"),
        "partial": ("Čiastočná", "Partial"),
        "preliminary": ("Predbežná", "Preliminary"),
        "final": ("Konečná", "Final"),
        "amended": ("Pozmenená", "Amended"),
        "corrected": ("Opravená", "Corrected"),
        "appended": ("Doplnená", "Appended"),
        "cancelled": ("Zrušená", "Cancelled"),
        "entered-in-error": ("Zadaná omylom", "Entered in error"),
        "unknown": ("Neznámy stav", "Unknown"),
    },
    "encounter_kind": {
        "visit": ("Návšteva", "Visit"),
        "lab_draw": ("Odber", "Lab draw"),
        "imaging": ("Zobrazovacie vyšetrenie", "Imaging"),
        "procedure": ("Výkon", "Procedure"),
        "chemo_cycle": ("Cyklus chemoterapie", "Chemotherapy cycle"),
        "stay": ("Hospitalizácia", "Hospital stay"),
        "other": ("Iné", "Other"),
    },
    "encounter_status": {"planned": ("Plánovaná", "Planned")},
    "curated_run_trigger": {
        "manual": ("Ručne", "Manual"),
        "mcp": ("Nástroj MCP", "MCP tool"),
        "dashboard": ("Dashboard", "Dashboard"),
        "auto": ("Priebežný režim", "Continuous mode"),
        "auto-preview": ("Náhľad priebežného režimu", "Continuous-mode preview"),
        "nightly": ("Nočný beh", "Nightly run"),
        "nightly-preview": ("Náhľad nočného behu", "Nightly preview"),
        "reocr": ("Nové OCR", "Re-OCR"),
        "reread": ("Opätovné prečítanie", "Re-read"),
        "clinical": ("Klinická extrakcia", "Clinical extraction"),
        "scorecard": ("Hodnotenie extrakcie", "Extraction scorecard"),
    },
    "curated_run_status": {
        "running": ("Prebieha", "Running"),
        "ok": ("Úspešný", "OK"),
        "partial": ("Čiastočný", "Partial"),
        "aborted": ("Zastavený kontrolou", "Aborted"),
        "failed": ("Zlyhal", "Failed"),
        "interrupted": ("Prerušený reštartom", "Interrupted"),
    },
    "sync_trigger": {
        "scheduled": ("Plánovaná", "Scheduled"),
        "manual": ("Ručná", "Manual"),
        "nightly": ("Nočná", "Nightly"),
        "startup": ("Pri štarte", "Startup"),
        "curated": ("Kurátorovaný priečinok", "Curated folder"),
    },
    "sync_status": {
        "running": ("Prebieha", "Running"),
        "completed": ("Dokončená", "Completed"),
        "failed": ("Zlyhala", "Failed"),
        "timeout": ("Vypršal čas", "Timed out"),
    },
    "diary_entry_type": {
        "summary": ("Zhrnutie", "Summary"),
        "decision": ("Rozhodnutie", "Decision"),
        "progress": ("Postup", "Progress"),
        "question": ("Otázka", "Question"),
        "note": ("Poznámka", "Note"),
    },
    "diary_participant": {
        "claude.ai": ("Claude.ai", "Claude.ai"),
        "claude-code": ("Claude Code", "Claude Code"),
        "chatgpt": ("ChatGPT", "ChatGPT"),
        "gemini": ("Gemini", "Gemini"),
        "other": ("Iný", "Other"),
        "oncoteam": ("Oncoteam (ukončený)", "Oncoteam (retired)"),
    },
    "diary_source": {
        "live": ("Priamo zo session", "Live session"),
        "import": ("Import prepisu", "Transcript import"),
    },
    "diary_session_type": {
        "patient": ("Pacientská", "Patient"),
        "technical": ("Technická", "Technical"),
    },
    "auth_method": {
        "oauth": ("OAuth", "OAuth"),
        "patient_bearer": ("Token pacienta", "Patient token"),
        "static_bearer": ("Statický token", "Static bearer token"),
        "stdio": ("Lokálne (stdio)", "Local (stdio)"),
        "none": ("Bez overenia", "None"),
    },
    "tool_call_status": {"ok": ("OK", "OK"), "error": ("Chyba", "Error")},
    "prompt_call_type": {
        "ocr": ("OCR", "OCR"),
        "summary_tags": ("Zhrnutie a štítky", "Summary and tags"),
        "structured_metadata": ("Štruktúrované metadáta", "Structured metadata"),
        "filename_description": ("Popis do názvu súboru", "Filename description"),
        "email_classify": ("Klasifikácia e-mailu", "E-mail classification"),
        "calendar_classify": ("Klasifikácia udalosti", "Calendar classification"),
        "doc_composition": ("Zloženie dokumentu", "Document composition"),
        "doc_consolidation": ("Zlúčenie dokumentov", "Document consolidation"),
        "doc_relationships": ("Vzťahy dokumentov", "Document relationships"),
        "doc_classification": ("Klasifikácia dokumentu", "Document classification"),
        "vaccination_events": ("Očkovania", "Vaccination events"),
        "lab_values_extraction": ("Extrakcia laboratórnych hodnôt", "Lab value extraction"),
    },
    "prompt_status": {
        "ok": ("OK", "OK"),
        "error": ("Chyba", "Error"),
        "dedup_hit": ("Odpoveď z cache", "Deduplicated (cache hit)"),
    },
    "audit_action": {
        "create": ("Vytvorenie", "Create"),
        "update": ("Úprava", "Update"),
        "delete": ("Vymazanie", "Delete"),
        "restore": ("Obnovenie", "Restore"),
    },
    "patient_tier": {
        "free_onboarding": ("Úvodné obdobie zdarma", "Free onboarding"),
        "free": ("Zdarma", "Free"),
        "paid_basic": ("Platený základ", "Paid basic"),
        "paid_pro": ("Platený pro", "Paid pro"),
        "admin": ("Administrátor", "Admin"),
    },
    "patient_type": {
        "oncology": ("Onkologický pacient", "Oncology"),
        "general": ("Všeobecné zdravie", "General health"),
    },
    "language": {"en": ("Angličtina", "English"), "sk": ("Slovenčina", "Slovak")},
    "newsletter_status": {
        "pending": ("Čaká na potvrdenie", "Pending"),
        "verified": ("Potvrdený", "Verified"),
        "unsubscribed": ("Odhlásený", "Unsubscribed"),
    },
    "oauth_provider": {"google": ("Google", "Google")},
    "mcp_token_type": {
        "access": ("Prístupový token", "Access token"),
        "refresh": ("Obnovovací token", "Refresh token"),
    },
    "onboarding_event_type": {
        "created": ("Účet vytvorený", "Created"),
        "oauth_ok": ("Google pripojený", "Google connected"),
        "folder_set": ("Priečinok nastavený", "Folder set"),
        "first_sync": ("Prvá synchronizácia", "First sync"),
        "first_ai": ("Prvé spracovanie AI", "First AI processing"),
        "stuck_24h": ("Zaseknuté 24 h", "Stuck for 24 h"),
        "oauth_failure": ("Chyba pripojenia Google", "OAuth failure"),
        "doc_limit_hit": ("Dosiahnutý limit dokumentov", "Document limit reached"),
    },
    "calendar_event_status": {
        "confirmed": ("Potvrdená", "Confirmed"),
        "tentative": ("Predbežná", "Tentative"),
        "cancelled": ("Zrušená", "Cancelled"),
    },
    "device_kind": {"llm-extractor": ("Extraktor s jazykovým modelom", "LLM extractor")},
}


# ── registry ─────────────────────────────────────────────────────────────


def _codes_of(raw: Any) -> list[str]:
    if isinstance(raw, set | frozenset):
        return sorted(raw)
    return list(dict.fromkeys(raw))


def _labels_for(name: str, spec: _Spec) -> dict[str, tuple[str, str]]:
    return spec.labels() if spec.labels else _LABELS.get(name, {})


@functools.cache
def registry() -> dict[str, Domain]:
    """Every domain, loaded from its source on first call."""
    out: dict[str, Domain] = {}
    for name, spec in _SPECS.items():
        labels = _labels_for(name, spec)
        deprecated = spec.deprecated() if callable(spec.deprecated) else spec.deprecated
        out[name] = Domain(
            name=name,
            codes=tuple(Code(code, *labels.get(code, ("", ""))) for code in _codes_of(spec.load())),
            source=spec.source,
            standard=spec.standard,
            deprecated=frozenset(deprecated),
        )
    return out


def domain(name: str) -> Domain:
    return registry()[name]


def domain_for(table: str, column: str) -> Domain | None:
    name = COLUMN_DOMAINS.get((table, column))
    return registry()[name] if name else None


def codes(name: str) -> frozenset[str]:
    return domain(name).code_set


def label(name: str, code: str, lang: str = "sk") -> str | None:
    return domain(name).label(code, lang)


def rows() -> Iterator[tuple[str, str, str, str]]:
    """(domain, code, label_sk, label_en): the content of the future ``ref_codes`` view."""
    for d in registry().values():
        for c in d.codes:
            yield d.name, c.code, c.label_sk, c.label_en


def label_gaps() -> dict[str, dict[str, set[str]]]:
    """Per domain: codes without a label (``missing``) and labels of no code (``unused``)."""
    gaps: dict[str, dict[str, set[str]]] = {}
    for name, spec in _SPECS.items():
        labels = _labels_for(name, spec)
        have = {c for c, (sk, en) in labels.items() if sk and en}
        codes_ = set(domain(name).code_set)
        missing, unused = codes_ - have, set(labels) - codes_
        if spec.labels is _entity_labels:
            unused = set()  # one shared label table serves several domains
        if missing or unused:
            gaps[name] = {"missing": missing, "unused": unused}
    return gaps
