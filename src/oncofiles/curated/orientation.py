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
UNSURE_SOURCES = frozenset({SOURCE_MODEL_SCORE, SOURCE_SCORE})

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
    source: str  # model | model+score | score | off (blank: re-OCR only)
    png: bytes  # the page image as it was (or will be) read
    text: str | None = None  # the kept reading when both orientations were read

    @property
    def unsure(self) -> bool:
        return self.source in UNSURE_SOURCES


def enabled() -> bool:
    return str(getattr(config, "OCR_ORIENTATION", "on")).strip().lower() not in (
        "off", "false", "0", "no",
    )  # fmt: skip


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


def ask_rotation(png: bytes, *, client=None) -> Guess:
    """The model's report of how the page is turned (never a transcription).
    A per-call failure is "no answer", which makes the caller read both ways;
    an unreachable model fails again on the transcription call itself."""
    try:
        small = _render(png, 0, ORIENT_MAX_SIDE)
    except Exception as exc:  # not a decodable image: nothing to ask about
        logger.warning("orientation: page image not decodable (%s)", type(exc).__name__)
        return Guess(None, False)
    try:
        answer = llm.chat(
            ORIENTATION_PROMPT,
            [{"type": "text", "text": "How is this page turned?"}, llm.image_part(small)],
            max_tokens=ORIENT_BUDGET,
            thinking=False,
            client=client,
        )
    except llm.LocalLLMError as exc:
        logger.info("orientation: no answer (%s)", type(exc).__name__)
        return Guess(None, False)
    return parse_orientation(answer)


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
