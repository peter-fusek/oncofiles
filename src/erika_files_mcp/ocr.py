"""Claude Vision OCR for extracting text from document images (#36)."""

from __future__ import annotations

import base64

import anthropic
from fastmcp.utilities.types import Image

from erika_files_mcp.config import ANTHROPIC_API_KEY

OCR_MODEL = "claude-haiku-4-5-20251001"

OCR_SYSTEM_PROMPT = (
    "You are an OCR assistant. Extract ALL text from this medical document image "
    "exactly as written. Preserve the original language (Slovak/Czech), formatting, "
    "section headers, values, units, and reference ranges. Do not interpret, summarize, "
    "or translate. Output only the extracted text, nothing else."
)


def extract_text_from_image(image: Image) -> str:
    """Extract text from a single page image using Claude Vision.

    Uses claude-haiku-4-5-20251001 for cost efficiency (~$0.01/doc).
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Image.data is raw bytes, encode to base64 for the API
    image_b64 = base64.b64encode(image.data).decode("utf-8")
    media_type = image._mime_type or "image/jpeg"

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
                        "text": "Extract all text from this document image.",
                    },
                ],
            }
        ],
    )

    # Extract text from response
    return response.content[0].text if response.content else ""
