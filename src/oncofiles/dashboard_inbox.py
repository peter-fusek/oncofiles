"""Inbox files as rows of the dashboard document table (#921 follow-up).

The curated read model indexes filed folders only, so a file waiting in a
curated inbox has no ``documents`` row until a run files it. The curated probe
keeps an in-memory snapshot of the inbox per patient
(``tools.curated.inbox_snapshot``); ``GET /api/documents`` turns each entry
into a row here. Pure functions: no DB, no Drive, no model call.

An inbox row:

* has no document id: ``id`` is ``inbox:<drive id>`` and ``inbox`` is true, so
  the dashboard never asks ``/api/doc-detail`` for it (it shows a short
  explanation instead);
* is ``in_inbox`` (in progress, "Waiting to be processed"), or ``attention``
  with ``review_needed`` when its sidecar holds it for a human;
* counts in the summary like any other row, so ready + in_progress +
  attention == total still holds.

An entry whose Drive id already backs a ``documents`` row (the run filed it
and the indexer mirrored it before the next probe refreshed the snapshot: a
Drive move keeps the id) is dropped, so a file never shows twice.
"""

from __future__ import annotations

from oncofiles import document_stage

ID_PREFIX = "inbox:"


def stage(review: bool) -> dict:
    """``steps`` / ``ready`` / ``stage`` / ``attention_reason`` of an inbox file."""
    steps = {"extracted": False, "named": False, "filed": False}
    if review:
        return {
            "steps": steps,
            "ready": False,
            "stage": document_stage.STAGE_ATTENTION,
            "attention_reason": document_stage.REASON_REVIEW_NEEDED,
        }
    return {
        "steps": steps,
        "ready": False,
        "stage": document_stage.STAGE_IN_INBOX,
        "attention_reason": None,
    }


def row(entry: dict) -> dict:
    """One table row for one snapshot entry (the same keys as a document row)."""
    st = stage(bool(entry.get("review")))
    return {
        "id": ID_PREFIX + str(entry.get("id") or ""),
        "inbox": True,
        "filename": entry.get("name") or "",
        "category": None,
        "date": None,
        "institution": None,
        "gdrive_id": None,
        "size": int(entry.get("size") or 0),
        "mime": entry.get("mime") or "",
        "has_ocr": False,
        "has_ai": False,
        "has_metadata": False,
        "has_date": False,
        "has_institution": False,
        "is_synced": False,
        "is_standard_name": False,
        "fully_complete": False,
        "uploaded_at": entry.get("modified") or None,
        "curated": True,
        **st,
    }


def rows(entries: list[dict] | None, known_drive_ids: set[str]) -> list[dict]:
    """Rows for the entries whose Drive id backs no document row yet."""
    out = []
    seen: set[str] = set()
    for e in entries or []:
        drive_id = str(e.get("id") or "")
        if not drive_id or drive_id in known_drive_ids or drive_id in seen:
            continue
        seen.add(drive_id)
        out.append(row(e))
    return out
