"""Claude Vision OCR for extracting text from document images (#36)."""

from __future__ import annotations

import base64
import time

import anthropic
from fastmcp.utilities.types import Image

from oncofiles.config import ANTHROPIC_API_KEY
from oncofiles.prompt_logger import log_ai_call

OCR_MODEL = "claude-haiku-4-5-20251001"

OCR_SYSTEM_PROMPT = (
    "You are an OCR assistant specializing in medical documents including handwritten notes. "
    "Extract ALL text from this medical document image exactly as written. "
    "For handwritten text: read carefully, preserve the original language (Slovak/Czech), "
    "mark uncertain readings with [?]. "
    "For printed text: preserve formatting, section headers, values, units, and reference ranges. "
    "Do not interpret, summarize, or translate. Output only the extracted text, nothing else."
)


def extract_text_from_image(
    image: Image,
    *,
    db=None,
    document_id: int | None = None,
) -> str:
    """Extract text from a single page image using Claude Vision.

    Uses claude-haiku-4-5-20251001 for cost efficiency (~$0.01/doc).

    Args:
        image: Image to OCR.
        db: Database instance for prompt logging (optional).
        document_id: Document ID for prompt logging (optional).
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Image.data is raw bytes, encode to base64 for the API
    image_b64 = base64.b64encode(image.data).decode("utf-8")
    media_type = image._mime_type or "image/jpeg"

    user_prompt = "Extract all text from this document image."

    start = time.perf_counter()
    response = client.messages.create(
        model=OCR_MODEL,
        max_tokens=4096,
        system=OCR_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": user_prompt,
                    },
                ],
            }
        ],
    )
    duration_ms = int((time.perf_counter() - start) * 1000)

    raw = response.content[0].text if response.content else ""

    # Log the OCR call (text prompt only, not base64 image data)
    log_ai_call(
        db,
        call_type="ocr",
        document_id=document_id,
        model=OCR_MODEL,
        system_prompt=OCR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        raw_response=raw,
        input_tokens=getattr(response.usage, "input_tokens", None),
        output_tokens=getattr(response.usage, "output_tokens", None),
        duration_ms=duration_ms,
    )

    return raw
