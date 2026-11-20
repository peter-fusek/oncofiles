"""Text extraction ladder for curated folders (#562, OCR decision #565).

1. **Text layer** — PDF text via PyMuPDF, only when every page is
   born-digital (``born_digital``: no invisible text, no page-sized raster
   image; the same rule as re-OCR, #931) and it holds ``>= min_text_chars``
   → done. A scanner's own OCR layer over a scan is OCR of unknown quality,
   so such a PDF is read like any scan.
2. **Vision** — every page rendered to PNG and read by the *local* vision
   model (no tesseract, no cloud OCR). Handwriting and photos always land here.
   Each page's orientation is decided first and the image rotated locally
   (``orientation``, #609; an explicit EXIF / PDF rotation or a portrait page
   needs no model call, #882); charts are fenced as ``[chart: …]`` and never read
   as data (``ocr_markers``). A chart, a rotated region or an unsure
   orientation sends the document to review (``review_notes``); a confident
   rotation alone does not. A page answering near-empty although nearly all
   its completion was reasoning is asked once more with thinking off
   (``retry_near_empty``, #806 shared with re-OCR, #931).

Plain-text sources (``.md``, ``.txt``, ``.csv``, ``.json``) are decoded as-is.
Google Docs and Slides are exported by Drive as plain text, Sheets as CSV
(``NATIVE_EXPORT``); other Google-native types (Forms, Drawings…) get no
extraction and stay in the inbox.
Anything else is reported as unsupported so the file stays in the inbox.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from oncofiles.curated import llm, ocr_markers, orientation

logger = logging.getLogger(__name__)

TEXT_EXTS = {"md", "txt", "csv", "json"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "heic", "tif", "tiff", "webp", "bmp", "gif"}
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
# Google-native type → the format Drive exports it to (a read, never a write).
NATIVE_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
MAX_VISION_PAGES = 40
RENDER_DPI = 150

# Bump on ANY change to OCR_SYSTEM_PROMPT or to how a page is prepared for the
# model (orientation, #609). Written to the sidecar as ``ocr_version``; a
# sidecar without it was read before #609 (``ocr-v1``). ocr-v3: the #882
# orientation pre-filter (EXIF / PDF rotation, portrait = upright). ocr-v4
# (#931): a PDF whose text layer is not born-digital is vision-read, and a
# near-empty reasoning-heavy page answer is retried once with thinking off.
OCR_VERSION = "ocr-v4"

# #806, shared by ingest and re-OCR (#931); thresholds may only become stricter
NEAR_EMPTY_CHARS = 50  # a page answer with fewer stripped characters is near-empty
RETRY_REASONING_RATIO = 0.8  # near-empty answer with reasoning/completion above this → 1 retry
# the retry's per-call mode: thinking off through the reasoning marker, nothing global
RETRY_MODE = {"thinking": False, "reasoning_marker": True}
MIN_OWN_TEXT_CHARS = 100  # re-OCR's floor for a born-digital text layer

OCR_SYSTEM_PROMPT = (
    "You are an OCR engine for Slovak and Czech medical documents. Transcribe ALL text "
    "on the page exactly as written, in the original language, including headers, "
    "dates, names, stamps, values, units and reference ranges. Preserve line breaks and "
    "table rows. Mark uncertain readings with [?]. Do not summarise, translate or "
    "interpret. If the page is handwritten, start your answer with the line "
    "'[handwritten]'. Output only the transcription.\n\n"
    "Digits: never guess. A wrong digit is worse than a gap: when any digit of a value "
    "is not clearly legible, write the value followed by [?].\n"
    "Charts and graphs (trend plots, ECG or monitor strips, growth curves, bar charts): "
    "put everything that belongs to the chart into ONE block [chart: ...] with its title, "
    "axis labels and axis tick labels exactly as printed. Never read a value off a plotted "
    "line, bar or point, and never write such a value anywhere. Tables, results and "
    "captions printed outside the chart stay normal text.\n"
    "Text turned sideways or upside down relative to the rest of the page: do not read "
    "it; write the line [rotated region] in its place.\n"
    "Examples:\n"
    "A trend plot of a tumour marker with tick marks 0, 5 and 10 and months under it:\n"
    "[chart: Vývoj CEA; y: ug/l 0 5 10; x: 03/2027 06/2027 09/2027]\n"
    "An ECG strip with the time printed above it:\n"
    "Záznam 14.03.2027 10:12\n"
    "[chart: EKG; y: mV; waveform trace]\n"
    "A results table next to a chart (the table is normal text):\n"
    "Leukocyty 6,37 G/l 4,0 - 11\n"
    "[chart: Leukocyty v čase; y: G/l 0 5 10 15]\n"
    "A smudged digit:\n"
    "Hemoglobín 132 [?] g/l 120 - 160\n"
    "A stamp printed upside down in a corner:\n"
    "[rotated region]"
)


@dataclass
class Extraction:
    text: str
    method: str  # text_layer | vision | plain | none
    pages: int
    engine: str
    handwritten: bool = False
    error: str | None = None
    # #609, vision only (empty / False otherwise): per page, the clockwise
    # rotation applied before reading and how it was decided
    # (``orientation.SOURCE_*``); 1-based pages with a chart block or a
    # rotated region; the OCR prompt version.
    rotation: list[int] = field(default_factory=list)
    rotation_source: list[str] = field(default_factory=list)
    chart_pages: list[int] = field(default_factory=list)
    rotated_region_pages: list[int] = field(default_factory=list)
    ocr_version: str | None = None
    # #882: ``orientation.tally`` of this extraction (model direction questions
    # vs pages decided without one); counts only, never written to a sidecar.
    orientation: dict = field(default_factory=dict)
    # Set when ``error`` came from the local model: ``llm.LocalLLMError.failure``
    # (transport | answer | unknown). None = not a model error (corrupt file, …).
    llm_failure: str | None = None
    # #931 (#806 at ingest): 1-based pages asked once more with thinking off;
    # counts only, never written to a sidecar.
    retried_pages: list[int] = field(default_factory=list)
    # #931: the PDF had a text layer of enough characters that was not
    # born-digital (a scanner's OCR layer), so it was vision-read instead.
    text_layer_ignored: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    def review_notes(self) -> list[str]:
        """Why a vision reading must go to review (#609): a chart, a rotated
        region, or an unsure orientation. A page the model confidently reported
        as turned and that was read upright after local rotation is NOT a
        review reason (maintainer: review only real ambiguity); it is recorded
        in ``rotation`` / ``rotation_source`` only."""
        notes = []
        if self.chart_pages:
            notes.append(f"chart on page {_pages(self.chart_pages)}: values never read as data")
        if self.rotated_region_pages:
            notes.append(f"rotated region on page {_pages(self.rotated_region_pages)}: not read")
        unsure = [i for i, s in enumerate(self.rotation_source, start=1)
                  if s in orientation.UNSURE_SOURCES]  # fmt: skip
        if unsure:
            notes.append(f"orientation unsure on page {_pages(unsure)}: read both ways")
        return notes

    def front_matter(self) -> dict:
        """Optional sidecar keys of a vision reading (readers tolerate their absence)."""
        if self.method != "vision":
            return {}
        return {
            "ocr_version": self.ocr_version,
            "rotation": list(self.rotation),
            "rotation_source": list(self.rotation_source),
            "has_chart": bool(self.chart_pages),
            "rotated_region": bool(self.rotated_region_pages),
        }


def _pages(numbers: list[int]) -> str:
    return ", ".join(str(n) for n in numbers)


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def needs_retry(text: str | None, reasoning_tokens, completion_tokens) -> bool:
    """A near-empty answer from a call that spent nearly all its tokens on
    reasoning (#806). Unknown counts never trigger a retry."""
    if len((text or "").strip()) >= NEAR_EMPTY_CHARS:
        return False
    if not all(isinstance(v, int) and not isinstance(v, bool)
               for v in (reasoning_tokens, completion_tokens)):  # fmt: skip
        return False
    return completion_tokens > 0 and reasoning_tokens / completion_tokens > RETRY_REASONING_RATIO


def retry_near_empty(
    text: str | None, tokens: tuple, again: Callable[[], str | None], *, where: str = ""
) -> tuple[str | None, bool]:
    """The #806 retry, shared by ingest and re-OCR (#931).

    When ``needs_retry`` holds (``tokens`` = (reasoning_tokens,
    completion_tokens) of the first call), ``again()`` makes ONE more call for
    the same image and prompt with ``RETRY_MODE``; its text stands only when
    longer. A failed retry keeps the first answer (it is a bonus). Returns
    (answer, retried); ``retried`` is True whenever the retry was attempted.
    Only answer texts are compared: the reasoning channel is never a
    transcript. ``where`` (page / pass numbers, no content) goes to the log."""
    if not needs_retry(text, *tokens):
        return text, False
    try:
        retry_text = again()
    except Exception as exc:
        logger.warning("OCR retry failed: %s %s", where, type(exc).__name__)
        return text, True
    if len((retry_text or "").strip()) > len((text or "").strip()):
        return retry_text, True
    return text, True


@dataclass
class VisionRead:
    text: str
    handwritten: bool
    rotation: list[int]
    rotation_source: list[str]
    chart_pages: list[int]
    rotated_region_pages: list[int]
    orientation_calls: int = 0
    retried_pages: list[int] = field(default_factory=list)


def _vision_pages(png_pages: list[bytes], hints: list | None = None) -> VisionRead:
    """Each page: orientation decided first (``orientation.decide``: an
    explicit EXIF / PDF rotation or a portrait page needs no model call,
    #882; otherwise the model only reports the direction and the image is
    rotated locally), then the upright image is transcribed. A doubtful first
    reading of a page decided without the model gets one direction question
    (``orientation.settle``) and is read again when the model reports a turn.
    Every transcription call gets the #806 near-empty retry
    (``retry_near_empty``, #931); the retried pages are reported.
    ``hints``: per page an ``orientation.Hint`` or None."""
    calls = 0
    retried: set[int] = set()

    def ask(image: bytes, *, client=None):
        nonlocal calls
        calls += 1
        return orientation.ask_rotation(image, client=client)

    texts: list[str] = []
    handwritten = False
    rotation: list[int] = []
    sources: list[str] = []
    charts: list[int] = []
    regions: list[int] = []
    for i, png in enumerate(png_pages, start=1):

        def read(image: bytes, i=i) -> str:
            content = [{"type": "text", "text": f"Page {i} of {len(png_pages)}."},
                       llm.image_part(image)]  # fmt: skip

            def once(**mode) -> tuple[str, tuple]:
                info: dict = {}
                try:
                    text = llm.chat(OCR_SYSTEM_PROMPT, content, info=info, **mode)
                    return text, (info.get("reasoning_tokens"), info.get("completion_tokens"))
                finally:
                    info.clear()  # the raw answer channels never outlive the call

            text, tokens = once()
            answer, again = retry_near_empty(
                text, tokens, lambda: once(**RETRY_MODE)[0], where=f"page={i}"
            )
            if again:
                retried.add(i)
            return answer or ""

        hint = hints[i - 1] if hints and i - 1 < len(hints) else None
        page = orientation.decide(png, read, hint=hint, ask=ask)
        answer = page.text if page.text is not None else read(page.png)
        if (again := orientation.settle(page, answer, read, ask=ask)) is not None:
            page = again
            answer = again.text if again.text is not None else read(again.png)
        rotation.append(page.rotation)
        sources.append(page.source)
        if answer.lower().startswith("[handwritten]"):
            handwritten = True
            answer = answer.split("\n", 1)[1] if "\n" in answer else ""
        if ocr_markers.has_chart(answer):
            charts.append(i)
        if ocr_markers.has_rotated_region(answer):
            regions.append(i)
        texts.append(f"<!-- page {i} -->\n{answer.strip()}")
    return VisionRead("\n\n".join(texts), handwritten, rotation, sources, charts, regions,
                      orientation_calls=calls, retried_pages=sorted(retried))  # fmt: skip


def _vision_extraction(
    png_pages: list[bytes], pages: int, hints: list | None = None, *, layer_ignored: bool = False
) -> Extraction:
    v = _vision_pages(png_pages, hints)
    return Extraction(
        v.text, "vision", pages, llm.model_id(), handwritten=v.handwritten,
        rotation=v.rotation, rotation_source=v.rotation_source, chart_pages=v.chart_pages,
        rotated_region_pages=v.rotated_region_pages, ocr_version=OCR_VERSION,
        orientation=orientation.tally(v.rotation_source, v.orientation_calls),
        retried_pages=v.retried_pages, text_layer_ignored=layer_ignored,
    )  # fmt: skip


MAX_PAGE_PIXELS = 25_000_000  # per rendered page; larger pages are rendered at lower dpi
MAX_FILE_BYTES = 50 * 1024 * 1024  # checked by the runner before download


def _pixmap_png(page, dpi: int | None = None) -> bytes:
    """Render one page at up to RENDER_DPI, never above MAX_PAGE_PIXELS.

    The zoom is fractional (an absurd MediaBox gets a tiny zoom, not 1 dpi), and
    the final bitmap size is checked before rendering — over the cap is refused.
    """
    import pymupdf

    area_pt = max(page.rect.width * page.rect.height, 1e-6)
    zoom = min((dpi or RENDER_DPI) / 72, (MAX_PAGE_PIXELS / area_pt) ** 0.5)
    w, h = int(page.rect.width * zoom) + 1, int(page.rect.height * zoom) + 1
    if w * h > MAX_PAGE_PIXELS * 1.01 or w < 8 or h < 8:
        raise ValueError(f"page {page.rect.width:.0f}x{page.rect.height:.0f} pt cannot be rendered")
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")


def born_digital(page, *, image_cover: float = 0.8) -> bool:
    """Is this PDF page's text its own (born-digital), not OCR laid over a scan?
    False when any text is invisible (render mode 3 or opacity 0, how OCR layers are added)
    or a raster image covers most of the page. Shared by ingest and re-OCR (#931)."""
    try:
        if any(span.get("type") == 3 or span.get("opacity", 1) == 0
               for span in page.get_texttrace()):  # fmt: skip
            return False
        area = abs(page.rect.width * page.rect.height) or 1.0
        for info in page.get_image_info():
            x0, y0, x1, y1 = info.get("bbox") or (0, 0, 0, 0)
            if abs((x1 - x0) * (y1 - y0)) / area >= image_cover:
                return False
    except Exception:  # unreadable structure: do not trust the layer
        return False
    return True


@dataclass
class PdfTextLayer:
    pages: list[str]  # per page, as PyMuPDF returns it
    born_digital: bool  # every page passes ``born_digital``

    @property
    def text(self) -> str:
        return "\n".join(self.pages)

    @property
    def chars(self) -> int:
        return len("".join(self.pages).strip())

    def is_own_text(self, min_chars: int = MIN_OWN_TEXT_CHARS) -> bool:
        """The one decision (#931, ingest and re-OCR): a text layer is the
        document's own text only when it is born-digital and holds at least
        ``min_chars`` characters. An OCR'd layer (invisible text over a scan)
        is OCR of unknown quality, maybe the very text an old transcription
        copied: the image is read instead."""
        return self.born_digital and self.chars >= min_chars


def pdf_text_layer(content: bytes) -> PdfTextLayer:
    import pymupdf

    with pymupdf.open(stream=content, filetype="pdf") as doc:
        layer = [page.get_text() for page in doc]
        digital = all(born_digital(page) for page in doc)
    return PdfTextLayer(layer, digital)


def _pdf_text(content: bytes) -> tuple[str, int]:
    layer = pdf_text_layer(content)
    return layer.text, len(layer.pages)


def _render_pdf_pages(content: bytes) -> list[bytes]:
    """Render pages lazily — only called once the text layer is known to be too thin."""
    import pymupdf

    with pymupdf.open(stream=content, filetype="pdf") as doc:
        if doc.page_count > MAX_VISION_PAGES:
            return []
        return [_pixmap_png(page) for page in doc]


def _pdf_hints(content: bytes) -> list:
    """Per page the PDF's own ``/Rotate`` (``orientation.pdf_hint``, #882)."""
    import pymupdf

    with pymupdf.open(stream=content, filetype="pdf") as doc:
        return [orientation.pdf_hint(page) for page in doc]


def _image_hint(content: bytes, ext: str, png: bytes):
    """A JPEG's EXIF rotation (``orientation.image_hint``, #882)."""
    return orientation.image_hint(content, png) if ext in ("jpg", "jpeg") else None


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
    if mime_type in NATIVE_EXPORT:  # content is Drive's text export
        text = content.decode("utf-8-sig", errors="replace")
        return Extraction(text, "plain", 1, "google-export")
    if mime_type.startswith(GOOGLE_NATIVE_PREFIX):
        return Extraction("", "none", 0, "google-native", error=None)
    try:
        if ext in TEXT_EXTS:
            return Extraction(content.decode("utf-8", errors="replace"), "plain", 1, "utf-8")
        if ext == "pdf":
            layer = pdf_text_layer(content)
            text, n = layer.text, len(layer.pages)
            if layer.is_own_text(min_text_chars):
                return Extraction(text, "text_layer", n, "pymupdf")
            # #931: a scanner's OCR layer is never taken as the text; the scan
            # is vision-read and the engine recorded is the local model.
            ignored = not layer.born_digital and layer.chars >= min_text_chars
            pages = _render_pdf_pages(content)
            if not pages:
                return Extraction(
                    text,
                    "none",
                    n,
                    "pymupdf",
                    error=f"{n} pages exceed vision limit {MAX_VISION_PAGES}",
                )
            return _vision_extraction(pages, n, _pdf_hints(content), layer_ignored=ignored)
        if ext in IMAGE_EXTS:
            png = _render_image(content, ext)
            return _vision_extraction([png], 1, [_image_hint(content, ext, png)])
    except llm.LocalLLMError as exc:
        return Extraction("", "none", 0, llm.model_id(), error=str(exc), llm_failure=exc.failure)
    except Exception as exc:  # corrupt file, unsupported image codec, …
        logger.warning("extract failed for %s", name, exc_info=True)
        return Extraction("", "none", 0, "pymupdf", error=f"{type(exc).__name__}")
    return Extraction("", "none", 0, "-", error=f"unsupported file type .{ext}")
