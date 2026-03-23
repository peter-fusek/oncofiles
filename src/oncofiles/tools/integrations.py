"""Gmail and Calendar integration tools."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import CalendarQuery, EmailQuery
from oncofiles.tools._helpers import _clamp_limit, _get_db, _get_patient_id, _parse_date


async def integration_status(ctx: Context) -> str:
    """Show which Google services are connected and entry counts.

    Returns the status of Drive, Gmail, and Calendar integrations,
    including whether each is authorized and how many entries are stored.
    """
    db = _get_db(ctx)
    token = await db.get_oauth_token(patient_id=_get_patient_id())
    granted = json.loads(token.granted_scopes) if token else []

    from oncofiles.oauth import SCOPE_CALENDAR, SCOPE_DRIVE, SCOPE_GMAIL

    gmail_count = None
    cal_count = None
    if SCOPE_GMAIL in granted:
        gmail_count = await db.count_email_entries(patient_id=_get_patient_id())
    if SCOPE_CALENDAR in granted:
        cal_count = await db.count_calendar_entries(patient_id=_get_patient_id())

    return json.dumps(
        {
            "token_present": bool(token),
            "token_expiry": token.token_expiry.isoformat()
            if token and token.token_expiry
            else None,
            "services": {
                "drive": {"enabled": SCOPE_DRIVE in granted},
                "gmail": {
                    "enabled": SCOPE_GMAIL in granted,
                    "entry_count": gmail_count,
                },
                "calendar": {
                    "enabled": SCOPE_CALENDAR in granted,
                    "entry_count": cal_count,
                },
            },
        }
    )


async def gmail_auth_enable(ctx: Context) -> str:
    """Start Gmail authorization flow. Returns a URL the user must visit.

    After visiting the URL and completing Google's consent screen, Gmail
    read access will be enabled. Call integration_status() to verify.

    WARNING: This grants read access to ALL emails in the Gmail account.
    """
    from oncofiles.config import GOOGLE_OAUTH_CLIENT_ID
    from oncofiles.oauth import GMAIL_SCOPES, SCOPE_GMAIL, get_auth_url_for_scopes

    if not GOOGLE_OAUTH_CLIENT_ID:
        return json.dumps({"error": "OAuth not configured. Set GOOGLE_OAUTH_CLIENT_ID."})

    db = _get_db(ctx)
    token = await db.get_oauth_token(patient_id=_get_patient_id())
    if token:
        granted = json.loads(token.granted_scopes)
        if SCOPE_GMAIL in granted:
            return json.dumps({"status": "already_enabled", "service": "gmail"})

    auth_url = get_auth_url_for_scopes(GMAIL_SCOPES)
    return json.dumps(
        {
            "status": "authorization_required",
            "service": "gmail",
            "auth_url": auth_url,
            "warning": "This grants read access to ALL emails in your Gmail account.",
            "instruction": (
                "Visit auth_url to grant Gmail read access."
                " The server must be restarted after authorization."
            ),
        }
    )


async def calendar_auth_enable(ctx: Context) -> str:
    """Start Calendar authorization flow. Returns a URL the user must visit.

    After visiting the URL and completing Google's consent screen, Calendar
    read access will be enabled. Call integration_status() to verify.

    WARNING: This grants read access to ALL events in Google Calendar.
    """
    from oncofiles.config import GOOGLE_OAUTH_CLIENT_ID
    from oncofiles.oauth import CALENDAR_SCOPES, SCOPE_CALENDAR, get_auth_url_for_scopes

    if not GOOGLE_OAUTH_CLIENT_ID:
        return json.dumps({"error": "OAuth not configured. Set GOOGLE_OAUTH_CLIENT_ID."})

    db = _get_db(ctx)
    token = await db.get_oauth_token(patient_id=_get_patient_id())
    if token:
        granted = json.loads(token.granted_scopes)
        if SCOPE_CALENDAR in granted:
            return json.dumps({"status": "already_enabled", "service": "calendar"})

    auth_url = get_auth_url_for_scopes(CALENDAR_SCOPES)
    return json.dumps(
        {
            "status": "authorization_required",
            "service": "calendar",
            "auth_url": auth_url,
            "warning": "This grants read access to ALL events in your Google Calendar.",
            "instruction": (
                "Visit auth_url to grant Calendar read access."
                " The server must be restarted after authorization."
            ),
        }
    )


async def search_emails(
    ctx: Context,
    query: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sender: str | None = None,
    is_medical: bool | None = None,
    limit: int = 50,
) -> str:
    """Search stored email entries by text, date, sender, or medical relevance.

    Args:
        query: Text to search in subject, body snippet, and sender.
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        sender: Filter by sender email or name (partial match).
        is_medical: Filter to medical emails only when True.
        limit: Maximum results to return.
    """
    try:
        db = _get_db(ctx)
        eq = EmailQuery(
            text=query,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            sender=sender,
            is_medical=is_medical,
            limit=_clamp_limit(limit),
        )
        entries = await db.search_email_entries(eq, patient_id=_get_patient_id())
    except ValueError as e:
        return json.dumps({"error": str(e)})
    items = [
        {
            "id": e.id,
            "subject": e.subject,
            "sender": e.sender,
            "date": e.date.isoformat(),
            "body_snippet": e.body_snippet[:200],
            "has_attachments": e.has_attachments,
            "is_medical": e.is_medical,
            "ai_summary": e.ai_summary,
        }
        for e in entries
    ]
    return json.dumps({"emails": items, "total": len(items)})


async def get_email(ctx: Context, email_entry_id: int) -> str:
    """Get full details of a stored email entry by ID.

    Args:
        email_entry_id: The email entry ID.
    """
    db = _get_db(ctx)
    entry = await db.get_email_entry(email_entry_id)
    if not entry:
        return json.dumps({"error": f"Email entry not found: {email_entry_id}"})
    return json.dumps(
        {
            "id": entry.id,
            "gmail_message_id": entry.gmail_message_id,
            "thread_id": entry.thread_id,
            "subject": entry.subject,
            "sender": entry.sender,
            "recipients": json.loads(entry.recipients),
            "date": entry.date.isoformat(),
            "body_snippet": entry.body_snippet,
            "body_text": entry.body_text[:2000],
            "labels": json.loads(entry.labels),
            "has_attachments": entry.has_attachments,
            "is_medical": entry.is_medical,
            "ai_summary": entry.ai_summary,
            "ai_relevance_score": entry.ai_relevance_score,
            "structured_metadata": json.loads(entry.structured_metadata)
            if entry.structured_metadata
            else None,
            "linked_document_ids": json.loads(entry.linked_document_ids),
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
    )


async def search_calendar_events(
    ctx: Context,
    query: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    is_medical: bool | None = None,
    limit: int = 50,
) -> str:
    """Search stored calendar entries by text, date, or medical relevance.

    Args:
        query: Text to search in summary and description.
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        is_medical: Filter to medical events only when True.
        limit: Maximum results to return.
    """
    try:
        db = _get_db(ctx)
        cq = CalendarQuery(
            text=query,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            is_medical=is_medical,
            limit=_clamp_limit(limit),
        )
        entries = await db.search_calendar_entries(cq, patient_id=_get_patient_id())
    except ValueError as e:
        return json.dumps({"error": str(e)})
    items = [
        {
            "id": e.id,
            "summary": e.summary,
            "start_time": e.start_time.isoformat(),
            "end_time": e.end_time.isoformat() if e.end_time else None,
            "location": e.location,
            "status": e.status,
            "is_medical": e.is_medical,
            "treatment_event_id": e.treatment_event_id,
            "ai_summary": e.ai_summary,
        }
        for e in entries
    ]
    return json.dumps({"events": items, "total": len(items)})


async def get_calendar_event(ctx: Context, calendar_entry_id: int) -> str:
    """Get full details of a stored calendar entry by ID.

    Args:
        calendar_entry_id: The calendar entry ID.
    """
    db = _get_db(ctx)
    entry = await db.get_calendar_entry(calendar_entry_id)
    if not entry:
        return json.dumps({"error": f"Calendar entry not found: {calendar_entry_id}"})
    return json.dumps(
        {
            "id": entry.id,
            "google_event_id": entry.google_event_id,
            "summary": entry.summary,
            "description": entry.description,
            "start_time": entry.start_time.isoformat(),
            "end_time": entry.end_time.isoformat() if entry.end_time else None,
            "location": entry.location,
            "attendees": json.loads(entry.attendees),
            "recurrence": entry.recurrence,
            "status": entry.status,
            "is_medical": entry.is_medical,
            "ai_summary": entry.ai_summary,
            "treatment_event_id": entry.treatment_event_id,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
    )


def register(mcp):
    mcp.tool()(integration_status)
    mcp.tool()(gmail_auth_enable)
    mcp.tool()(calendar_auth_enable)
    mcp.tool()(search_emails)
    mcp.tool()(get_email)
    mcp.tool()(search_calendar_events)
    mcp.tool()(get_calendar_event)
