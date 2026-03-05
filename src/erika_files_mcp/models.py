"""Pydantic data models for document metadata."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DocumentCategory(StrEnum):
    """Medical document categories."""

    LABS = "labs"
    REPORT = "report"
    IMAGING = "imaging"
    PATHOLOGY = "pathology"
    SURGERY = "surgery"
    PRESCRIPTION = "prescription"
    REFERRAL = "referral"
    DISCHARGE = "discharge"
    OTHER = "other"


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
    entry_date: date
    entry_type: str = "note"
    title: str
    content: str
    participant: str = "claude.ai"
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


# ── Agent state (#32) ────────────────────────────────────────────────────────


class AgentState(BaseModel):
    """A key-value pair persisted by an agent across sessions."""

    id: int | None = None
    agent_id: str = "oncoteam"
    key: str
    value: str = "{}"
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Treatment events (#34) ───────────────────────────────────────────────────


class TreatmentEvent(BaseModel):
    """A structured treatment milestone (chemo cycle, surgery, scan, etc.)."""

    id: int | None = None
    event_date: date
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
