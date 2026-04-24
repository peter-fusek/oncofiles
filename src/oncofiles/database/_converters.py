"""Row-to-model conversion functions for all database entities."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from oncofiles.models import (
    ActivityLogEntry,
    AgentState,
    CalendarEntry,
    ClinicalAnalysis,
    ClinicalRecord,
    ClinicalRecordAudit,
    ClinicalRecordNote,
    ConversationEntry,
    Document,
    DocumentCategory,
    EmailEntry,
    LabValue,
    OAuthToken,
    PromptCallType,
    PromptLogEntry,
    ResearchEntry,
    TreatmentEvent,
)

logger = logging.getLogger(__name__)


def _safe_get(row: Any, key: str, default=None):
    """Get a column value from a row, returning default if column doesn't exist."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _safe_date(value: str | None) -> date | None:
    """Parse a date string, returning None instead of crashing on invalid values."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        logger.warning("Invalid date string in DB, treating as NULL: %r", value)
        return None


def _row_to_oauth_token(row: Any) -> OAuthToken:
    """Convert a database row to an OAuthToken model."""
    return OAuthToken(
        id=row["id"],
        patient_id=row["patient_id"],
        provider=row["provider"],
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_expiry=(datetime.fromisoformat(row["token_expiry"]) if row["token_expiry"] else None),
        gdrive_folder_id=row["gdrive_folder_id"],
        gdrive_folder_name=_safe_get(row, "gdrive_folder_name"),
        owner_email=_safe_get(row, "owner_email"),
        granted_scopes=_safe_get(row, "granted_scopes", "[]"),
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )


def _row_to_agent_state(row: Any) -> AgentState:
    """Convert a database row to an AgentState model.

    Uses aliased column 'state_key' to avoid reserved-word issues with Turso.
    """
    d = dict(row)
    return AgentState(
        id=d["id"],
        agent_id=d["agent_id"],
        key=d["state_key"],
        value=d["value"],
        patient_id=d.get("patient_id", ""),
        created_at=datetime.fromisoformat(d["created_at"]) if d["created_at"] else None,
        updated_at=datetime.fromisoformat(d["updated_at"]) if d["updated_at"] else None,
    )


def _row_to_treatment_event(row: Any) -> TreatmentEvent:
    """Convert a database row to a TreatmentEvent model."""
    return TreatmentEvent(
        id=row["id"],
        event_date=_safe_date(row["event_date"]),
        event_type=row["event_type"],
        title=row["title"],
        notes=row["notes"],
        metadata=row["metadata"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )


def _row_to_research_entry(row: Any) -> ResearchEntry:
    """Convert a database row to a ResearchEntry model."""
    return ResearchEntry(
        id=row["id"],
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"],
        summary=row["summary"],
        tags=row["tags"],
        raw_data=row["raw_data"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )


def _row_to_activity_log(row: Any) -> ActivityLogEntry:
    """Convert a database row to an ActivityLogEntry model."""
    return ActivityLogEntry(
        id=row["id"],
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        tool_name=row["tool_name"],
        input_summary=row["input_summary"],
        output_summary=row["output_summary"],
        duration_ms=row["duration_ms"],
        status=row["status"],
        error_message=row["error_message"],
        tags=row["tags"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
    )


def _row_to_conversation_entry(row: Any) -> ConversationEntry:
    """Convert a database row to a ConversationEntry model."""
    return ConversationEntry(
        id=row["id"],
        entry_date=_safe_date(row["entry_date"]),
        entry_type=row["entry_type"],
        title=row["title"],
        content=row["content"],
        participant=row["participant"],
        session_type=row["session_type"] if "session_type" in dict(row) else "patient",
        session_id=row["session_id"],
        tags=json.loads(row["tags"]) if row["tags"] else None,
        document_ids=json.loads(row["document_ids"]) if row["document_ids"] else None,
        source=row["source"],
        source_ref=row["source_ref"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )


def _row_to_lab_value(row: Any) -> LabValue:
    """Convert a database row to a LabValue model."""
    return LabValue(
        id=row["id"],
        document_id=row["document_id"],
        lab_date=_safe_date(row["lab_date"]),
        parameter=row["parameter"],
        value=row["value"],
        unit=row["unit"],
        reference_low=row["reference_low"],
        reference_high=row["reference_high"],
        flag=row["flag"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
    )


def _row_to_document(row: Any) -> Document:
    """Convert a database row to a Document model."""
    row_dict = dict(row)
    return Document(
        id=row["id"],
        file_id=row["file_id"],
        filename=row["filename"],
        original_filename=row["original_filename"],
        document_date=_safe_date(row["document_date"]),
        institution=row["institution"],
        category=DocumentCategory(row["category"]),
        description=row["description"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        gdrive_id=row["gdrive_id"],
        gdrive_modified_time=(
            datetime.fromisoformat(row["gdrive_modified_time"])
            if row["gdrive_modified_time"]
            else None
        ),
        gdrive_md5=row_dict.get("gdrive_md5"),
        sync_state=row_dict.get("sync_state", "synced") or "synced",
        last_synced_at=(
            datetime.fromisoformat(row_dict["last_synced_at"])
            if row_dict.get("last_synced_at")
            else None
        ),
        gdrive_parent_outside_root=bool(row_dict.get("gdrive_parent_outside_root") or 0),
        ai_summary=row["ai_summary"],
        ai_tags=row["ai_tags"],
        ai_processed_at=(
            datetime.fromisoformat(row["ai_processed_at"]) if row["ai_processed_at"] else None
        ),
        structured_metadata=row_dict.get("structured_metadata"),
        deleted_at=(
            datetime.fromisoformat(row_dict["deleted_at"]) if row_dict.get("deleted_at") else None
        ),
        version=row_dict.get("version", 1) or 1,
        previous_version_id=row_dict.get("previous_version_id"),
        group_id=_safe_get(row, "group_id"),
        part_number=_safe_get(row, "part_number"),
        total_parts=_safe_get(row, "total_parts"),
        split_source_doc_id=_safe_get(row, "split_source_doc_id"),
    )


def _row_to_prompt_log(row: Any) -> PromptLogEntry:
    """Convert a database row to a PromptLogEntry."""
    row_dict = dict(row)
    return PromptLogEntry(
        id=row["id"],
        call_type=PromptCallType(row["call_type"]),
        document_id=row["document_id"],
        patient_id=row_dict.get("patient_id", ""),
        model=row["model"],
        system_prompt=row["system_prompt"],
        user_prompt=row["user_prompt"],
        raw_response=row["raw_response"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        duration_ms=row["duration_ms"],
        result_summary=row["result_summary"],
        status=row["status"],
        error_message=row["error_message"],
        created_at=(datetime.fromisoformat(row["created_at"]) if row["created_at"] else None),
    )


def _row_to_email_entry(row: Any) -> EmailEntry:
    """Convert a database row to an EmailEntry model."""
    return EmailEntry(
        id=row["id"],
        patient_id=row["patient_id"],
        gmail_message_id=row["gmail_message_id"],
        thread_id=row["thread_id"],
        subject=row["subject"],
        sender=row["sender"],
        recipients=row["recipients"],
        date=datetime.fromisoformat(row["date"]),
        body_snippet=row["body_snippet"],
        body_text=row["body_text"],
        labels=row["labels"],
        has_attachments=bool(row["has_attachments"]),
        ai_summary=row["ai_summary"],
        ai_relevance_score=row["ai_relevance_score"],
        structured_metadata=row["structured_metadata"],
        linked_document_ids=row["linked_document_ids"],
        is_medical=bool(row["is_medical"]),
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )


def _row_to_calendar_entry(row: Any) -> CalendarEntry:
    """Convert a database row to a CalendarEntry model."""
    return CalendarEntry(
        id=row["id"],
        patient_id=row["patient_id"],
        google_event_id=row["google_event_id"],
        summary=row["summary"],
        description=row["description"],
        start_time=datetime.fromisoformat(row["start_time"]),
        end_time=datetime.fromisoformat(row["end_time"]) if row["end_time"] else None,
        location=row["location"],
        attendees=row["attendees"],
        recurrence=row["recurrence"],
        status=row["status"],
        ai_summary=row["ai_summary"],
        treatment_event_id=row["treatment_event_id"],
        is_medical=bool(row["is_medical"]),
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )


def _row_to_clinical_record(row: Any) -> ClinicalRecord:
    """Convert a database row to a ClinicalRecord model."""
    return ClinicalRecord(
        id=row["id"],
        patient_id=row["patient_id"],
        record_type=row["record_type"],
        source_document_id=_safe_get(row, "source_document_id"),
        occurred_at=_safe_get(row, "occurred_at"),
        param=_safe_get(row, "param"),
        value_num=_safe_get(row, "value_num"),
        value_text=_safe_get(row, "value_text"),
        unit=_safe_get(row, "unit"),
        status=_safe_get(row, "status"),
        ref_range_low=_safe_get(row, "ref_range_low"),
        ref_range_high=_safe_get(row, "ref_range_high"),
        metadata_json=_safe_get(row, "metadata_json"),
        source=row["source"],
        session_id=_safe_get(row, "session_id"),
        caller_identity=_safe_get(row, "caller_identity"),
        created_at=_safe_get(row, "created_at"),
        created_by=_safe_get(row, "created_by"),
        updated_at=_safe_get(row, "updated_at"),
        updated_by=_safe_get(row, "updated_by"),
        deleted_at=_safe_get(row, "deleted_at"),
        deleted_by=_safe_get(row, "deleted_by"),
    )


def _row_to_clinical_record_note(row: Any) -> ClinicalRecordNote:
    """Convert a database row to a ClinicalRecordNote model."""
    return ClinicalRecordNote(
        id=row["id"],
        record_id=row["record_id"],
        note_text=row["note_text"],
        tags=_safe_get(row, "tags"),
        source=row["source"],
        session_id=_safe_get(row, "session_id"),
        mcp_conversation_ref=_safe_get(row, "mcp_conversation_ref"),
        caller_identity=_safe_get(row, "caller_identity"),
        created_at=_safe_get(row, "created_at"),
        created_by=_safe_get(row, "created_by"),
        updated_at=_safe_get(row, "updated_at"),
        updated_by=_safe_get(row, "updated_by"),
        deleted_at=_safe_get(row, "deleted_at"),
        deleted_by=_safe_get(row, "deleted_by"),
    )


def _row_to_clinical_record_audit(row: Any) -> ClinicalRecordAudit:
    """Convert a database row to a ClinicalRecordAudit model."""
    return ClinicalRecordAudit(
        id=row["id"],
        record_id=row["record_id"],
        action=row["action"],
        before_json=_safe_get(row, "before_json"),
        after_json=_safe_get(row, "after_json"),
        changed_fields=_safe_get(row, "changed_fields"),
        reason=_safe_get(row, "reason"),
        source=row["source"],
        session_id=_safe_get(row, "session_id"),
        caller_identity=_safe_get(row, "caller_identity"),
        changed_at=_safe_get(row, "changed_at"),
        changed_by=_safe_get(row, "changed_by"),
    )


def _row_to_clinical_analysis(row: Any) -> ClinicalAnalysis:
    """Convert a database row to a ClinicalAnalysis model."""
    return ClinicalAnalysis(
        id=row["id"],
        patient_id=row["patient_id"],
        record_ids=_safe_get(row, "record_ids"),
        analysis_type=row["analysis_type"],
        result_json=row["result_json"],
        result_summary=_safe_get(row, "result_summary"),
        tags=_safe_get(row, "tags"),
        produced_by=row["produced_by"],
        session_id=_safe_get(row, "session_id"),
        created_at=_safe_get(row, "created_at"),
    )
