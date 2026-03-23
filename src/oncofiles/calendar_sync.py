"""Calendar sync — scan events, classify medical, create treatment events."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from oncofiles.calendar_client import CalendarClient
from oncofiles.database import Database
from oncofiles.models import CalendarEntry, TreatmentEvent

logger = logging.getLogger(__name__)

# Module-level lock
_calendar_sync_lock = asyncio.Lock()
_calendar_sync_lock_acquired_at: float = 0.0
_LOCK_TIMEOUT = 600

# Initial scan date (RFC3339)
_INITIAL_SCAN_AFTER = "2025-12-01T00:00:00Z"

# Medical keywords for pre-filtering (both SK and EN)
_MEDICAL_KEYWORDS = [
    "chemo",
    "chemoterapia",
    "onkologia",
    "oncology",
    "vyšetrenie",
    "vysetrenie",
    "kontrola",
    "ambulancia",
    "CT",
    "MRI",
    "USG",
    "PET",
    "biopsia",
    "biopsy",
    "operácia",
    "surgery",
    "lab",
    "laboratórium",
    "NOU",
    "OUSA",
    "UNB",
    "nemocnica",
    "hospital",
    "klinika",
    "doktor",
    "doctor",
    "MUDr",
    "onkolog",
    "Fusekova",
    "fusekova",
    "Erika",
]


async def _classify_event_medical(
    summary: str, description: str, location: str, *, db=None
) -> tuple[bool, float, str]:
    """Use Haiku to classify if a calendar event is medically relevant.

    Returns (is_medical, confidence, event_type).
    """
    import anthropic

    from oncofiles.config import ANTHROPIC_API_KEY
    from oncofiles.prompt_logger import log_ai_call

    if not ANTHROPIC_API_KEY:
        # Fallback: keyword matching
        text = f"{summary} {description} {location}".lower()
        for kw in _MEDICAL_KEYWORDS:
            if kw.lower() in text:
                return True, 0.7, "other_medical"
        return False, 0.0, "not_medical"

    prompt = (
        "Classify this calendar event as MEDICAL or NOT_MEDICAL.\n"
        "Medical = doctor appointment, hospital visit, lab test, treatment session, "
        "medical consultation, scan, surgery, any healthcare-related event.\n\n"
        f"Summary: {summary}\n"
        f"Description: {(description or '')[:300]}\n"
        f"Location: {location or 'not specified'}\n\n"
        "Respond with ONLY a JSON object: "
        '{"medical": true/false, "confidence": 0.0-1.0, '
        '"event_type": "chemo|surgery|scan|consult|lab|other_medical|not_medical"}'
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
            call_type="calendar_classify",
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
        return (
            result.get("medical", False),
            result.get("confidence", 0.0),
            result.get("event_type", "not_medical"),
        )
    except Exception:
        logger.warning("Calendar event classification failed: %s", summary[:60], exc_info=True)
        # Fallback to keyword match
        text_combined = f"{summary} {description} {location}".lower()
        for kw in _MEDICAL_KEYWORDS:
            if kw.lower() in text_combined:
                return True, 0.5, "other_medical"
        return False, 0.0, "not_medical"


async def _auto_create_treatment_event(
    db: Database,
    entry: CalendarEntry,
    event_type: str = "consult",
) -> int | None:
    """Auto-create a treatment event from a medical calendar entry if not already linked."""
    if entry.treatment_event_id:
        return entry.treatment_event_id  # Already linked

    try:
        event = TreatmentEvent(
            event_date=entry.start_time.date(),
            event_type=event_type if event_type != "not_medical" else "consult",
            title=entry.summary or "Medical appointment",
            notes=f"Auto-created from calendar event. Location: {entry.location or 'N/A'}",
            metadata=json.dumps(
                {
                    "source": "calendar_sync",
                    "google_event_id": entry.google_event_id,
                    "calendar_entry_id": entry.id,
                }
            ),
        )
        saved = await db.insert_treatment_event(event)

        # Link back to calendar entry
        await db.db.execute(
            "UPDATE calendar_entries SET treatment_event_id = ? WHERE id = ?",
            (saved.id, entry.id),
        )
        await db.db.commit()

        logger.info(
            "Auto-created treatment event #%d from calendar: %s", saved.id, entry.summary[:60]
        )
        return saved.id
    except Exception:
        logger.warning(
            "Failed to create treatment event from calendar: %s",
            entry.summary[:60],
            exc_info=True,
        )
        return None


async def calendar_sync(
    db: Database,
    calendar: CalendarClient,
    initial: bool = False,
    *,
    patient_id: str = "erika",
) -> dict:
    """Sync medical events from Google Calendar.

    Args:
        db: Database instance
        calendar: Google Calendar API client
        initial: If True, scan from Dec 1 2025. Otherwise, scan last 7 days.

    Returns:
        Stats dict with counts of processed/skipped/medical events.
    """
    global _calendar_sync_lock_acquired_at

    # Acquire lock
    if _calendar_sync_lock.locked():
        elapsed = time.monotonic() - _calendar_sync_lock_acquired_at
        if elapsed < _LOCK_TIMEOUT:
            logger.info("Calendar sync already running (%.0fs), skipping", elapsed)
            return {"skipped": True, "reason": "already_running"}

    async with _calendar_sync_lock:
        _calendar_sync_lock_acquired_at = time.monotonic()
        return await _calendar_sync_inner(db, calendar, initial, patient_id=patient_id)


async def _calendar_sync_inner(
    db: Database,
    calendar: CalendarClient,
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
        "treatment_events_created": 0,
        "errors": 0,
    }

    # Determine time range
    if initial:
        time_min = _INITIAL_SCAN_AFTER
    else:
        # Incremental: scan last 7 days (overlap for safety)
        time_min = (datetime.now(UTC) - timedelta(days=7)).isoformat()

    # Include future events (next 90 days)
    time_max = (datetime.now(UTC) + timedelta(days=90)).isoformat()

    logger.info("Calendar sync started (initial=%s, time_min=%s)", initial, time_min[:19])

    # Fetch all events (paginated)
    all_events: list[dict] = []
    page_token: str | None = None

    while True:
        try:
            result = await asyncio.to_thread(
                calendar.list_events,
                time_min=time_min,
                time_max=time_max,
                max_results=250,
                page_token=page_token,
            )
        except Exception:
            logger.error("Calendar list_events failed", exc_info=True)
            stats["errors"] += 1
            break

        events = result.get("items", [])
        all_events.extend(events)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    stats["total_fetched"] = len(all_events)
    logger.info("Calendar sync: fetched %d events", len(all_events))

    for event in all_events:
        try:
            event_id = event.get("id", "")
            if not event_id:
                continue

            # Dedup: skip if already in DB
            existing = await db.get_calendar_entry_by_google_id(event_id, patient_id=patient_id)
            if existing:
                stats["already_stored"] += 1
                continue

            summary = event.get("summary", "")
            description = event.get("description", "")
            location = event.get("location", "")

            # Parse start/end times
            start = event.get("start", {})
            end = event.get("end", {})
            start_str = start.get("dateTime") or start.get("date", "")
            end_str = end.get("dateTime") or end.get("date", "")

            try:
                start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                start_time = datetime.now(UTC)

            try:
                end_time = (
                    datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else None
                )
            except (ValueError, AttributeError):
                end_time = None

            # Attendees
            attendees = [a.get("email", "") for a in event.get("attendees", [])]

            # Classify
            is_medical, confidence, event_type = await _classify_event_medical(
                summary, description, location, db=db
            )
            stats["classified"] += 1

            if is_medical:
                stats["medical"] += 1
            else:
                stats["not_medical"] += 1

            # Store
            entry = CalendarEntry(
                google_event_id=event_id,
                summary=summary,
                description=(description or "")[:2000],
                start_time=start_time,
                end_time=end_time,
                location=location,
                attendees=json.dumps(attendees),
                recurrence=json.dumps(event.get("recurrence")) if event.get("recurrence") else None,
                status=event.get("status", "confirmed"),
                is_medical=is_medical,
                ai_summary=None,
            )
            entry.patient_id = patient_id
            entry = await db.upsert_calendar_entry(entry)

            # Auto-create treatment event for medical appointments
            if is_medical and entry.id:
                te_id = await _auto_create_treatment_event(db, entry, event_type)
                if te_id:
                    stats["treatment_events_created"] += 1

        except Exception:
            logger.warning(
                "Calendar sync: error processing event %s", event.get("id", "?"), exc_info=True
            )
            stats["errors"] += 1

    logger.info(
        "Calendar sync done: %d fetched, %d stored, %d medical, %d treatment events, %d errors",
        stats["total_fetched"],
        stats["already_stored"],
        stats["medical"],
        stats["treatment_events_created"],
        stats["errors"],
    )
    return stats
