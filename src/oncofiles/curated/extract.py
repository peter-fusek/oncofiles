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


MAX_PAGE_PIXELS = 25_000_000  # per rendered page; larger pages are rendered at lower dpi
MAX_FILE_BYTES = 50 * 1024 * 1024  # checked by the runner before download


def _pixmap_png(page) -> bytes:
    """Render one page at RENDER_DPI, lowered so the bitmap stays under MAX_PAGE_PIXELS."""
    import pymupdf

    w_in, h_in = page.rect.width / 72, page.rect.height / 72
    dpi = RENDER_DPI
    if w_in * h_in * dpi * dpi > MAX_PAGE_PIXELS:
        dpi = max(1, int((MAX_PAGE_PIXELS / max(w_in * h_in, 1e-6)) ** 0.5))
    zoom = dpi / 72
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")


def _pdf_text(content: bytes) -> tuple[str, int]:
    import pymupdf

    with pymupdf.open(stream=content, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc), doc.page_count


def _render_pdf_pages(content: bytes) -> list[bytes]:
    """Render pages lazily — only called once the text layer is known to be too thin."""
    import pymupdf

    with pymupdf.open(stream=content, filetype="pdf") as doc:
        if doc.page_count > MAX_VISION_PAGES:
            return []
        return [_pixmap_png(page) for page in doc]


def _render_pdf(content: bytes) -> tuple[str, list[bytes], int]:
    """Text plus rendered pages (kept for the OCR benchmark script)."""
    text, n = _pdf_text(content)
    return text, _render_pdf_pages(content), n


def _render_image(content: bytes, ext: str) -> bytes:
    import pymupdf

    with pymupdf.open(stream=content, filetype=ext) as doc:
        return _pixmap_png(doc[0])


def extract(
    name: str, content: bytes, mime_type: str = "", *, min_text_chars: int = 100
) -> Extraction:
    """Run the ladder. Never raises — failures come back as ``error``."""
    ext = _ext(name)
    if len(content) > MAX_FILE_BYTES:
        return Extraction("", "none", 0, "-", error=f"file larger than {MAX_FILE_BYTES >> 20} MB")
    if mime_type.startswith(GOOGLE_NATIVE_PREFIX):
        return Extraction("", "none", 0, "google-native", error=None)
    try:
        if ext in TEXT_EXTS:
            return Extraction(content.decode("utf-8", errors="replace"), "plain", 1, "utf-8")
        if ext == "pdf":
            text, n = _pdf_text(content)
            if len(text.strip()) >= min_text_chars:
                return Extraction(text, "text_layer", n, "pymupdf")
            pages = _render_pdf_pages(content)
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
