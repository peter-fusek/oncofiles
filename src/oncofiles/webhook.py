"""Fire-and-forget webhook to notify oncoteam of new documents."""

from __future__ import annotations

import asyncio
import logging

from oncofiles.config import ONCOTEAM_WEBHOOK_TOKEN, ONCOTEAM_WEBHOOK_URL

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0  # seconds


async def _notify_oncoteam_async(
    document_id: int,
    filename: str | None = None,
    category: str | None = None,
) -> None:
    """POST to oncoteam's document-webhook endpoint. Never raises."""
    if not ONCOTEAM_WEBHOOK_URL:
        return

    import httpx

    payload: dict = {"document_id": document_id}
    if filename:
        payload["filename"] = filename
    if category:
        payload["category"] = category
    import datetime as _dt

    payload["uploaded_at"] = _dt.datetime.now(_dt.UTC).isoformat()

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                ONCOTEAM_WEBHOOK_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {ONCOTEAM_WEBHOOK_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            if response.status_code < 300:
                logger.info(
                    "Oncoteam webhook OK for doc %d: %s",
                    document_id,
                    response.text[:100],
                )
            else:
                logger.warning(
                    "Oncoteam webhook %d for doc %d: %s",
                    response.status_code,
                    document_id,
                    response.text[:200],
                )
    except Exception:
        logger.warning("Oncoteam webhook failed for doc %d", document_id, exc_info=True)


def notify_oncoteam(
    document_id: int,
    filename: str | None = None,
    category: str | None = None,
) -> None:
    """Fire-and-forget notification to oncoteam. Non-blocking."""
    if not ONCOTEAM_WEBHOOK_URL:
        return
    try:
        asyncio.create_task(_notify_oncoteam_async(document_id, filename, category))
    except RuntimeError:
        # No running event loop (e.g. during tests)
        logger.debug("Cannot create webhook task — no event loop")
