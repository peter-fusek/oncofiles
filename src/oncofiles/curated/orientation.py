"""Page orientation before vision OCR (#609), shared by ingest and re-OCR.

The local vision model invents digits when it reads text that is upside down
or sideways. Infra rule (architect review on #609): the model only *reports*
how the page is turned; the page image is rotated locally (PyMuPDF, no new
dependency, no cloud, no Tesseract in the image) and only the upright image
is transcribed. Never ask the model to read upside-down text.

Per page:

1. ``ask_rotation`` — one small call on a downscaled copy of the page
   (``ORIENT_MAX_SIDE``) asking ONLY for the text direction, answer
   ``{"orientation": upright | upside_down | turned_clockwise |
   turned_counterclockwise, "confident": true | false}``, parsed strictly
   (anything else = no answer). Thinking off, small budget.
2. A confident answer → the page is rotated by that angle and read once
   (``rotation_source`` ``model``).
3. Unsure, or no usable answer (unparseable, truncated, a per-call error) →
   the page is read at the guess (0° without one) AND at the guess + 180°;
   the reading with the higher ``score_reading`` is kept (``model+score``,
   or ``score`` without a guess). Ties keep the guess.
4. A blank back side in a re-OCR (``reocr.is_blank``) is not asked about
   (``blank``).
5. ``LOCAL_LLM_OCR_ORIENTATION=off`` (runtime env; the ``LOCAL_LLM_`` prefix
   also reaches the re-OCR child) skips all of it: rotation 0, ``off``.

Pre-filter (#882, ``LOCAL_LLM_OCR_ORIENTATION=prefilter``, the default; a
spot check of 23 pages found no turned page while the question cost about
16 % per page). ``decide`` runs before steps 1–3, with no model call:

a. An explicit rotation is honoured: a JPEG's EXIF ``Orientation`` (read by
   ``jpeg_info``, stdlib only; mirrored values are ignored) or a PDF page's
   ``/Rotate``. MuPDF already applies both when it renders the page (a test
   locks that); an EXIF 90/270 that the rendered size shows as NOT applied is
   turned locally. ``rotation`` = that angle, ``rotation_source`` ``exif`` or
   ``pdf``.
b. Otherwise a portrait page (height ≥ ``PORTRAIT_RATIO`` × width) is taken
   as upright: rotation 0, ``prefilter``.
c. A landscape or near-square page goes through steps 1–3 unchanged.

Safety net (``settle``): when the first reading of a page decided by a or b
is ``doubtful`` (``score_reading`` below ``DOUBTFUL_MIN_SCORE``, or at least
``DOUBTFUL_MIN_UNCERTAIN`` ``[?]`` markers that are more than
``DOUBTFUL_UNCERTAIN_SHARE`` of the word-like tokens), the model is asked once
for that page. Confident → ``prefilter+model`` (read again only when it
reports a turn); unsure → step 3's two-way reading (0° reuses the first
reading) with its unsure source, so the page goes to review as before.
``always`` = the #609 behaviour (every page asked, no pre-filter, no safety
net). ``tally`` gives the per-document counts (model calls vs pages decided
without the model) for the re-OCR report and the ingest run report.

``rotation`` is the clockwise angle (0/90/180/270) applied to the page image
before it was read; it is recorded per page in the sidecar front matter
(``rotation``, ``rotation_source``; optional, older sidecars lack them).

``score_reading`` (documented on purpose, it decides between two readings):
``2 × known words + 1 × other word-like tokens − 3 × [?] markers − 3 ×
[rotated region] markers``. A known word is one of ``COMMON_WORDS`` (frequent
Slovak / Czech / English words of medical documents); a word-like token has
at least three letters and a vowel. An upright reading yields real words; an
upside-down one yields fragments and ``[?]``.
"""

from __future__ import annotations

import json
import logging
import re
import struct
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from oncofiles import config
from oncofiles.curated import llm
from oncofiles.curated.ocr_markers import ROTATED_REGION

logger = logging.getLogger(__name__)

ROTATIONS = (0, 90, 180, 270)
ORIENT_MAX_SIDE = 1000  # px, longest side of the image sent for the direction question
ORIENT_BUDGET = 2_000  # tokens; the answer is one short JSON object
SOURCE_MODEL = "model"
SOURCE_MODEL_SCORE = "model+score"
SOURCE_SCORE = "score"
SOURCE_OFF = "off"
SOURCE_BLANK = "blank"  # re-OCR only: a measured blank back side is not asked about
SOURCE_EXIF = "exif"  # #882: the JPEG's EXIF Orientation, no model call
SOURCE_PDF = "pdf"  # #882: the PDF page's /Rotate, no model call
SOURCE_PREFILTER = "prefilter"  # #882: portrait page without a flag → upright
SOURCE_PREFILTER_MODEL = "prefilter+model"  # #882: safety net, the model was sure
UNSURE_SOURCES = frozenset({SOURCE_MODEL_SCORE, SOURCE_SCORE})
# Decided without the model; the safety net (``settle``) may still ask once.
PREFILTER_SOURCES = frozenset({SOURCE_EXIF, SOURCE_PDF, SOURCE_PREFILTER})

MODE_OFF = "off"
MODE_PREFILTER = "prefilter"
MODE_ALWAYS = "always"
_MODE_ALIASES = {
    "off": MODE_OFF, "false": MODE_OFF, "0": MODE_OFF, "no": MODE_OFF,
    "prefilter": MODE_PREFILTER, "always": MODE_ALWAYS,
    # the #609 switch was on/off; "on" meant "enabled", now the default mode
    "on": MODE_PREFILTER, "true": MODE_PREFILTER, "1": MODE_PREFILTER, "yes": MODE_PREFILTER,
}  # fmt: skip
PORTRAIT_RATIO = 1.1  # height ≥ 1.1 × width → portrait
# Safety net thresholds (``doubtful``). An upright page of a medical document
# yields many real words; a reading of a turned page yields fragments and
# ``[?]``. A score below 6 is fewer than three known words or six word-like
# tokens on the whole page; three or more ``[?]`` that are more than a quarter
# of the word-like tokens is a page the model could not read. Either one asks
# the model once (a near-empty or picture-only page costs one call, as before).
DOUBTFUL_MIN_SCORE = 6
DOUBTFUL_MIN_UNCERTAIN = 3
DOUBTFUL_UNCERTAIN_SHARE = 0.25

# EXIF Orientation → clockwise rotation that shows the image upright.
# Mirrored values (2, 4, 5, 7) are not rotations and are ignored.
EXIF_ROTATION = {1: 0, 3: 180, 6: 90, 8: 270}

# Text direction → clockwise rotation that makes the page upright.
_ANGLE = {
    "upright": 0,
    "turned_counterclockwise": 90,  # lines read bottom-to-top
    "upside_down": 180,
    "turned_clockwise": 270,  # lines read top-to-bottom
}

ORIENTATION_PROMPT = (
    "You check how a scanned page is turned. Do NOT transcribe anything. Look only at the "
    "direction of the printed text lines and answer with ONE JSON object and nothing else:\n"
    '{"orientation": "<upright|upside_down|turned_clockwise|turned_counterclockwise>", '
    '"confident": <true|false>}\n'
    "upright = lines read left to right, top of the letters up. upside_down = the page is "
    "turned 180 degrees. turned_clockwise = lines run from top to bottom (the page was "
    "turned a quarter turn clockwise). turned_counterclockwise = lines run from bottom to "
    "top. If most of the text has one direction, answer that direction. confident=false "
    "when the page has little text, only a picture, or you are not sure.\n"
    "Examples:\n"
    "A normal lab report: "
    '{"orientation": "upright", "confident": true}\n'
    "A report scanned upside down (the header is at the bottom, letters are inverted): "
    '{"orientation": "upside_down", "confident": true}\n'
    "A photo where the lines run from the bottom edge up to the top edge: "
    '{"orientation": "turned_counterclockwise", "confident": true}\n'
    "An almost empty page with a stamp only: "
    '{"orientation": "upright", "confident": false}'
)

COMMON_WORDS = frozenset({
    "pacient", "pacientka", "meno", "priezvisko", "dátum", "datum", "narodenia", "narozen",
    "rodné", "číslo", "adresa", "poisťovňa", "pojišťovna", "lekár", "lékař", "lekárka",
    "lekárske", "lékařská", "správa", "zpráva", "diagnóza", "diagnoza", "vyšetrenie",
    "vyšetření", "výsledok", "výsledek", "výsledky", "hodnota", "hodnoty", "jednotka", "norma",
    "referenčné", "referenční", "rozmedzie", "rozmezí", "odber", "odběr", "materiál", "krv",
    "krev", "sérum", "serum", "moč", "plazma", "laboratórium", "laboratoř", "oddelenie",
    "oddělení", "klinika", "nemocnica", "nemocnice", "ambulancia", "ambulance", "ústav",
    "onkológia", "onkologie", "chirurgia", "chirurgie", "interná", "interní", "liečba", "léčba",
    "terapia", "terapie", "odporúčanie", "doporučení", "kontrola", "nález", "anamnéza",
    "epikríza", "prepúšťacia", "leukocyty", "erytrocyty", "hemoglobín", "hemoglobin",
    "hematokrit", "trombocyty", "neutrofily", "lymfocyty", "monocyty", "glukóza", "glukoza",
    "kreatinín", "kreatinin", "urea", "albumín", "albumin", "bilirubín", "bilirubin", "sodík",
    "draslík", "chloridy", "vápnik", "vápník", "the", "and", "for", "with", "date", "patient",
    "result", "results", "report", "name", "unit", "reference", "range", "page", "strana",
    "podpis", "pečiatka", "razítko", "telefón", "telefon", "email"
})  # fmt: skip
_WORD = re.compile(r"[^\W\d_]{3,}")
_VOWEL = re.compile(r"[aeiouyáäéíóôúýěůö]", re.I)


@dataclass
class Guess:
    rotation: int | None  # None: no usable answer
    confident: bool


@dataclass
class Oriented:
    rotation: int  # clockwise degrees applied before the page was read
    # model | model+score | score | off | exif | pdf | prefilter | prefilter+model
    # (blank: re-OCR only)
    source: str
    png: bytes  # the page image as it was (or will be) read
    text: str | None = None  # the kept reading when both orientations were read
    # Degrees WE turned the rendered image (``png`` already includes them).
    # None = ``rotation``; differs only when the renderer applied an explicit
    # EXIF / PDF rotation itself (#882), so a re-render at another dpi is
    # turned by ``local_rotation`` only.
    local: int | None = None

    @property
    def unsure(self) -> bool:
        return self.source in UNSURE_SOURCES

    @property
    def local_rotation(self) -> int:
        return self.rotation if self.local is None else self.local


@dataclass(frozen=True)
class Hint:
    """An explicit rotation found in the file (#882)."""

    rotation: int  # clockwise degrees the file asks for
    source: str  # exif | pdf
    local: int = 0  # degrees still to turn locally (0: the renderer applied it)


_warned_modes: set[str] = set()


def mode() -> str:
    """``off`` | ``prefilter`` | ``always`` (#882), read at call time. An
    unknown value is ``prefilter`` with one warning per value."""
    raw = str(getattr(config, "OCR_ORIENTATION", MODE_PREFILTER)).strip().lower()
    known = _MODE_ALIASES.get(raw)
    if known is None:
        if raw not in _warned_modes:
            _warned_modes.add(raw)
            logger.warning("LOCAL_LLM_OCR_ORIENTATION=%r is not off|prefilter|always; "
                           "using prefilter", raw[:40])  # fmt: skip
        return MODE_PREFILTER
    return known


def enabled() -> bool:
    return mode() != MODE_OFF


def png_size(png: bytes) -> tuple[int, int] | None:
    """(width, height) from the PNG header; None when it is not a PNG."""
    if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    return None


def is_portrait(png: bytes) -> bool:
    size = png_size(png)
    return size is not None and size[1] >= size[0] * PORTRAIT_RATIO


def jpeg_info(content: bytes) -> tuple[int | None, tuple[int, int] | None]:
    """(EXIF Orientation value, (width, height) of the stored raster) of a
    JPEG; None for what is absent. Stdlib only, bounded, never raises."""
    value: int | None = None
    size: tuple[int, int] | None = None
    try:
        if content[:2] != b"\xff\xd8":
            return None, None
        pos, n = 2, len(content)
        for _ in range(512):  # bound on the number of segments
            if pos + 4 > n or content[pos] != 0xFF:
                break
            marker = content[pos + 1]
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # markers without a length
                pos += 2
                continue
            if marker == 0xDA:  # start of scan: no headers after it
                break
            length = struct.unpack(">H", content[pos + 2 : pos + 4])[0]
            seg = content[pos + 4 : pos + 2 + length]
            if marker == 0xE1 and value is None and seg[:6] == b"Exif\x00\x00":
                value = _exif_orientation(seg[6:])
            elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC) and len(seg) >= 5:
                h, w = struct.unpack(">HH", seg[1:5])
                size = (w, h)
            pos += 2 + max(length, 2)
    except Exception:  # a malformed header: whatever was found so far
        return value, size
    return value, size


def _exif_orientation(tiff: bytes) -> int | None:
    end = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if end is None:
        return None
    ifd = struct.unpack(end + "I", tiff[4:8])[0]
    count = struct.unpack(end + "H", tiff[ifd : ifd + 2])[0]
    for k in range(min(count, 256)):
        at = ifd + 2 + 12 * k
        tag, typ = struct.unpack(end + "HH", tiff[at : at + 4])
        if tag == 0x0112 and typ == 3:  # Orientation, SHORT
            return struct.unpack(end + "H", tiff[at + 8 : at + 10])[0]
    return None


def image_hint(content: bytes, rendered_png: bytes) -> Hint | None:
    """The EXIF rotation of a JPEG, if any (#882). ``rendered_png`` is the
    page as MuPDF rendered it: when an EXIF 90/270 did not change which side
    of the stored raster is longer, MuPDF did not apply it and we turn it."""
    value, raw = jpeg_info(content)
    angle = EXIF_ROTATION.get(value) if value is not None else None
    if not angle:
        return None
    local = 0
    shown = png_size(rendered_png)
    turned = angle in (90, 270) and raw and shown and raw[0] != raw[1]
    if turned and (raw[0] > raw[1]) == (shown[0] > shown[1]):
        local = angle
    return Hint(angle, SOURCE_EXIF, local)


def pdf_hint(page) -> Hint | None:
    """A PDF page's ``/Rotate`` (#882); MuPDF applies it when rendering."""
    try:
        angle = int(page.rotation) % 360
    except Exception:
        return None
    return Hint(angle, SOURCE_PDF) if angle in (90, 180, 270) else None


def _render(png: bytes, degrees: int = 0, max_side: int | None = None) -> bytes:
    """``png`` rotated clockwise by ``degrees`` and/or scaled so its longest side
    is at most ``max_side`` px. Exact for multiples of 90° (no resampling)."""
    import pymupdf

    with pymupdf.open(stream=png, filetype="png") as doc:
        page = doc[0]
        pix_w = pymupdf.Pixmap(png).width
        zoom = pix_w / max(page.rect.width, 1e-6)  # keep the pixel size
        if max_side:
            longest = max(page.rect.width, page.rect.height) * zoom
            zoom *= min(1.0, max_side / max(longest, 1.0))
        matrix = pymupdf.Matrix(zoom, zoom).prerotate(degrees % 360)
        return page.get_pixmap(matrix=matrix).tobytes("png")


def rotate_png(png: bytes, degrees: int) -> bytes:
    """The page image turned clockwise by ``degrees`` (0 returns it unchanged)."""
    return png if degrees % 360 == 0 else _render(png, degrees)


def parse_orientation(answer: str | None) -> Guess:
    """Strict: one JSON object with exactly the two keys and allowed values."""
    try:
        obj = json.loads(llm.strip_think(answer or "").strip().strip("`").removeprefix("json"))
    except ValueError:
        return Guess(None, False)
    if not isinstance(obj, dict) or set(obj) != {"orientation", "confident"}:
        return Guess(None, False)
    angle = _ANGLE.get(obj.get("orientation"))
    if angle is None or not isinstance(obj.get("confident"), bool):
        return Guess(None, False)
    return Guess(angle, obj["confident"])


def ask_rotation(
    png: bytes, *, client=None, record: dict | None = None, model: str | None = None,
    reasoning_marker: bool | None = None,
) -> Guess:  # fmt: skip
    """The model's report of how the page is turned (never a transcription).
    A per-call failure is "no answer", which makes the caller read both ways;
    an unreachable model fails again on the transcription call itself.

    ``record`` (optional dict, #882) receives the call's usage, counts only:
    ``seconds``, ``prompt_tokens``, ``completion_tokens``, ``reasoning_tokens``,
    ``finish``, ``valid`` (a parseable answer), ``confident``, the sent image's
    ``width`` / ``height``, and ``error`` (exception class) on a failure.
    Never the answer text.
    ``model`` (``None`` = ``LOCAL_LLM_MODEL``): another loaded LM Studio model
    for this call only; only the dry re-OCR compare passes it, and only then
    is it sent, so every production request is unchanged.
    ``reasoning_marker`` (``None`` = env, #1044): the dry re-OCR compare's
    per-job marker override, passed to ``llm.chat`` only when given."""
    try:
        small = _render(png, 0, ORIENT_MAX_SIDE)
    except Exception as exc:  # not a decodable image: nothing to ask about
        logger.warning("orientation: page image not decodable (%s)", type(exc).__name__)
        return Guess(None, False)
    info: dict | None = {} if record is not None else None
    t0 = time.monotonic()
    guess, error = Guess(None, False), None
    try:
        answer = llm.chat(
            ORIENTATION_PROMPT,
            [{"type": "text", "text": "How is this page turned?"}, llm.image_part(small)],
            max_tokens=ORIENT_BUDGET,
            thinking=False,
            client=client,
            caller="orientation",
            **({"info": info} if info is not None else {}),
            **({"model": model} if model is not None else {}),
            **({"reasoning_marker": reasoning_marker} if reasoning_marker is not None else {}),
        )
        guess = parse_orientation(answer)
    except llm.LocalLLMError as exc:
        logger.info("orientation: no answer (%s)", type(exc).__name__)
        error = exc
    finally:
        if record is not None:
            _record_usage(record, info or {}, small, time.monotonic() - t0, guess, error)
    return guess


def _record_usage(record: dict, info: dict, image: bytes, seconds: float, guess, error) -> None:
    """Counts of one orientation call (#882); never raises, never the answer."""
    try:

        def count(key):
            value = info.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        finish = info.get("finish_reason")
        ok = len(image) >= 24 and image[:8] == b"\x89PNG\r\n\x1a\n"
        record.update(
            seconds=round(seconds, 1),
            prompt_tokens=count("prompt_tokens"),
            completion_tokens=count("completion_tokens"),
            reasoning_tokens=count("reasoning_tokens"),
            finish=finish if isinstance(finish, str) else None,
            valid=guess.rotation is not None,
            confident=bool(guess.rotation is not None and guess.confident),
            width=int.from_bytes(image[16:20], "big") if ok else None,
            height=int.from_bytes(image[20:24], "big") if ok else None,
        )
        if error is not None:
            record["error"] = type(error).__name__
    except Exception as exc:
        logger.warning("orientation usage not recorded: %s", type(exc).__name__)
    finally:
        info.clear()  # the raw answer channels never outlive the call


def score_reading(text: str | None) -> int:
    """How readable a transcription is (see the module docstring)."""
    body = unicodedata.normalize("NFC", text or "")
    known = other = 0
    for w in _WORD.findall(body):
        if w.lower() in COMMON_WORDS:
            known += 1
        elif _VOWEL.search(w):
            other += 1
    uncertain = body.count("[?]")
    rotated = body.lower().count(ROTATED_REGION)
    return 2 * known + other - 3 * uncertain - 3 * rotated


def doubtful(text: str | None) -> bool:
    """A first reading that may come from a turned page (#882 safety net;
    thresholds documented at ``DOUBTFUL_MIN_SCORE``)."""
    if score_reading(text) < DOUBTFUL_MIN_SCORE:
        return True
    body = unicodedata.normalize("NFC", text or "")
    words = sum(1 for w in _WORD.findall(body) if w.lower() in COMMON_WORDS or _VOWEL.search(w))
    uncertain = body.count("[?]")
    return uncertain >= DOUBTFUL_MIN_UNCERTAIN and uncertain > DOUBTFUL_UNCERTAIN_SHARE * words


def decide(
    png: bytes, read: Callable[[bytes], str], *, hint: Hint | None = None, client=None, ask=None
) -> Oriented:
    """The page's rotation under ``mode()`` (#882). ``prefilter``: an explicit
    ``hint`` or a portrait page is decided without the model; anything else
    (and every page in ``always``) goes to ``orient``."""
    m = mode()
    if m == MODE_OFF:
        return Oriented(0, SOURCE_OFF, png)
    pre = hint.local if hint else 0
    if pre:
        png = rotate_png(png, pre)
    if m == MODE_PREFILTER:
        if hint is not None:
            return Oriented(hint.rotation, hint.source, png, local=pre)
        if is_portrait(png):
            return Oriented(0, SOURCE_PREFILTER, png)
    o = orient(png, read, client=client, ask=ask)
    if pre:  # ``always`` with an EXIF turn MuPDF did not apply: count ours too
        o.rotation = o.local = (o.rotation + pre) % 360
    return o


def settle(
    first: Oriented, text: str | None, read: Callable[[bytes], str], *, client=None, ask=None
) -> Oriented | None:
    """#882 safety net. ``first`` is a page decided without the model and
    ``text`` its reading of ``first.png``. None when nothing changes (not a
    pre-filter page, mode not ``prefilter``, or the reading is not
    ``doubtful``). Otherwise the model is asked once: confident →
    ``prefilter+model`` (``text`` kept when no turn is needed, else None: the
    caller reads ``png`` again); unsure → the two-way reading of ``orient``,
    0° reusing ``text``, with the unsure source (review, as before).
    ``rotation`` / ``local`` of the result include ``first``'s."""
    if mode() != MODE_PREFILTER or first.source not in PREFILTER_SOURCES:
        return None
    if not doubtful(text):
        return None
    png = first.png
    guess = (ask or ask_rotation)(png, client=client)
    if guess.rotation is not None and guess.confident:
        extra, source = guess.rotation, SOURCE_PREFILTER_MODEL
        image, kept = rotate_png(png, extra), (text if extra == 0 else None)
    else:
        start = guess.rotation or 0
        source = SOURCE_MODEL_SCORE if guess.rotation is not None else SOURCE_SCORE
        best: tuple[int, int, bytes, str | None] | None = None
        for angle in (start, (start + 180) % 360):
            turned = rotate_png(png, angle)
            reading = text if angle == 0 else read(turned)
            score = score_reading(reading)
            if best is None or score > best[0]:
                best = (score, angle, turned, reading)
        _, extra, image, kept = best
    return Oriented(
        (first.rotation + extra) % 360, source, image, kept,
        local=(first.local_rotation + extra) % 360,
    )  # fmt: skip


def tally(sources: list[str], model_calls: int) -> dict:
    """Per-document orientation counts (#882), counts only: direction
    questions asked (safety net included) vs pages decided without one."""
    by_source: dict[str, int] = {}
    for s in sources:
        by_source[s] = by_source.get(s, 0) + 1
    return {
        "model_calls": int(model_calls),
        "prefilter_decisions": sum(by_source.get(s, 0) for s in PREFILTER_SOURCES),
        "pages": len(sources),
        "by_source": by_source,
    }


def add_tally(total: dict, part: dict | None) -> dict:
    """``total`` += ``part`` (two ``tally`` dicts, run totals), in place."""
    if not part:
        return total
    for key in ("model_calls", "prefilter_decisions", "pages"):
        total[key] = total.get(key, 0) + int(part.get(key) or 0)
    by = total.setdefault("by_source", {})
    for s, n in (part.get("by_source") or {}).items():
        by[s] = by.get(s, 0) + int(n)
    return total


def orient(png: bytes, read: Callable[[bytes], str], *, client=None, ask=None) -> Oriented:
    """Decide the rotation of one page image. ``read(png) -> text`` is one
    transcription call; it is used only when the direction is unsure, and then
    the kept reading is returned in ``text`` (the caller need not read again).
    ``ask`` replaces ``ask_rotation`` (tests)."""
    if not enabled():
        return Oriented(0, SOURCE_OFF, png)
    guess = (ask or ask_rotation)(png, client=client)
    if guess.rotation is not None and guess.confident:
        return Oriented(guess.rotation, SOURCE_MODEL, rotate_png(png, guess.rotation))
    first = guess.rotation or 0
    source = SOURCE_MODEL_SCORE if guess.rotation is not None else SOURCE_SCORE
    best: Oriented | None = None
    best_score = 0
    for angle in (first, (first + 180) % 360):
        image = rotate_png(png, angle)
        text = read(image)
        score = score_reading(text)
        if best is None or score > best_score:
            best, best_score = Oriented(angle, source, image, text), score
    return best
