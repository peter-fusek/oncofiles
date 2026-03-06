"""AI enhancement layer for documents — summaries and tags (#v0.9)."""

from __future__ import annotations

import json
import logging

import anthropic

from oncofiles.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

ENHANCE_MODEL = "claude-haiku-4-5-20251001"

ENHANCE_SYSTEM_PROMPT = (
    "You are a medical document analyst. Given the extracted text of a medical document, "
    "produce a JSON object with exactly two keys:\n"
    '- "summary": A 2-3 sentence summary of the document in English. Include key findings, '
    "diagnoses, or results.\n"
    '- "tags": An array of 3-8 lowercase tags describing the document content '
    '(e.g. ["labs", "blood-count", "oncology", "NOU", "chemotherapy"]).\n\n'
    "Respond ONLY with the JSON object, no markdown fencing or extra text."
)


def enhance_document_text(text: str) -> tuple[str, str]:
    """Generate AI summary and tags from document text.

    Args:
        text: Extracted text from the document (OCR or native PDF).

    Returns:
        Tuple of (summary, tags_json).
    """
    if not text.strip():
        return "", "[]"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Truncate to ~8k chars to stay within Haiku context cheaply
    truncated = text[:8000]

    response = client.messages.create(
        model=ENHANCE_MODEL,
        max_tokens=512,
        system=ENHANCE_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Document text:\n\n{truncated}",
            }
        ],
    )

    raw = response.content[0].text if response.content else "{}"

    try:
        parsed = json.loads(raw)
        summary = parsed.get("summary", "")
        tags = parsed.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        return summary, json.dumps(tags)
    except json.JSONDecodeError:
        logger.warning("Failed to parse AI enhancement response: %s", raw[:200])
        return raw[:500], "[]"
