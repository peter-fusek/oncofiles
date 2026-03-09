"""AI enhancement layer for documents — summaries and tags (#v0.9)."""

from __future__ import annotations

import json
import logging

import anthropic

from oncofiles.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

ENHANCE_MODEL = "claude-haiku-4-5-20251001"


def _strip_markdown_fencing(text: str) -> str:
    """Strip markdown code fencing (```json ... ```) from AI responses."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = stripped.index("\n") if "\n" in stripped else len(stripped)
        stripped = stripped[first_newline + 1 :]
        # Remove closing fence
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3].rstrip()
    return stripped

ENHANCE_SYSTEM_PROMPT = (
    "You are a medical document analyst. Given the extracted text of a medical document, "
    "produce a JSON object with exactly these keys:\n"
    '- "summary": A 2-3 sentence summary of the document in English. Include key findings, '
    "diagnoses, or results.\n"
    '- "summary_sk": The same summary translated to Slovak (slovenčina).\n'
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
        parsed = json.loads(_strip_markdown_fencing(raw))
        summary = parsed.get("summary", "")
        tags = parsed.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        return summary, json.dumps(tags)
    except json.JSONDecodeError:
        logger.warning("Failed to parse AI enhancement response: %s", raw[:200])
        return raw[:500], "[]"


METADATA_SYSTEM_PROMPT = (
    "You are a medical document analyst. Given the extracted text of a medical document, "
    "produce a JSON object with exactly these keys:\n"
    '- "document_type": detected type (one of: lab_report, discharge_summary, pathology, '
    "imaging, surgical_report, prescription, referral, "
    "genetics, chemo_sheet, consultation, other)\n"
    '- "findings": array of key findings as short strings (max 10)\n'
    '- "diagnoses": array of objects with "name" and optional "icd_code" keys\n'
    '- "medications": array of medication names mentioned\n'
    '- "dates_mentioned": array of dates found in document (YYYY-MM-DD format when possible)\n'
    '- "providers": array of healthcare provider/institution names\n'
    '- "plain_summary": 3-sentence patient-friendly summary in English\n'
    '- "plain_summary_sk": The same patient-friendly summary in Slovak (slovenčina)\n\n'
    "Respond ONLY with the JSON object, no markdown fencing or extra text."
)


def extract_structured_metadata(text: str) -> dict:
    """Extract structured medical metadata from document text.

    Args:
        text: Extracted text from the document.

    Returns:
        Dict with structured metadata fields.
    """
    if not text.strip():
        return {
            "document_type": "other",
            "findings": [],
            "diagnoses": [],
            "medications": [],
            "dates_mentioned": [],
            "providers": [],
            "plain_summary": "",
            "plain_summary_sk": "",
        }

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    truncated = text[:8000]

    response = client.messages.create(
        model=ENHANCE_MODEL,
        max_tokens=1024,
        system=METADATA_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Document text:\n\n{truncated}",
            }
        ],
    )

    raw = response.content[0].text if response.content else "{}"

    try:
        parsed = json.loads(_strip_markdown_fencing(raw))
        # Validate expected keys with defaults
        return {
            "document_type": parsed.get("document_type", "other"),
            "findings": parsed.get("findings", []),
            "diagnoses": parsed.get("diagnoses", []),
            "medications": parsed.get("medications", []),
            "dates_mentioned": parsed.get("dates_mentioned", []),
            "providers": parsed.get("providers", []),
            "plain_summary": parsed.get("plain_summary", ""),
            "plain_summary_sk": parsed.get("plain_summary_sk", ""),
        }
    except json.JSONDecodeError:
        logger.warning("Failed to parse metadata response: %s", raw[:200])
        return {
            "document_type": "other",
            "findings": [],
            "diagnoses": [],
            "medications": [],
            "dates_mentioned": [],
            "providers": [],
            "plain_summary": "",
            "plain_summary_sk": "",
        }
