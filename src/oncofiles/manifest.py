"""Manifest export/import and metadata renderers for GDrive sync."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime

from oncofiles.database import Database

logger = logging.getLogger(__name__)

MANIFEST_VERSION = "1.0"


async def export_manifest(db: Database) -> dict:
    """Serialize all DB tables into a manifest dict."""
    documents = await db.list_documents(limit=1000)
    conversations = await db.get_conversation_timeline(limit=1000)
    treatment_events = await db.get_treatment_events_timeline(limit=1000)
    research_entries = await db.list_research_entries(limit=1000)
    agent_states = await db.list_agent_states()

    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "version": MANIFEST_VERSION,
        "documents": [_doc_to_manifest(d) for d in documents],
        "conversation_entries": [_conversation_to_manifest(e) for e in conversations],
        "treatment_events": [_treatment_to_manifest(e) for e in treatment_events],
        "research_entries": [_research_to_manifest(e) for e in research_entries],
        "agent_state": [_state_to_manifest(s) for s in agent_states],
    }


def render_manifest_json(manifest: dict) -> str:
    """Pretty-print manifest dict as JSON string."""
    return json.dumps(manifest, indent=2, ensure_ascii=False)


def parse_manifest(json_str: str) -> dict:
    """Parse a manifest JSON string back to dict (for disaster recovery)."""
    return json.loads(json_str)


def render_conversation_month(entries: list) -> str:
    """Render conversation entries for a single month as markdown with YAML frontmatter."""
    if not entries:
        return ""

    lines: list[str] = []
    for entry in entries:
        # YAML frontmatter per entry
        lines.append("---")
        lines.append(f"date: {entry.entry_date.isoformat()}")
        lines.append(f"type: {entry.entry_type}")
        lines.append(f"participant: {entry.participant}")
        if entry.session_id:
            lines.append(f"session: {entry.session_id}")
        if entry.tags:
            lines.append(f"tags: {json.dumps(entry.tags)}")
        if entry.document_ids:
            lines.append(f"documents: {json.dumps(entry.document_ids)}")
        lines.append("---")
        lines.append("")
        lines.append(f"## {entry.title}")
        lines.append("")
        lines.append(entry.content)
        lines.append("")
        lines.append("")

    return "\n".join(lines)


def render_treatment_timeline(events: list, lang: str = "en") -> str:
    """Render treatment events as a chronological markdown timeline."""
    from oncofiles.i18n import t

    if not events:
        return f"{t('treatment_timeline', lang)}\n\n{t('no_treatment_events', lang)}\n"

    lines = [t("treatment_timeline", lang), ""]
    current_date = None

    for event in events:
        date_str = event.event_date.isoformat()
        if date_str != current_date:
            current_date = date_str
            lines.append(f"## {current_date}")
            lines.append("")

        lines.append(f"### [{event.event_type}] {event.title}")
        if event.notes:
            lines.append("")
            lines.append(event.notes)
        lines.append("")

    return "\n".join(lines)


def render_research_library(entries: list, lang: str = "en") -> str:
    """Render research entries as markdown grouped by source."""
    from oncofiles.i18n import t

    if not entries:
        return f"{t('research_library', lang)}\n\n{t('no_research_entries', lang)}\n"

    by_source: dict[str, list] = defaultdict(list)
    for entry in entries:
        by_source[entry.source].append(entry)

    lines = [t("research_library", lang), ""]

    for source, source_entries in sorted(by_source.items()):
        lines.append(f"## {source}")
        lines.append("")
        for entry in source_entries:
            lines.append(f"### {entry.title}")
            if entry.external_id:
                lines.append(f"*ID: {entry.external_id}*")
            if entry.summary:
                lines.append("")
                lines.append(entry.summary)
            if entry.tags and entry.tags != "[]":
                try:
                    tag_list = json.loads(entry.tags)
                    lines.append(f"\n{t('tags', lang)}: {', '.join(tag_list)}")
                except json.JSONDecodeError:
                    pass
            lines.append("")

    return "\n".join(lines)


def group_conversations_by_month(entries: list) -> dict[str, list]:
    """Group conversation entries by YYYY-MM key."""
    by_month: dict[str, list] = defaultdict(list)
    for entry in entries:
        month_key = entry.entry_date.isoformat()[:7]
        by_month[month_key].append(entry)
    return dict(by_month)


# ── Manifest serializers ──────────────────────────────────────────────────


def _doc_to_manifest(doc) -> dict:
    return {
        "id": doc.id,
        "file_id": doc.file_id,
        "filename": doc.filename,
        "original_filename": doc.original_filename,
        "document_date": doc.document_date.isoformat() if doc.document_date else None,
        "institution": doc.institution,
        "category": doc.category.value,
        "description": doc.description,
        "mime_type": doc.mime_type,
        "size_bytes": doc.size_bytes,
        "gdrive_id": doc.gdrive_id,
        "ai_summary": doc.ai_summary,
        "ai_tags": doc.ai_tags,
        "structured_metadata": doc.structured_metadata,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


def _conversation_to_manifest(entry) -> dict:
    return {
        "id": entry.id,
        "entry_date": entry.entry_date.isoformat(),
        "entry_type": entry.entry_type,
        "title": entry.title,
        "content": entry.content,
        "participant": entry.participant,
        "session_id": entry.session_id,
        "tags": entry.tags,
        "document_ids": entry.document_ids,
        "source": entry.source,
        "source_ref": entry.source_ref,
    }


def _treatment_to_manifest(event) -> dict:
    return {
        "id": event.id,
        "event_date": event.event_date.isoformat(),
        "event_type": event.event_type,
        "title": event.title,
        "notes": event.notes,
        "metadata": event.metadata,
    }


def _research_to_manifest(entry) -> dict:
    return {
        "id": entry.id,
        "source": entry.source,
        "external_id": entry.external_id,
        "title": entry.title,
        "summary": entry.summary,
        "tags": entry.tags,
    }


def _state_to_manifest(state) -> dict:
    return {
        "agent_id": state.agent_id,
        "key": state.key,
        "value": state.value,
    }
