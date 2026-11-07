"""Text extraction ladder for curated folders (#562, OCR decision #565).

1. **Text layer** — PDF text via PyMuPDF. ``>= min_text_chars`` → done.
2. **Vision** — every page rendered to PNG and read by the *local* vision
   model (no tesseract, no cloud OCR). Handwriting and photos always land here.

Plain-text sources (``.md``, ``.txt``, ``.csv``, ``.json``) are decoded as-is.
Google-native documents get no extraction (the sidecar just links them).
Anything else is reported as unsupported so the file stays in the inbox.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from oncofiles.curated import llm

logger = logging.getLogger(__name__)

TEXT_EXTS = {"md", "txt", "csv", "json"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "heic", "tif", "tiff", "webp", "bmp", "gif"}
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
MAX_VISION_PAGES = 40
RENDER_DPI = 150

OCR_SYSTEM_PROMPT = (
    "You are an OCR engine for Slovak and Czech medical documents. Transcribe ALL text "
    "on the page exactly as written, in the original language, including headers, "
    "dates, names, stamps, values, units and reference ranges. Preserve line breaks and "
    "table rows. Mark uncertain readings with [?]. Do not summarise, translate or "
    "interpret. If the page is handwritten, start your answer with the line "
    "'[handwritten]'. Output only the transcription."
)


@dataclass
class Extraction:
    text: str
    method: str  # text_layer | vision | plain | none
    pages: int
    engine: str
    handwritten: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _vision_pages(png_pages: list[bytes]) -> tuple[str, bool]:
    texts: list[str] = []
    handwritten = False
    for i, png in enumerate(png_pages, start=1):
        answer = llm.chat(
            OCR_SYSTEM_PROMPT,
            [{"type": "text", "text": f"Page {i} of {len(png_pages)}."}, llm.image_part(png)],
            max_tokens=4096,
        )
        if answer.lower().startswith("[handwritten]"):
            handwritten = True
            answer = answer.split("\n", 1)[1] if "\n" in answer else ""
        texts.append(f"<!-- page {i} -->\n{answer.strip()}")
    return "\n\n".join(texts), handwritten


def _render_pdf(content: bytes) -> tuple[str, list[bytes], int]:
    import pymupdf

    with pymupdf.open(stream=content, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)
        n = doc.page_count
        pages = []
        if n <= MAX_VISION_PAGES:
            pages = [page.get_pixmap(dpi=RENDER_DPI).tobytes("png") for page in doc]
    return text, pages, n


def _render_image(content: bytes, ext: str) -> bytes:
    import pymupdf

    with pymupdf.open(stream=content, filetype=ext) as doc:
        return doc[0].get_pixmap(dpi=RENDER_DPI).tobytes("png")


def extract(
    name: str, content: bytes, mime_type: str = "", *, min_text_chars: int = 100
) -> Extraction:
    """Run the ladder. Never raises — failures come back as ``error``."""
    ext = _ext(name)
    if mime_type.startswith(GOOGLE_NATIVE_PREFIX):
        return Extraction("", "none", 0, "google-native", error=None)
    try:
        if ext in TEXT_EXTS:
            return Extraction(content.decode("utf-8", errors="replace"), "plain", 1, "utf-8")
        if ext == "pdf":
            text, pages, n = _render_pdf(content)
            if len(text.strip()) >= min_text_chars:
                return Extraction(text, "text_layer", n, "pymupdf")
            if not pages:
                return Extraction(
                    text,
                    "none",
                    n,
                    "pymupdf",
                    error=f"{n} pages exceed vision limit {MAX_VISION_PAGES}",
                )
            vtext, hand = _vision_pages(pages)
            return Extraction(vtext, "vision", n, llm.model_id(), handwritten=hand)
        if ext in IMAGE_EXTS:
            vtext, hand = _vision_pages([_render_image(content, ext)])
            return Extraction(vtext, "vision", 1, llm.model_id(), handwritten=hand)
    except llm.LocalLLMError as exc:
        return Extraction("", "none", 0, llm.model_id(), error=str(exc))
    except Exception as exc:  # corrupt file, unsupported image codec, …
        logger.warning("extract failed for %s", name, exc_info=True)
        return Extraction("", "none", 0, "pymupdf", error=f"{type(exc).__name__}")
    return Extraction("", "none", 0, "-", error=f"unsupported file type .{ext}")
