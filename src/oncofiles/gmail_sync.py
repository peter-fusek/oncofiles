"""Gmail sync — scan emails, classify medical relevance, process attachments."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from oncofiles.database import Database
from oncofiles.files_api import FilesClient
from oncofiles.gmail_client import GmailClient
from oncofiles.models import Document, DocumentCategory, EmailEntry

logger = logging.getLogger(__name__)

# Module-level lock
_gmail_sync_lock = asyncio.Lock()
_gmail_sync_lock_acquired_at: float = 0.0
_LOCK_TIMEOUT = 600

# Initial scan date
_INITIAL_SCAN_AFTER = "2025/12/01"

# Gmail search query for medical pre-filtering
_BASE_MEDICAL_QUERY = (
    "("
    "erika fusekova OR fusekova erika OR fusekova "  # patient name
    "OR lab OR laboratory OR výsledky OR vyšetrenie OR vysetrenie "  # lab terms
    "OR onkologia OR onkologický OR chemoterapia OR chemo "  # oncology SK
    "OR oncology OR pathology OR radiology OR CT OR MRI "  # medical EN
    "OR nemocnica OR hospital OR klinika OR ambulancia "  # places
    "OR diagnóza OR diagnosis OR biopsia OR biopsy "
    "OR recept OR prescription OR prepúšťacia OR discharge "
    "OR NOU OR OUSA OR UNB OR Medirex OR Alpha OR Synlab "  # known institutions
    "OR has:attachment filename:pdf OR has:attachment filename:jpg"  # doc attachments
    ")"
)

# Supported attachment types for document pipeline
_DOC_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/jpg",
}


def _build_gmail_query(after_date: str, learned_senders: list[str] | None = None) -> str:
    """Build Gmail search query with date filter and optional learned senders."""
    query = f"after:{after_date} {_BASE_MEDICAL_QUERY}"
    if learned_senders:
        sender_clause = " OR ".join(f"from:{s}" for s in learned_senders[:20])
        query = f"after:{after_date} ({_BASE_MEDICAL_QUERY} OR {sender_clause})"
    return query


async def _get_learned_senders(db: Database) -> list[str]:
    """Learn medical senders from previously classified medical emails."""
    from oncofiles.models import EmailQuery

    try:
        entries = await db.search_email_entries(EmailQuery(is_medical=True, limit=200))
        senders = set()
        for e in entries:
            # Extract email address from "Name <email>" format
            sender = e.sender
            if "<" in sender and ">" in sender:
                sender = sender.split("<")[1].split(">")[0]
            senders.add(sender.strip().lower())
        return list(senders)[:20]
    except Exception:
        logger.warning("Failed to learn medical senders", exc_info=True)
        return []


def _extract_email_parts(msg: dict) -> dict:
    """Extract subject, sender, recipients, body, labels, attachments from Gmail message."""
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    to = headers.get("to", "")

    # Parse recipients as JSON list
    recipients = [r.strip() for r in to.split(",") if r.strip()] if to else []

    # Extract body text
    body_text = ""
    body_snippet = msg.get("snippet", "")

    def _walk_parts(payload: dict) -> None:
        nonlocal body_text
        mime = payload.get("mimeType", "")
        if mime == "text/plain" and "data" in payload.get("body", {}):
            body_text += base64.urlsafe_b64decode(payload["body"]["data"]).decode(
                "utf-8", errors="replace"
            )
        for part in payload.get("parts", []):
            _walk_parts(part)

    _walk_parts(msg.get("payload", {}))

    # Extract attachment info
    attachments: list[dict] = []

    def _find_attachments(payload: dict) -> None:
        for part in payload.get("parts", []):
            if part.get("filename") and part.get("body", {}).get("attachmentId"):
                attachments.append(
                    {
                        "filename": part["filename"],
                        "mime_type": part.get("mimeType", ""),
                        "attachment_id": part["body"]["attachmentId"],
                        "size": part["body"].get("size", 0),
                    }
                )
            _find_attachments(part)

    _find_attachments(msg.get("payload", {}))

    # Parse date
    email_date = datetime.now(UTC)
    if "internalDate" in msg:
        import contextlib

        with contextlib.suppress(ValueError, TypeError):
            email_date = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=UTC)

    labels = msg.get("labelIds", [])

    return {
        "subject": subject,
        "sender": sender,
        "recipients": json.dumps(recipients),
        "body_text": body_text[:10000],  # limit body size
        "body_snippet": body_snippet[:500],
        "date": email_date,
        "labels": json.dumps(labels),
        "has_attachments": len(attachments) > 0,
        "attachments": attachments,
        "thread_id": msg.get("threadId", ""),
    }


async def _classify_medical_relevance(
    subject: str, snippet: str, sender: str, *, db=None
) -> tuple[bool, float]:
    """Use Haiku to classify if an email is medically relevant. Returns (is_medical, score)."""
    import anthropic

    from oncofiles.config import ANTHROPIC_API_KEY
    from oncofiles.prompt_logger import log_ai_call

    if not ANTHROPIC_API_KEY:
        return False, 0.0

    prompt = (
        "Classify this email as MEDICAL or NOT_MEDICAL.\n"
        "Medical = related to healthcare, treatment, lab results, appointments, "
        "prescriptions, hospital communication, insurance claims for medical services.\n\n"
        f"Subject: {subject}\n"
        f"From: {sender}\n"
        f"Snippet: {snippet[:300]}\n\n"
        'Respond with ONLY a JSON object: {"medical": true/false, "confidence": 0.0-1.0, '
        '"reason": "brief reason"}'
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        start = time.time()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        duration_ms = int((time.time() - start) * 1000)

        text = response.content[0].text.strip()
        log_ai_call(
            db,
            call_type="email_classify",
            model="claude-haiku-4-5-20251001",
            system_prompt="",
            user_prompt=prompt[:500],
            raw_response=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            duration_ms=duration_ms,
        )

        # Parse JSON response — handle markdown code blocks
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        return result.get("medical", False), result.get("confidence", 0.0)
    except Exception:
        logger.warning("Email classification failed for: %s", subject[:60], exc_info=True)
        return False, 0.0


async def _process_attachment(
    db: Database,
    files: FilesClient,
    gmail: GmailClient,
    message_id: str,
    attachment: dict,
    email_entry: EmailEntry,
) -> Document | None:
    """Download email attachment and process through document pipeline."""
    from oncofiles.filename_parser import parse_filename
    from oncofiles.webhook import notify_oncoteam

    filename = attachment["filename"]
    mime_type = attachment["mime_type"]

    # Only process supported document types
    if mime_type not in _DOC_MIME_TYPES:
        return None

    try:
        # Download attachment
        content_bytes = await asyncio.to_thread(
            gmail.get_attachment, message_id, attachment["attachment_id"]
        )

        # Upload to Anthropic Files API
        file_metadata = files.upload(io.BytesIO(content_bytes), filename, mime_type)

        # Parse filename for metadata
        parsed = parse_filename(filename)

        # Build description — use parsed or derive from email context
        description = parsed.description or f"Email attachment from {email_entry.sender}"

        # Create document
        doc = Document(
            file_id=file_metadata.id,
            filename=filename,
            original_filename=filename,
            document_date=parsed.document_date or email_entry.date.date(),
            institution=parsed.institution,
            category=parsed.category or DocumentCategory.OTHER,
            description=description,
            mime_type=mime_type,
            size_bytes=len(content_bytes),
        )
        doc = await db.insert_document(doc)

        # Notify oncoteam
        notify_oncoteam(doc.id, doc.filename, doc.category.value)

        # Link document to email entry
        linked = json.loads(email_entry.linked_document_ids)
        linked.append(doc.id)
        await db.db.execute(
            "UPDATE email_entries SET linked_document_ids = ? WHERE id = ?",
            (json.dumps(linked), email_entry.id),
        )
        await db.db.commit()

        logger.info("Gmail attachment processed: %s -> doc #%d", filename, doc.id)
        return doc
    except Exception:
        logger.warning(
            "Failed to process attachment %s from message %s",
            filename,
            message_id,
            exc_info=True,
        )
        return None


async def gmail_sync(
    db: Database,
    files: FilesClient,
    gmail: GmailClient,
    initial: bool = False,
    *,
    patient_id: str = "erika",
) -> dict:
    """Sync medical emails from Gmail.

    Args:
        db: Database instance
        files: Anthropic Files API client
        gmail: Gmail API client
        initial: If True, scan from Dec 1 2025. Otherwise, scan last 24h.

    Returns:
        Stats dict with counts of processed/skipped/medical emails.
    """
    global _gmail_sync_lock_acquired_at

    # Acquire lock
    if _gmail_sync_lock.locked():
        elapsed = time.monotonic() - _gmail_sync_lock_acquired_at
        if elapsed < _LOCK_TIMEOUT:
            logger.info("Gmail sync already running (%.0fs), skipping", elapsed)
            return {"skipped": True, "reason": "already_running"}

    async with _gmail_sync_lock:
        _gmail_sync_lock_acquired_at = time.monotonic()
        return await _gmail_sync_inner(db, files, gmail, initial, patient_id=patient_id)


async def _gmail_sync_inner(
    db: Database,
    files: FilesClient,
    gmail: GmailClient,
    initial: bool,
    *,
    patient_id: str = "erika",
) -> dict:
    """Inner sync logic."""
    stats: dict = {
        "total_fetched": 0,
        "already_stored": 0,
        "classified": 0,
        "medical": 0,
        "not_medical": 0,
        "attachments_processed": 0,
        "errors": 0,
    }

    # Determine date range
    if initial:
        after_date = _INITIAL_SCAN_AFTER
    else:
        # Incremental: scan last 24 hours (overlap for safety)
        yesterday = datetime.now(UTC).replace(hour=0, minute=0, second=0) - timedelta(days=1)
        after_date = yesterday.strftime("%Y/%m/%d")

    # Learn from existing data
    learned_senders = await _get_learned_senders(db)
    query = _build_gmail_query(after_date, learned_senders)

    logger.info(
        "Gmail sync started (initial=%s, after=%s, learned_senders=%d)",
        initial,
        after_date,
        len(learned_senders),
    )

    # Fetch all matching messages (paginated)
    all_message_ids: list[str] = []
    page_token: str | None = None

    while True:
        try:
            result = await asyncio.to_thread(
                gmail.list_messages, query=query, max_results=100, page_token=page_token
            )
        except Exception:
            logger.error("Gmail list_messages failed", exc_info=True)
            stats["errors"] += 1
            break

        messages = result.get("messages", [])
        all_message_ids.extend(m["id"] for m in messages)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    stats["total_fetched"] = len(all_message_ids)
    logger.info("Gmail sync: fetched %d message IDs", len(all_message_ids))

    # Process each message
    for msg_id in all_message_ids:
        try:
            # Dedup: skip if already in DB
            existing = await db.get_email_entry_by_gmail_id(msg_id, patient_id=patient_id)
            if existing:
                stats["already_stored"] += 1
                continue

            # Fetch full message
            msg = await asyncio.to_thread(gmail.get_message, msg_id)
            parts = _extract_email_parts(msg)

            # Classify medical relevance
            is_medical, confidence = await _classify_medical_relevance(
                parts["subject"], parts["body_snippet"], parts["sender"], db=db
            )
            stats["classified"] += 1

            if is_medical:
                stats["medical"] += 1
            else:
                stats["not_medical"] += 1

            # Store email entry
            entry = EmailEntry(
                gmail_message_id=msg_id,
                thread_id=parts["thread_id"],
                subject=parts["subject"],
                sender=parts["sender"],
                recipients=parts["recipients"],
                date=parts["date"],
                body_snippet=parts["body_snippet"],
                body_text=parts["body_text"],
                labels=parts["labels"],
                has_attachments=parts["has_attachments"],
                is_medical=is_medical,
                ai_relevance_score=confidence,
            )
            entry.patient_id = patient_id
            entry = await db.upsert_email_entry(entry)

            # Process attachments for medical emails
            if is_medical and parts["attachments"]:
                for att in parts["attachments"]:
                    doc = await _process_attachment(db, files, gmail, msg_id, att, entry)
                    if doc:
                        stats["attachments_processed"] += 1

        except Exception:
            logger.warning("Gmail sync: error processing message %s", msg_id, exc_info=True)
            stats["errors"] += 1

    logger.info(
        "Gmail sync done: %d fetched, %d stored, %d medical, %d attachments, %d errors",
        stats["total_fetched"],
        stats["already_stored"],
        stats["medical"],
        stats["attachments_processed"],
        stats["errors"],
    )
    return stats
