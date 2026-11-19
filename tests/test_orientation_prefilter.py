"""Orientation pre-filter before the direction question (#882, #609 follow-up).

A dry spot check found no turned page in 23 while the direction question cost
about 16 % per page. Now an explicit EXIF / PDF rotation is honoured and a
portrait page is taken as upright without a model call; landscape pages still
ask, and a doubtful first reading of a pre-filter page asks once.

Synthetic only: pages are rendered here from invented lines (2027 dates), the
"vision model" is a stub, no network.
"""

from __future__ import annotations

import base64
import json
import logging
import struct

import pymupdf
import pytest

from oncofiles import config
from oncofiles.curated import extract, llm, orientation, reocr

UPRIGHT_TEXT = (
    "Laboratórny nález\nOdber: 14.03.2027 07:40\nLeukocyty 6,37 G/l 4,0 - 11\n"
    "Hemoglobín 132 g/l 120 - 160\nTrombocyty 241 G/l 150 - 400"
)
GARBLED_TEXT = "ʎʇʎɔ [?] l/ꓨ\nuᴉqolƃ [?]\nɐɹʇs [?]"
LINES = ["Laboratorny nalez", "Odber: 14.03.2027 07:40", "Leukocyty 6,37 G/l 4,0 - 11"]
PAD, SIDE = 10, 80  # the corner block, in points
BLACK = pymupdf.pdfcolor["black"]
MIRRORED = 5  # an EXIF value that is a mirror, not a rotation
SOI = 2  # bytes of the JPEG start marker
REPORT = {0: "upright", 90: "turned_counterclockwise", 180: "upside_down", 270: "turned_clockwise"}


@pytest.fixture(autouse=True)
def _prefilter(monkeypatch):
    monkeypatch.setattr(config, "OCR_ORIENTATION", "prefilter")  # the production default


def _confident(correction: int) -> str:
    return json.dumps({"orientation": REPORT[correction], "confident": True})


def _page_png(width: int, height: int, *, block: bool = False) -> bytes:
    """A synthetic page; ``block``: a black square in the top-left corner,
    so which way the page is turned survives JPEG compression."""
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    if block:
        corner = pymupdf.Rect(PAD, PAD, PAD + SIDE, PAD + SIDE)
        page.draw_rect(corner, color=BLACK, fill=BLACK)
    y = 120 if block else 30
    for line in LINES:
        page.insert_text((20, y), line, fontsize=11)
        y += 18
    png = page.get_pixmap(dpi=72).tobytes("png")
    doc.close()
    return png


def _dark_corner(png: bytes) -> str:
    """The darkest quadrant of an image: tl | tr | bl | br."""
    pix = pymupdf.Pixmap(png)
    w, h = pix.width, pix.height

    def ink(x0, y0):
        total = 0
        for yy in range(12):
            for xx in range(12):
                x = x0 + int((xx + 0.5) * (w / 2) / 12)
                y = y0 + int((yy + 0.5) * (h / 2) / 12)
                total += 255 - sum(pix.pixel(x, y)[:3]) // 3
        return total

    hw, hh = w // 2, h // 2
    left = top = 0
    quads = {"tl": (left, top), "tr": (hw, top), "bl": (left, hh), "br": (hw, hh)}
    return max(quads, key=lambda q: ink(*quads[q]))


def _samples(png: bytes) -> tuple:
    pix = pymupdf.Pixmap(png)
    return pix.width, pix.height, pix.samples


def _image_of(content) -> bytes:
    part = next(p for p in content if p.get("type") == "image_url")
    return base64.b64decode(part["image_url"]["url"].split(",", 1)[1])


def _exif_segment(value: int, little: bool = False) -> bytes:
    end = "<" if little else ">"
    ifd = struct.pack(end + "H", 1) + struct.pack(end + "HHI", 0x0112, 3, 1)
    ifd += struct.pack(end + "H", value) + b"\x00\x00" + struct.pack(end + "I", 0)
    tiff = (b"II*\x00" if little else b"MM\x00*") + struct.pack(end + "I", 8) + ifd
    body = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body


def _jpeg(raster_png: bytes, exif: int | None, little: bool = False) -> bytes:
    """A phone-photo-like JPEG: the stored raster plus an EXIF Orientation."""
    data = pymupdf.Pixmap(raster_png).tobytes("jpeg")
    return data if exif is None else data[:SOI] + _exif_segment(exif, little) + data[SOI:]


class Fake:
    """Stub vision model. ``upright``: the exact upright PNG (None = accept
    anything, b"" = nothing); a transcription of any other image is garbled."""

    def __init__(self, report: str | None = None, upright: bytes | None = None):
        self.report = report
        self.upright = upright
        self.orientation_calls = 0
        self.reads: list[bytes] = []

    def __call__(self, system, content, **kw):
        if system == orientation.ORIENTATION_PROMPT:
            self.orientation_calls += 1
            return self.report
        image = _image_of(content)
        self.reads.append(image)
        if self.upright is None:
            return UPRIGHT_TEXT
        if self.upright == b"":  # a page the model never reads well
            return GARBLED_TEXT
        ok = _samples(image) == _samples(self.upright) or any(
            _samples(image) == _samples(half) for half in reocr.split_png(self.upright, 2)
        )
        return UPRIGHT_TEXT if ok else GARBLED_TEXT


# ── EXIF parser (stdlib) ──────────────────────────────────────────────────


@pytest.mark.parametrize("little", [False, True])
@pytest.mark.parametrize("value", [*sorted(orientation.EXIF_ROTATION), MIRRORED])
def test_jpeg_info_reads_exif_orientation_and_size(value, little):
    raster = _page_png(200, 120)
    value_read, size = orientation.jpeg_info(_jpeg(raster, value, little))
    assert value_read == value and size == (200, 120)


def test_jpeg_info_never_raises():
    assert orientation.jpeg_info(b"") == (None, None)
    assert orientation.jpeg_info(b"\x89PNG not a jpeg") == (None, None)
    start = b"\xff\xd8\xff\xe1"
    header = struct.pack(">H", 16) + b"Exif" + bytes(SOI)
    broken = start + header + b"MM" + bytes(1) + b"*" + b"\xff" * 4
    assert orientation.jpeg_info(broken)[0] is None
    assert orientation.jpeg_info(_jpeg(_page_png(80, 60), None)) == (None, (80, 60))


def test_image_hint_turns_locally_when_the_renderer_did_not():
    raster = _page_png(200, 120)  # landscape raster, EXIF 6 = shown portrait
    data = _jpeg(raster, 6)
    applied = orientation.rotate_png(raster, 90)
    assert orientation.image_hint(data, applied) == orientation.Hint(90, "exif", 0)
    assert orientation.image_hint(data, raster) == orientation.Hint(90, "exif", 90)
    assert orientation.image_hint(_jpeg(raster, 1), raster) is None  # identity: no flag
    assert orientation.image_hint(_jpeg(raster, 5), raster) is None  # mirrored: ignored


# ── 1. explicit rotation: no model call ───────────────────────────────────


TURNS = [(v, a) for v, a in orientation.EXIF_ROTATION.items() if a]


@pytest.mark.parametrize(("exif", "angle"), TURNS)
def test_exif_rotated_jpeg_is_upright_without_a_model_call(monkeypatch, exif, angle):
    up = _page_png(300, 420, block=True)
    raster = orientation.rotate_png(up, (360 - angle) % 360)  # how the camera stored it
    fake = Fake()
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract.extract("photo.jpg", _jpeg(raster, exif), "image/jpeg")
    assert ex.ok and ex.method == "vision"
    assert fake.orientation_calls == 0
    [read] = fake.reads
    assert _dark_corner(read) == "tl"  # the model read the upright page
    assert ex.rotation == [angle] and ex.rotation_source == ["exif"]
    assert ex.orientation["model_calls"] == 0 and ex.orientation["prefilter_decisions"] == 1
    assert ex.review_notes() == []


def _rotated_pdf(rotate: int) -> bytes:
    """A scan whose page is stored turned, with /Rotate making it upright."""
    doc = pymupdf.open()
    w, h = (420, 300) if rotate in (90, 270) else (300, 420)
    page = doc.new_page(width=w, height=h)
    page.set_rotation(rotate)
    shown = pymupdf.Rect(PAD, PAD, PAD + SIDE, PAD + SIDE) * page.derotation_matrix
    page.draw_rect(shown, color=BLACK, fill=BLACK)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_pdf_rotate_is_upright_without_a_model_call(monkeypatch, rotate):
    fake = Fake()
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract.extract("scan.pdf", _rotated_pdf(rotate), "application/pdf")
    assert ex.method == "vision"
    assert fake.orientation_calls == 0
    [read] = fake.reads
    assert _dark_corner(read) == "tl" and orientation.is_portrait(read)
    assert ex.rotation == [rotate] and ex.rotation_source == ["pdf"]


def test_reocr_honours_exif_without_a_model_call(monkeypatch):
    up = _page_png(300, 420, block=True)
    data = _jpeg(orientation.rotate_png(up, 270), 6)
    fake = Fake()
    monkeypatch.setattr(llm, "chat", fake)
    usage: list[dict] = []
    diag: dict = {}
    reocr._vision_passes(data, "jpg", [72, 100], client=object(), usage=usage, diag=diag)
    assert fake.orientation_calls == 0
    assert all(_dark_corner(r) in ("tl", "tr") for r in fake.reads[1:])  # halves of pass 2
    assert _dark_corner(fake.reads[0]) == "tl"
    assert diag["rotation"] == [90] and diag["rotation_source"] == ["exif"]
    assert diag["orientation"]["model_calls"] == 0
    assert not any(r.get("kind") == "orientation" for r in usage)


def test_reocr_honours_pdf_rotate_without_a_model_call(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(llm, "chat", fake)
    diag: dict = {}
    reocr._vision_passes(_rotated_pdf(90), "pdf", [72], client=object(), diag=diag)
    assert fake.orientation_calls == 0 and _dark_corner(fake.reads[0]) == "tl"
    assert diag["rotation"] == [90] and diag["rotation_source"] == ["pdf"]


# ── 2./3. portrait → no call; landscape → today's model path ─────────────


def test_portrait_page_needs_no_model_call(monkeypatch):
    up = _page_png(220, 300)
    fake = Fake(_confident(0), up)
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract._vision_extraction([up], 1)
    assert fake.orientation_calls == 0 and len(fake.reads) == 1
    assert ex.rotation == [0] and ex.rotation_source == ["prefilter"]
    assert ex.orientation["model_calls"] == 0
    assert ex.orientation["prefilter_decisions"] == ex.orientation["pages"] == 1
    assert ex.orientation["by_source"] == {"prefilter": 1}


@pytest.mark.parametrize("size", [(300, 220), (300, 310)])  # landscape, near-square
def test_landscape_or_square_page_asks_the_model(monkeypatch, size):
    up = _page_png(*size)
    fake = Fake(_confident(0), up)
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract._vision_extraction([up], 1)
    assert fake.orientation_calls == 1
    assert ex.rotation_source == ["model"]
    assert ex.orientation["model_calls"] == 1 and ex.orientation["prefilter_decisions"] == 0


# ── 4. safety net: a doubtful first reading asks once ─────────────────────


def test_bad_first_read_on_prefilter_page_asks_once_and_rereads(monkeypatch):
    up = _page_png(220, 300)
    scan = orientation.rotate_png(up, 180)  # portrait, but upside down
    fake = Fake(_confident(180), up)
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract._vision_extraction([scan], 1)
    assert fake.orientation_calls == 1
    assert len(fake.reads) == 2 and _samples(fake.reads[1]) == _samples(up)
    assert ex.rotation == [180] and ex.rotation_source == ["prefilter+model"]
    assert "Leukocyty 6,37" in ex.text and "[?]" not in ex.text
    assert ex.review_notes() == []  # a confident turn is not a review reason
    assert ex.orientation["model_calls"] == 1


def test_bad_first_read_model_says_upright_keeps_the_reading(monkeypatch):
    up = _page_png(220, 300)
    fake = Fake(_confident(0), upright=b"")  # every reading is garbled
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract._vision_extraction([up], 1)
    assert fake.orientation_calls == 1 and len(fake.reads) == 1
    assert ex.rotation == [0] and ex.rotation_source == ["prefilter+model"]
    assert ex.text.endswith(GARBLED_TEXT)  # read once, not again


def test_bad_first_read_unsure_model_reads_the_other_way_and_goes_to_review(monkeypatch):
    up = _page_png(220, 300)
    scan = orientation.rotate_png(up, 180)
    fake = Fake(json.dumps({"orientation": "upright", "confident": False}), up)
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract._vision_extraction([scan], 1)
    assert fake.orientation_calls == 1
    assert len(fake.reads) == 2  # the upright reading is reused, the other way is read
    assert ex.rotation == [180] and ex.rotation_source == ["model+score"]
    assert "Leukocyty 6,37" in ex.text
    assert any("orientation unsure" in n for n in ex.review_notes())


def test_good_first_read_never_asks():
    assert not orientation.doubtful(UPRIGHT_TEXT)
    assert orientation.doubtful(GARBLED_TEXT)
    assert orientation.doubtful("")  # empty / picture-only page asks once
    many_uncertain = "Leukocyty [?] Hemoglobín [?] Trombocyty [?] pacient [?] " * 2
    assert orientation.doubtful(many_uncertain)
    assert not orientation.doubtful(UPRIGHT_TEXT + "\nhodnota 12 [?]")


def test_reocr_safety_net_rereads_all_passes_upright(monkeypatch):
    up = _page_png(220, 300)
    scan = orientation.rotate_png(up, 180)
    monkeypatch.setattr(extract, "_pixmap_png", lambda page, dpi=None: scan)
    fake = Fake(_confident(180), up)
    monkeypatch.setattr(llm, "chat", fake)
    usage: list[dict] = []
    diag: dict = {}
    pdf = pymupdf.open()
    pdf.new_page(width=220, height=300).insert_text((20, 30), "x")
    passes = reocr._vision_passes(pdf.tobytes(), "pdf", [72, 100], client=object(),
                                  usage=usage, diag=diag)  # fmt: skip
    assert fake.orientation_calls == 1
    # the first pass garbled (upside down), then everything again, upright
    assert len(fake.reads) == 4
    assert passes[0] == [UPRIGHT_TEXT]
    assert diag["rotation"] == [180] and diag["rotation_source"] == ["prefilter+model"]
    [rec] = [r for r in usage if r.get("kind") == "orientation"]
    assert rec["recheck"] is True and rec["valid"] and rec["confident"]
    assert diag["orientation"]["model_calls"] == 1


def test_reocr_portrait_page_has_no_orientation_record(monkeypatch):
    up = _page_png(220, 300)
    monkeypatch.setattr(extract, "_pixmap_png", lambda page, dpi=None: up)
    fake = Fake(_confident(0), up)
    monkeypatch.setattr(llm, "chat", fake)
    usage: list[dict] = []
    diag: dict = {}
    pdf = pymupdf.open()
    pdf.new_page(width=220, height=300).insert_text((20, 30), "x")
    reocr._vision_passes(pdf.tobytes(), "pdf", [72, 100], client=object(), usage=usage,
                         diag=diag)  # fmt: skip
    assert fake.orientation_calls == 0 and len(fake.reads) == 3
    assert diag["rotation_source"] == ["prefilter"]
    assert not any(r.get("kind") == "orientation" for r in usage)


# ── 5. modes ──────────────────────────────────────────────────────────────


def test_always_asks_every_page(monkeypatch):
    monkeypatch.setattr(config, "OCR_ORIENTATION", "always")
    up = _page_png(220, 300)
    scan = orientation.rotate_png(up, 180)
    fake = Fake(_confident(180), up)
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract._vision_extraction([up, scan], 2)
    assert fake.orientation_calls == 2
    assert ex.rotation_source == ["model", "model"]


def test_always_ignores_the_exif_shortcut(monkeypatch):
    monkeypatch.setattr(config, "OCR_ORIENTATION", "always")
    up = _page_png(300, 420, block=True)
    fake = Fake(_confident(0))
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract.extract("photo.jpg", _jpeg(orientation.rotate_png(up, 270), 6), "image/jpeg")
    assert fake.orientation_calls == 1 and ex.rotation_source == ["model"]


def test_off_never_rotates(monkeypatch):
    monkeypatch.setattr(config, "OCR_ORIENTATION", "off")
    up = _page_png(300, 220)
    fake = Fake(_confident(180), up)
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract._vision_extraction([orientation.rotate_png(up, 180)], 1)
    assert fake.orientation_calls == 0 and len(fake.reads) == 1
    assert ex.rotation == [0] and ex.rotation_source == ["off"]
    data = _jpeg(orientation.rotate_png(_page_png(300, 420), 270), 6)
    assert extract.extract("p.jpg", data).rotation_source == ["off"]


def test_unknown_mode_is_prefilter_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(config, "OCR_ORIENTATION", "sometimes-xyz")
    with caplog.at_level(logging.WARNING, logger="oncofiles.curated.orientation"):
        assert orientation.mode() == "prefilter"
        assert orientation.mode() == "prefilter"
    assert sum("sometimes-xyz" in r.getMessage() for r in caplog.records) == 1
    for legacy, expected in (("on", "prefilter"), ("false", "off"), ("ALWAYS", "always")):
        monkeypatch.setattr(config, "OCR_ORIENTATION", legacy.strip().lower())
        assert orientation.mode() == expected


def test_default_is_prefilter():
    from pathlib import Path

    src = Path(config.__file__).read_text(encoding="utf-8")
    assert '"LOCAL_LLM_OCR_ORIENTATION", "prefilter"' in src


# ── 6. telemetry: counts per run ──────────────────────────────────────────


def test_run_counts_orientation_and_cache_hits_ask_nothing(monkeypatch):
    from oncofiles.curated.runner import RunReport
    from oncofiles.tools import curated as curated_tools

    portrait = pymupdf.open()
    portrait.new_page(width=220, height=300).insert_text((20, 30), "x")
    landscape = pymupdf.open()
    landscape.new_page(width=300, height=220).insert_text((20, 30), "x")
    fake = Fake(_confident(0))
    monkeypatch.setattr(llm, "chat", fake)
    report = RunReport(run_id="t", dry_run=True)
    pdfs = [doc.tobytes() + b"%synthetic-882" for doc in (portrait, landscape)]
    for pdf in pdfs:
        report.count_orientation(curated_tools.cached_extract("s.pdf", pdf, "application/pdf"))
    assert report.orientation["model_calls"] == report.orientation["prefilter_decisions"]
    assert report.orientation["model_calls"] == 1
    assert report.orientation["pages"] == 2
    assert report.orientation["by_source"] == {"prefilter": 1, "model": 1}
    again = curated_tools.cached_extract("s.pdf", pdfs[1], "application/pdf")
    assert again.orientation["model_calls"] == 0 and fake.orientation_calls == 1
    assert report.to_dict()["orientation"]["model_calls"] == 1


def test_reocr_report_sums_orientation():
    rep = reocr.ReocrReport(dry_run=True)
    for t in (orientation.tally(["prefilter", "exif"], 0),
              orientation.tally(["model", "prefilter+model"], 2)):  # fmt: skip
        doc = reocr.DocResult(1, "x.pdf", [])
        doc.orientation = t
        rep.documents.append(doc)
    total = rep.to_dict()["orientation"]
    assert total["model_calls"] == 2 and total["prefilter_decisions"] == 2
    assert total["pages"] == 4 and total["by_source"]["prefilter+model"] == 1
