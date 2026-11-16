"""Page orientation and chart blocks in the local OCR (#609).

Synthetic only: every page is rendered in the test from invented lab-like
lines (2027 dates), and the "vision model" is a stub that checks which way
the image it receives is turned. No real document, no network.
"""

from __future__ import annotations

import base64
import json

import pymupdf
import pytest

from oncofiles import config
from oncofiles.curated import extract, llm, llm_input, ocr_markers, orientation, reocr
from oncofiles.curated.facts_rules import Vocabulary, extract_lab_facts
from oncofiles.curated.indexer import _apply_sidecar, parse_sidecar

LAB_LINES = [
    "Laboratorny nalez",
    "Odber: 14.03.2027 07:40",
    "Leukocyty 6,37 G/l 4,0 - 11",
    "Hemoglobin 132 g/l 120 - 160",
    "Trombocyty 241 G/l 150 - 400",
]
UPRIGHT_TEXT = (
    "Laboratórny nález\nOdber: 14.03.2027 07:40\nLeukocyty 6,37 G/l 4,0 - 11\n"
    "Hemoglobín 132 g/l 120 - 160\nTrombocyty 241 G/l 150 - 400"
)
GARBLED_TEXT = "ʎʇʎɔ [?] l/ꓨ\nuᴉqolƃ [?]\nɐɹʇs [?]"  # what an upside-down read looks like

# The model's report → the clockwise correction it implies
REPORT_FOR_CORRECTION = {
    0: "upright",
    90: "turned_counterclockwise",
    180: "upside_down",
    270: "turned_clockwise",
}


@pytest.fixture(autouse=True)
def _orientation_on(monkeypatch):
    monkeypatch.setattr(config, "OCR_ORIENTATION", "on")  # the production default


def _page_png(lines=LAB_LINES, chart: bool = False) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=220)
    y = 30
    for line in lines:
        page.insert_text((20, y), line, fontsize=11)
        y += 18
    if chart:  # axes, ticks and a plotted curve: nothing here is data
        page.draw_line((40, 200), (280, 200))
        page.draw_line((40, 200), (40, 130))
        for i, tick in enumerate(("0", "5", "10")):
            page.insert_text((25, 203 - i * 30), tick, fontsize=7)
        page.draw_polyline([(40, 190), (100, 160), (160, 175), (220, 140), (280, 150)])
    png = page.get_pixmap(dpi=100).tobytes("png")
    doc.close()
    return png


def _samples(png: bytes) -> tuple:
    pix = pymupdf.Pixmap(png)
    return pix.width, pix.height, pix.samples


def _turned_by(received: bytes, upright: bytes) -> int | None:
    """How far (clockwise) the received image is turned from the upright page."""
    got = _samples(received)
    for k in orientation.ROTATIONS:
        if _samples(orientation.rotate_png(upright, k)) == got:
            return k
    return None


def _image_of(content) -> bytes:
    part = next(p for p in content if p.get("type") == "image_url")
    return base64.b64decode(part["image_url"]["url"].split(",", 1)[1])


class FakeVision:
    """Stub of the local vision model. The direction question gets the
    scripted ``report``; a transcription is correct only for an upright image."""

    def __init__(self, upright: bytes, report: str | None, upright_answer=UPRIGHT_TEXT):
        self.upright = upright
        self.report = report
        self.upright_answer = upright_answer
        self.orientation_calls = 0
        self.read_turns: list[int | None] = []

    def __call__(self, system, content, **kw):
        if system == orientation.ORIENTATION_PROMPT:
            self.orientation_calls += 1
            assert kw.get("thinking") is False
            return self.report
        turn = _turned_by(_image_of(content), self.upright)
        self.read_turns.append(turn)
        assert turn is not None, "the OCR got an image that is not the page"
        return self.upright_answer if turn == 0 else GARBLED_TEXT


def _confident(correction: int) -> str:
    return json.dumps({"orientation": REPORT_FOR_CORRECTION[correction], "confident": True})


# ── the rotation helper is exact ─────────────────────────────────────────


def test_rotate_png_is_clockwise_and_exact():
    up = _page_png()
    w, h, _ = _samples(up)
    for k in (90, 270):
        assert _samples(orientation.rotate_png(up, k))[:2] == (h, w)
    for k in orientation.ROTATIONS:
        back = orientation.rotate_png(orientation.rotate_png(up, k), (360 - k) % 360)
        assert _samples(back) == _samples(up)
    assert orientation.rotate_png(up, 0) is up


# ── ingest: the page is turned locally, the model only reports ───────────


@pytest.mark.parametrize("scan_turn", [0, 90, 180, 270])
def test_ingest_rotates_before_reading(monkeypatch, scan_turn):
    up = _page_png()
    scan = orientation.rotate_png(up, scan_turn)
    correction = (360 - scan_turn) % 360
    fake = FakeVision(up, _confident(correction))
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract._vision_extraction([scan], 1)
    assert fake.orientation_calls == 1
    assert fake.read_turns == [0]  # one read, of the upright image only
    assert ex.rotation == [correction] and ex.rotation_source == ["model"]
    assert "Leukocyty 6,37" in ex.text
    assert ex.ocr_version == extract.OCR_VERSION
    fm = ex.front_matter()
    assert fm["rotation"] == [correction] and fm["rotation_source"] == ["model"]
    assert fm["has_chart"] is False and fm["ocr_version"] == extract.OCR_VERSION
    assert ex.review_notes() == []  # a confident rotation alone is not a review reason


@pytest.mark.parametrize(
    ("report", "source"),
    [
        (json.dumps({"orientation": "upright", "confident": False}), "model+score"),
        ("It looks upright to me.", "score"),  # not the schema: no answer
        (json.dumps({"orientation": "upright", "confident": True, "x": 1}), "score"),
    ],
)
def test_unsure_reads_both_ways_and_keeps_the_readable_one(monkeypatch, report, source):
    up = _page_png()
    scan = orientation.rotate_png(up, 180)
    fake = FakeVision(up, report)
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract._vision_extraction([scan], 1)
    assert sorted(fake.read_turns) == [0, 180]  # read at 0° and at 180°, no third read
    assert ex.rotation == [180] and ex.rotation_source == [source]
    assert "Leukocyty 6,37" in ex.text and "[?]" not in ex.text
    assert any("orientation unsure" in n for n in ex.review_notes())


def test_orientation_off_reads_the_page_as_it_is(monkeypatch):
    monkeypatch.setattr(config, "OCR_ORIENTATION", "off")
    up = _page_png()
    fake = FakeVision(up, _confident(0))
    monkeypatch.setattr(llm, "chat", fake)
    ex = extract._vision_extraction([up], 1)
    assert fake.orientation_calls == 0
    assert ex.rotation == [0] and ex.rotation_source == ["off"]


def test_orientation_error_is_no_answer(monkeypatch):
    up = _page_png()

    def chat(system, content, **kw):
        if system == orientation.ORIENTATION_PROMPT:
            raise llm.LocalLLMError("local LLM answer truncated")
        return FakeVision(up, None)(system, content, **kw)

    monkeypatch.setattr(llm, "chat", chat)
    ex = extract._vision_extraction([orientation.rotate_png(up, 180)], 1)
    assert ex.rotation == [180] and ex.rotation_source == ["score"]


def test_parse_orientation_is_strict():
    ok = orientation.parse_orientation(
        '```json\n{"orientation": "upside_down", "confident": true}\n```'
    )
    assert (ok.rotation, ok.confident) == (180, True)
    for bad in (
        "",
        "upright",
        '{"orientation": "sideways", "confident": true}',
        '{"orientation": "upright", "confident": "yes"}',
        '{"orientation": "upright"}',
        "[1, 2]",
    ):
        assert orientation.parse_orientation(bad).rotation is None


def test_score_prefers_real_words_over_fragments_and_markers():
    assert orientation.score_reading(UPRIGHT_TEXT) > orientation.score_reading(GARBLED_TEXT)
    assert orientation.score_reading("Leukocyty 6,3[?]") < orientation.score_reading(
        "Leukocyty 6,37"
    )
    assert orientation.score_reading("[rotated region]") < 0


# ── charts: fenced, routed to review, never data ─────────────────────────

CHART_ANSWER = (
    "Leukocyty 6,37 G/l 4,0 - 11\n[chart: Leukocyty v čase; y: G/l 0 5 10 [?]; x: 01/2027 02/2027]"
)


def test_chart_page_is_flagged_and_goes_to_review(monkeypatch):
    up = _page_png(LAB_LINES[:3], chart=True)
    monkeypatch.setattr(llm, "chat", FakeVision(up, _confident(0), CHART_ANSWER))
    ex = extract._vision_extraction([up], 1)
    assert ex.chart_pages == [1]
    assert ex.front_matter()["has_chart"] is True
    assert any(n.startswith("chart on page 1") for n in ex.review_notes())


def test_ocr_prompt_asks_for_markers_with_examples():
    p = extract.OCR_SYSTEM_PROMPT
    assert "[chart:" in p and "[rotated region]" in p and "[?]" in p
    assert p.count("Examples") == 1 and "never write such a value" in p
    assert reocr.REOCR_SYSTEM_PROMPT.startswith(p)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A\n[chart: CEA; y: 0 5 10]\nB", "A\n[chart]\nB"),
        ("[Chart: x [?] 12] tail", "[chart] tail"),  # nested [?], any case
        ("a [chart: 1] b [chart: 2] c", "a [chart] b [chart] c"),
        ("head\n[chart: y: 0 5\n10 15", "head\n[chart]"),  # unclosed: rest of page
        ("no chart 12,5 [?]", "no chart 12,5 [?]"),
    ],
)
def test_strip_charts(text, expected):
    assert ocr_markers.strip_charts(text) == expected


@pytest.fixture
async def vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


async def test_lab_rule_parsers_ignore_chart_values(vocab):
    plain = "Lab 4.10.2027: WBC 5,10; HGB 130; PLT 240"
    page = plain + "\n[chart: Lab 4.10.2027: WBC 9,90; HGB 99; PLT 77]"
    with_chart, _ = extract_lab_facts([page], vocab, report_date="2027-10-08", inline=True)
    without, _ = extract_lab_facts([plain], vocab, report_date="2027-10-08", inline=True)
    values = sorted((f["code"], f["value_num"]) for f in with_chart)
    assert values == sorted((f["code"], f["value_num"]) for f in without)
    assert ("WBC", 5.1) in values and all(v not in (9.9, 99, 77) for _, v in values)


async def test_chart_only_page_yields_no_lab_facts(vocab):
    page = "[chart: Lab 4.10.2027: WBC 9,90; HGB 99; PLT 77]"
    facts, extractor = extract_lab_facts([page], vocab, report_date="2027-10-08", inline=True)
    assert facts == [] and extractor is None


class _PagesDB:
    def __init__(self, text):
        self.text = text

    async def get_ocr_pages(self, doc_id, *, patient_id):
        return [{"page_number": 1, "extracted_text": self.text}]


async def test_llm_input_strips_charts_before_any_model_call():
    doc = {"id": 7, "filename": "scan.pdf", "mime_type": "application/pdf", "category": "labs"}
    pages = await llm_input.load_for_llm(_PagesDB(CHART_ANSWER), "pid", doc)
    assert pages == [(1, "Leukocyty 6,37 G/l 4,0 - 11\n[chart]")]


@pytest.mark.parametrize("module", ["llm_extract", "llm_extract_clinical"])
def test_llm_extractors_never_send_chart_content(module):
    import importlib

    from oncofiles.cloud_guard import local_only

    mod = importlib.import_module(f"oncofiles.curated.{module}")
    vocab = Vocabulary.from_rows([
        {"code": "WBC", "category": "lab", "unit_canonical": "G/l", "aliases": ["Leukocyty"],
         "unit_conversions": {}},
    ])  # fmt: skip
    sent: list[str] = []

    def chat(*a, **k):
        sent.append(json.dumps([a, k], ensure_ascii=False, default=str))
        return "{}"

    page = "Leukocyty 6,37 G/l 4,0 - 11\n[chart: Leukocyty; y: 0 5 10; bod 8,88]"
    with local_only("test"):
        mod.extract_document([(1, page)], vocab=vocab, chat=chat)
    assert sent and all("8,88" not in s for s in sent)
    assert any("6,37" in s for s in sent)


# ── sidecars: new keys written, old sidecars still parse ─────────────────

OLD_SIDECAR = (
    '---\nsource_file: "x.pdf"\ndoc_type: "Labs"\nextraction: "vision"\n'
    'ocr_engine: "qwen-test"\nhandwritten: false\n---\n\n<!-- page 1 -->\nLeukocyty 6,37\n'
)


def _rec():
    from oncofiles.curated.indexer import IndexRecord
    from oncofiles.models import DocumentCategory

    return IndexRecord(
        gdrive_id="g1", filename="x.pdf", folder="labs", category=DocumentCategory.LABS,
        mime_type="application/pdf", size_bytes=1, modified="",
    )  # fmt: skip


def test_old_sidecar_without_new_keys_still_parses():
    front, pages = parse_sidecar(OLD_SIDECAR)
    assert pages == ["Leukocyty 6,37"]
    assert "rotation" not in front and "has_chart" not in front


def test_sidecar_keys_round_trip_through_runner_and_indexer(monkeypatch):
    from oncofiles.curated import runner

    up = _page_png()
    monkeypatch.setattr(llm, "chat", FakeVision(up, _confident(180), UPRIGHT_TEXT))
    ex = extract._vision_extraction([orientation.rotate_png(up, 180)], 1)
    keys = runner._vision_keys(ex)
    assert keys == {"ocr_version": extract.OCR_VERSION, "rotation": [180],
                    "rotation_source": ["model"], "has_chart": False,
                    "rotated_region": False}  # fmt: skip
    front_text = "\n".join(f"{k}: {runner._y(v)}" for k, v in keys.items())
    front, _ = parse_sidecar(f"---\n{front_text}\n---\n\nbody\n")
    assert front["rotation"] == [180] and front["has_chart"] is False
    assert runner._vision_keys(extract.Extraction("t", "text_layer", 1, "pymupdf")) == {}


# ── re-OCR: the same implementation ──────────────────────────────────────


def _one_page_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page(width=300, height=220).insert_text((20, 30), "x")
    data = doc.tobytes()
    doc.close()
    return data


def test_reocr_passes_read_the_rotated_page(monkeypatch):
    up = _page_png()
    scan = orientation.rotate_png(up, 90)  # the scanned page is a quarter turn off
    monkeypatch.setattr(extract, "_pixmap_png", lambda page, dpi=None: scan)
    fake = FakeVision(up, _confident(270))
    monkeypatch.setattr(llm, "chat", fake)
    usage: list[dict] = []
    diag: dict = {}
    passes = reocr._vision_passes(_one_page_pdf(), "pdf", [100, 120], client=object(),
                                  usage=usage, diag=diag)  # fmt: skip
    assert fake.orientation_calls == 1
    assert fake.read_turns == [0, 0]  # both passes saw the upright page
    assert [p[0] for p in passes] == [UPRIGHT_TEXT, UPRIGHT_TEXT]
    assert diag["rotation"] == [270] and diag["rotation_source"] == ["model"]
    assert not any(r.get("orientation_probe") for r in usage)


def test_reocr_unsure_probes_are_recorded_not_used(monkeypatch):
    up = _page_png()
    scan = orientation.rotate_png(up, 180)
    monkeypatch.setattr(extract, "_pixmap_png", lambda page, dpi=None: scan)
    fake = FakeVision(up, json.dumps({"orientation": "upright", "confident": False}))
    monkeypatch.setattr(llm, "chat", fake)
    usage: list[dict] = []
    diag: dict = {}
    passes = reocr._vision_passes(_one_page_pdf(), "pdf", [100], client=object(),
                                  usage=usage, diag=diag)  # fmt: skip
    assert sorted(fake.read_turns[:2]) == [0, 180] and fake.read_turns[2:] == [0]
    assert passes == [[UPRIGHT_TEXT]]
    assert diag["rotation"] == [180] and diag["rotation_source"] == ["model+score"]
    assert sum(1 for r in usage if r.get("orientation_probe")) == 2


def _reread(**kw) -> reocr.Reread:
    base = {"pages": ["Leukocyty 6,37 G/l"], "method": "vision2", "engine": "qwen-test"}
    return reocr.Reread(**{**base, **kw})


def test_reocr_review_codes_and_sidecar_keys():
    from datetime import UTC, datetime

    assert reocr.vision_review_codes(_reread(rotation=[0], rotation_source=["model"])) == []
    rr = _reread(rotation=[180], rotation_source=["model+score"], chart_pages=[1],
                 rotated_region_pages=[1], ocr_version=extract.OCR_VERSION)  # fmt: skip
    assert reocr.vision_review_codes(rr) == [
        "chart_page", "rotated_region", "orientation_unsure",
    ]  # fmt: skip
    now = datetime(2027, 3, 14, tzinfo=UTC)
    new = reocr.rewrite_sidecar(OLD_SIDECAR, rr, previous_engine="qwen-test", now=now)
    front, _ = parse_sidecar(new)
    assert front["rotation"] == [180] and front["rotation_source"] == ["model+score"]
    assert front["has_chart"] is True and front["ocr_version"] == extract.OCR_VERSION
    # a text-layer re-read drops stale vision keys of an earlier reading
    layer = reocr.Reread(["Leukocyty 6,37 G/l"], "text_layer", "pymupdf")
    again, _ = parse_sidecar(reocr.rewrite_sidecar(new, layer, previous_engine="x", now=now))
    assert not {"rotation", "has_chart", "ocr_version"} & set(again)


def test_index_metadata_carries_the_keys():
    rec = _rec()
    front, pages = parse_sidecar(OLD_SIDECAR.replace(
        "handwritten: false\n", "handwritten: false\nrotation: [90]\nhas_chart: true\n"
    ))  # fmt: skip
    _apply_sidecar(rec, front, pages)
    assert rec.metadata["rotation"] == [90] and rec.metadata["has_chart"] is True
    old = _rec()
    _apply_sidecar(old, *parse_sidecar(OLD_SIDECAR))
    assert "rotation" not in old.metadata


# ── a curated run sends chart / rotated pages to review ──────────────────


def _run_with(vision_extract, *, dry_run: bool):
    from oncofiles.curated.runner import RunOptions, run_curated
    from tests.test_curated_runner import INBOX, NOW, FakeDrive, fake_propose

    d = FakeDrive()
    d.add("scan_001.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026")
    r = run_curated(d, d.root, RunOptions(now=NOW, run_id="t", dry_run=dry_run),
                    extract_fn=vision_extract, propose_fn=fake_propose)  # fmt: skip
    doc = next(x for x in r.documents if x["source"] == "scan_001.pdf")
    return d, doc, INBOX


@pytest.mark.parametrize(
    ("vision", "note"),
    [
        ({"chart_pages": [1]}, "chart on page 1"),
        ({"rotation": [0], "rotation_source": ["score"]}, "orientation unsure on page 1"),
        ({"rotation": [180], "rotation_source": ["model+score"]}, "orientation unsure on page 1"),
        ({"rotated_region_pages": [1]}, "rotated region on page 1"),
    ],
)
def test_run_routes_vision_flags_to_review(vision, note):
    def vision_extract(name, content, mime="", *, min_text_chars=100):
        fields = {"rotation": [0], "rotation_source": ["model"], **vision}
        return extract.Extraction(content.decode(), "vision", 1, "qwen-test",
                                  ocr_version=extract.OCR_VERSION, **fields)  # fmt: skip

    d, doc, inbox = _run_with(vision_extract, dry_run=False)
    assert doc["result"] == "review_needed"
    sidecar = d.items[d.find(d.folder[inbox], "scan_001.pdf.md")]["content"].decode()
    front, _ = parse_sidecar(sidecar)
    assert front["review_needed"] is True
    assert any(r.startswith(note) for r in front["review_reasons"])
    assert front["ocr_version"] == extract.OCR_VERSION and "rotation" in front


def test_run_files_an_upright_vision_page_without_flags():
    def vision_extract(name, content, mime="", *, min_text_chars=100):
        return extract.Extraction(
            content.decode(), "vision", 1, "qwen-test", rotation=[0],
            rotation_source=["model"], ocr_version=extract.OCR_VERSION,
        )  # fmt: skip

    _, doc, _ = _run_with(vision_extract, dry_run=True)
    assert doc["result"] == "filed"


def test_confident_rotation_is_filed_and_recorded():
    def vision_extract(name, content, mime="", *, min_text_chars=100):
        return extract.Extraction(
            content.decode(), "vision", 1, "qwen-test", rotation=[180],
            rotation_source=["model"], ocr_version=extract.OCR_VERSION,
        )  # fmt: skip

    d, doc, _ = _run_with(vision_extract, dry_run=False)
    assert doc["result"] == "filed"
    sidecar = next(v["content"].decode() for v in d.items.values()
                   if v["name"].endswith(".pdf.md"))  # fmt: skip
    front, _ = parse_sidecar(sidecar)
    assert front["rotation"] == [180] and front["rotation_source"] == ["model"]
    assert front["review_needed"] is False


def test_reocr_confident_rotation_does_not_block():
    assert reocr.vision_review_codes(_reread(rotation=[90], rotation_source=["model"])) == []
    assert reocr.vision_review_codes(_reread(rotation=[180], rotation_source=["model+score"])) == [
        "orientation_unsure"
    ]
