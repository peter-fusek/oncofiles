"""Pydantic data models for document metadata."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DocumentCategory(StrEnum):
    """Medical document categories.

    Taxonomy (v4.6.0):
    - pathology = tissue morphology, histology, biopsy reports
    - genetics = somatic/tumor molecular testing (KRAS, MSI, HER2, BRAF panels)
    - hereditary_genetics = germline/inherited DNA testing (BRCA1/2, Lynch, Li-Fraumeni)
    - surgery = all surgical documentation (reports, protocols, notes)
    - discharge = all discharge docs (summaries, epikrízy)
    - consultation = doctor visits, clinical notes, follow-ups
    """

    LABS = "labs"
    REPORT = "report"
    IMAGING = "imaging"
    PATHOLOGY = "pathology"
    GENETICS = "genetics"
    HEREDITARY_GENETICS = "hereditary_genetics"
    SURGERY = "surgery"
    CONSULTATION = "consultation"
    PRESCRIPTION = "prescription"
    REFERRAL = "referral"
    DISCHARGE = "discharge"
    CHEMO_SHEET = "chemo_sheet"
    REFERENCE = "reference"
    ADVOCATE = "advocate"
    OTHER = "other"
    # General health categories (v5.2+)
    VACCINATION = "vaccination"
    DENTAL = "dental"
    PREVENTIVE = "preventive"
    # Legacy aliases — kept for backward compat (DB may have these values)
    SURGICAL_REPORT = "surgical_report"
    DISCHARGE_SUMMARY = "discharge_summary"


class Document(BaseModel):
    """A medical document stored in the Files API with local metadata."""

    id: int | None = None
    file_id: str = Field(description="Anthropic Files API file_id")
    filename: str = Field(description="Canonical filename (YYYYMMDD convention)")
    original_filename: str = Field(description="Original filename before normalization")
    document_date: date | None = Field(default=None, description="Date from filename")
    institution: str | None = Field(default=None, description="Medical institution code")
    category: DocumentCategory = DocumentCategory.OTHER
    description: str | None = Field(default=None, description="Human-readable description")
    mime_type: str = "application/pdf"
    size_bytes: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    gdrive_id: str | None = Field(default=None, description="Google Drive file ID")
    gdrive_modified_time: datetime | None = None
    gdrive_md5: str | None = Field(
        default=None, description="GDrive md5Checksum for content change detection"
    )
    sync_state: str = Field(
        default="synced",
        description="Sync state: synced, pending, conflict, deleted_remote",
    )
    last_synced_at: datetime | None = None
    gdrive_parent_outside_root: bool = Field(
        default=False,
        description=(
            "True when the GDrive file exists but lives outside the patient's sync "
            "root (e.g., uploaded by a 3rd-party service into a different folder). "
            "Distinct from sync_state='deleted_remote' (file gone entirely). #477"
        ),
    )
    ai_summary: str | None = Field(default=None, description="AI-generated document summary")
    ai_tags: str | None = Field(default=None, description="JSON array of AI-generated tags")
    ai_processed_at: datetime | None = None
    structured_metadata: str | None = Field(
        default=None, description="JSON object with structured medical metadata"
    )
    deleted_at: datetime | None = Field(default=None, description="Soft-delete timestamp")
    version: int = Field(default=1, description="Document version number")
    previous_version_id: int | None = Field(
        default=None, description="ID of the previous version (soft-deleted)"
    )
    # Multi-document grouping (splitting / consolidation)
    group_id: str | None = Field(
        default=None, description="Shared UUID for documents in the same logical group"
    )
    part_number: int | None = Field(default=None, description="1-based position within a group")
    total_parts: int | None = Field(default=None, description="Total count of parts in the group")
    split_source_doc_id: int | None = Field(
        default=None, description="Original document ID this was split from"
    )

    @property
    def content_block(self) -> dict:
        """Return the MCP/API content block to reference this file."""
        if self.mime_type.startswith("image/"):
            return {
                "type": "image",
                "source": {"type": "file", "file_id": self.file_id},
            }
        return {
            "type": "document",
            "source": {"type": "file", "file_id": self.file_id},
        }


class ParsedFilename(BaseModel):
    """Result of parsing a YYYYMMDD_institution_category_description.ext filename."""

    document_date: date | None = None
    institution: str | None = None
    category: DocumentCategory = DocumentCategory.OTHER
    description: str | None = None
    extension: str = ""


class SearchQuery(BaseModel):
    """Search parameters for document lookup."""

    text: str | None = Field(default=None, description="Full-text search query")
    institution: str | None = None
    category: DocumentCategory | None = None
    date_from: date | None = None
    date_to: date | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


# ── Conversation archive (#37) ───────────────────────────────────────────────


class EntryType(StrEnum):
    """Conversation entry types (soft convention, not a strict enum)."""

    SUMMARY = "summary"
    DECISION = "decision"
    PROGRESS = "progress"
    QUESTION = "question"
    NOTE = "note"


class ConversationEntry(BaseModel):
    """A diary/worklog entry in the conversation archive."""

    id: int | None = None
    entry_date: date | None = None
    entry_type: str = "note"
    title: str
    content: str
    participant: str = "claude.ai"
    session_type: str = "patient"  # "patient" or "technical"
    session_id: str | None = None
    tags: list[str] | None = None
    document_ids: list[int] | None = None
    source: str | None = None
    source_ref: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ConversationQuery(BaseModel):
    """Search parameters for conversation entries."""

    text: str | None = None
    entry_type: str | None = None
    participant: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    tags: list[str] | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


# ── Patients (#134) ──────────────────────────────────────────────────────────


class Patient(BaseModel):
    """A patient managed by this oncofiles instance."""

    patient_id: str
    slug: str = ""
    display_name: str
    caregiver_email: str | None = None
    diagnosis_summary: str | None = None
    is_active: bool = True
    preferred_lang: str = "sk"
    # Billing tier (migration 061, #442): free_onboarding | free | paid_basic |
    # paid_pro | admin. Existing prod patients were grandfathered to 'admin'.
    tier: str = "free_onboarding"
    onboarding_ends_at: datetime | None = None
    upgraded_at: datetime | None = None
    tier_notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Agent state (#32) ────────────────────────────────────────────────────────


class AgentState(BaseModel):
    """A key-value pair persisted by an agent across sessions."""

    id: int | None = None
    agent_id: str = "oncoteam"
    key: str
    value: str = "{}"
    patient_id: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Treatment events (#34) ───────────────────────────────────────────────────


class TreatmentEvent(BaseModel):
    """A structured treatment milestone (chemo cycle, surgery, scan, etc.)."""

    id: int | None = None
    event_date: date | None = None
    event_type: str
    title: str
    notes: str = ""
    metadata: str = "{}"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TreatmentEventQuery(BaseModel):
    """Search parameters for treatment events."""

    event_type: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    limit: int = Field(default=50, ge=1, le=200)


# ── Research entries (#33) ───────────────────────────────────────────────────


class ResearchEntry(BaseModel):
    """A research article or clinical trial saved by an agent."""

    id: int | None = None
    source: str
    external_id: str = ""
    title: str
    summary: str = ""
    tags: str = "[]"
    raw_data: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ResearchQuery(BaseModel):
    """Search parameters for research entries."""

    text: str | None = None
    source: str | None = None
    limit: int = Field(default=20, ge=1, le=200)


# ── Activity log (#38) ──────────────────────────────────────────────────────


class ActivityLogEntry(BaseModel):
    """An immutable record of an agent tool call."""

    id: int | None = None
    session_id: str
    agent_id: str
    tool_name: str
    input_summary: str = ""
    output_summary: str = ""
    duration_ms: int | None = None
    status: str = "ok"
    error_message: str | None = None
    tags: str = "[]"
    created_at: datetime | None = None


# ── OAuth tokens (#12) ────────────────────────────────────────────────────


class OAuthToken(BaseModel):
    """OAuth token pair for a user's Google Drive access."""

    id: int | None = None
    patient_id: str = ""
    provider: str = "google"
    access_token: str
    refresh_token: str
    token_expiry: datetime | None = None
    gdrive_folder_id: str | None = None
    gdrive_folder_name: str | None = None
    owner_email: str | None = None
    granted_scopes: str = "[]"
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Email entries (#104) ─────────────────────────────────────────────────


class EmailEntry(BaseModel):
    """A Gmail email entry stored for medical record tracking."""

    id: int | None = None
    patient_id: str = ""
    gmail_message_id: str
    thread_id: str = ""
    subject: str = ""
    sender: str = ""
    recipients: str = "[]"
    date: datetime
    body_snippet: str = ""
    body_text: str = ""
    labels: str = "[]"
    has_attachments: bool = False
    ai_summary: str | None = None
    ai_relevance_score: float | None = None
    structured_metadata: str | None = None
    linked_document_ids: str = "[]"
    is_medical: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EmailQuery(BaseModel):
    """Search parameters for email entries."""

    text: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    is_medical: bool | None = None
    sender: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


# ── Calendar entries (#104) ──────────────────────────────────────────────


class CalendarEntry(BaseModel):
    """A Google Calendar event stored for medical record tracking."""

    id: int | None = None
    patient_id: str = ""
    google_event_id: str
    summary: str = ""
    description: str = ""
    start_time: datetime
    end_time: datetime | None = None
    location: str | None = None
    attendees: str = "[]"
    recurrence: str | None = None
    status: str = "confirmed"
    ai_summary: str | None = None
    treatment_event_id: int | None = None
    is_medical: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CalendarQuery(BaseModel):
    """Search parameters for calendar entries."""

    text: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    is_medical: bool | None = None
    limit: int = Field(default=50, ge=1, le=200)


class ActivityLogQuery(BaseModel):
    """Search parameters for activity log entries."""

    session_id: str | None = None
    agent_id: str | None = None
    tool_name: str | None = None
    status: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    text: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class LabValue(BaseModel):
    """A single lab parameter value linked to a source document."""

    id: int | None = None
    document_id: int
    lab_date: date | None = None
    parameter: str
    value: float
    unit: str = ""
    reference_low: float | None = None
    reference_high: float | None = None
    flag: str = ""
    created_at: datetime | None = None


class LabTrendQuery(BaseModel):
    """Query parameters for lab trend retrieval."""

    parameter: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    limit: int = Field(default=50, ge=1, le=200)
    patient_id: str = ""


# ── Prompt observability ─────────────────────────────────────────────────────


class PromptCallType(StrEnum):
    """Types of AI calls made during document processing."""

    OCR = "ocr"
    SUMMARY_TAGS = "summary_tags"
    STRUCTURED_METADATA = "structured_metadata"
    FILENAME_DESCRIPTION = "filename_description"
    EMAIL_CLASSIFY = "email_classify"
    CALENDAR_CLASSIFY = "calendar_classify"
    DOC_COMPOSITION = "doc_composition"
    DOC_CONSOLIDATION = "doc_consolidation"
    DOC_RELATIONSHIPS = "doc_relationships"
    DOC_CLASSIFICATION = "doc_classification"
    VACCINATION_EVENTS = "vaccination_events"


class PromptLogEntry(BaseModel):
    """A single logged AI prompt call."""

    id: int | None = None
    call_type: PromptCallType
    document_id: int | None = None
    patient_id: str = ""
    model: str
    system_prompt: str = ""
    user_prompt: str = ""
    raw_response: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    result_summary: str = ""
    status: str = "ok"
    error_message: str | None = None
    created_at: datetime | None = None


class PromptLogQuery(BaseModel):
    """Query parameters for prompt log search."""

    call_type: str | None = None
    document_id: int | None = None
    status: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    text: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


# ── Clinical records (#450) ──────────────────────────────────────────────────


class ClinicalRecord(BaseModel):
    """A canonical clinical fact — lab value, biomarker, finding, etc.

    Replacement for the ad-hoc JSON blobs in ``patient_context`` and the partial
    coverage in ``lab_values`` / ``treatment_events``. Every write generates a
    row in ``clinical_record_audit`` so the full change history is queryable.
    """

    id: int | None = None
    patient_id: str
    record_type: str = Field(
        description=(
            "One of: lab, finding, medication, treatment_event, biomarker, "
            "condition, procedure, imaging, pathology, genetic_variant, "
            "allergy, vital"
        )
    )
    source_document_id: int | None = None
    occurred_at: str | None = Field(
        default=None, description="ISO date or datetime of the clinical event"
    )
    param: str | None = None
    value_num: float | None = None
    value_text: str | None = None
    unit: str | None = None
    status: str | None = None
    ref_range_low: float | None = None
    ref_range_high: float | None = None
    metadata_json: str | None = None
    source: str = Field(
        description=(
            "Provenance: manual | ai-extract | oncoteam | mcp-claude | "
            "mcp-chatgpt | import-* | migration"
        )
    )
    session_id: str | None = None
    caller_identity: str | None = None
    created_at: str | None = None
    created_by: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None
    deleted_at: str | None = None
    deleted_by: str | None = None


class ClinicalRecordQuery(BaseModel):
    """Filter for listing clinical records."""

    record_type: str | None = None
    param: str | None = None
    since: str | None = Field(default=None, description="Lower bound on occurred_at (ISO)")
    until: str | None = Field(default=None, description="Upper bound on occurred_at (ISO)")
    include_deleted: bool = False
    limit: int = Field(default=200, ge=1, le=2000)


class ClinicalRecordNote(BaseModel):
    """A free-form annotation tied to a clinical record.

    Sourceable from any session — Claude.ai chat, ChatGPT connector, caregiver
    dashboard, Oncoteam — so the caregiver can see *who said what when* about
    any single lab / biomarker / finding.
    """

    id: int | None = None
    record_id: int
    note_text: str
    tags: str | None = Field(default=None, description="JSON array of tag strings")
    source: str
    session_id: str | None = None
    mcp_conversation_ref: str | None = None
    caller_identity: str | None = None
    created_at: str | None = None
    created_by: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None
    deleted_at: str | None = None
    deleted_by: str | None = None


class ClinicalRecordAudit(BaseModel):
    """One row in the append-only change history of a clinical record."""

    id: int | None = None
    record_id: int
    action: str = Field(description="create | update | delete | restore")
    before_json: str | None = None
    after_json: str | None = None
    changed_fields: str | None = None
    reason: str | None = None
    source: str
    session_id: str | None = None
    caller_identity: str | None = None
    changed_at: str | None = None
    changed_by: str | None = None


class ClinicalAnalysis(BaseModel):
    """An analytic output over one or more clinical records.

    Lives alongside the facts in ``clinical_records`` — this is where Oncoteam
    (or any external AI) stores computed results: SII trends, lab deltas,
    biomarker safety checks, session summaries, etc. The ``record_ids`` JSON
    array traces which facts fed the analysis.
    """

    id: int | None = None
    patient_id: str
    record_ids: str | None = Field(
        default=None, description="JSON array of clinical_records.id that fed this analysis"
    )
    analysis_type: str = Field(
        description=(
            "sii_trend | ne_ly_ratio | lab_delta | biomarker_safety_check | "
            "trial_eligibility | session_note | precycle_checklist | custom"
        )
    )
    result_json: str = Field(description="JSON-serialised analytic payload")
    result_summary: str | None = None
    tags: str | None = Field(default=None, description="JSON array of tag strings")
    produced_by: str = Field(description="oncoteam | oncofiles-internal | external-ai | manual")
    session_id: str | None = None
    created_at: str | None = None
