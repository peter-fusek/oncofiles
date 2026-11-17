"""The two re-OCR vision passes are independent readings (#883, #882).

Production showed identical prompt / completion / reasoning tokens for pass 1
(150 dpi) and pass 2 (220 dpi) on every page: our code sent two different
images, but the model server fits every image into its own input size, so
both passes reached the model as the same picture and the "digits accepted
only where both passes agree" guard compared a reading with itself. Pass 2
now reads the page as two overlapping halves, each its own call with its own
text before the image. These tests use a fake model only (no network) and
synthetic values (dates 2027+)."""

from __future__ import annotations

import base64
import json

import pymupdf
import pytest

from oncofiles import config
from oncofiles.curated import extract, llm, orientation, reocr

# (pass, half) of each call on one page: pass one whole, pass two as two halves
PASS_ONE = (1, None)
TOP = (2, 1)
BOTTOM = (2, 2)
CALLS = (PASS_ONE, TOP, BOTTOM)

TOP_LINE = "Leukocyty 6,37 G/l 4,0 - 11"
BOTTOM_LINE = "Hemoglobin 128 g/l 120 - 160"


def _scan(pages: int = 1) -> bytes:
    """A scan-like PDF: ink but no text layer, so the vision path is taken
    and the page is not a blank back side (which skips the orientation call)."""
    doc = pymupdf.open()
    black = pymupdf.pdfcolor["black"]
    for _ in range(pages):
        doc.new_page().draw_rect(pymupdf.Rect(61, 83, 413, 297), color=black, fill=black)
    data = doc.tobytes()
    doc.close()
    return data


def _image(content) -> bytes:
    part = next(p for p in content if p.get("type") == "image_url")
    return base64.b64decode(part["image_url"]["url"].split(",", 1)[1])


def _size(png: bytes) -> tuple[int, int]:
    pix = pymupdf.Pixmap(png)
    return pix.width, pix.height


def _fitted(size: tuple[int, int], box: int = 768) -> tuple[int, int]:
    """What a server that fits every image into a ``box`` × ``box`` square
    (keeping the aspect ratio) hands the model."""
    w, h = size
    scale = min(1.0, box / max(w, h))
    return round(w * scale), round(h * scale)


class SizeModel:
    """Fake vision model whose answer depends on the image it receives.

    ``server_box`` simulates the model server's resize: the model then only
    ever "sees" the fitted size, so two images that fit to the same size get
    the same answer (the production failure). Records what each call got."""

    def __init__(self, *, server_box: int | None = None):
        self.server_box = server_box
        self.calls: list[dict] = []
        self.whole: tuple[int, int] | None = None  # first image seen = pass 1

    def __call__(self, system, content, **kw):
        png = _image(content)
        sent = _size(png)
        seen = _fitted(sent, self.server_box) if self.server_box else sent
        self.calls.append({"sent": sent, "seen": seen, "text": content[0]["text"]})
        if self.whole is None:
            self.whole = seen
        info = kw.get("info")
        if info is not None:
            info.update(finish_reason="stop", prompt_tokens=seen[0] * seen[1] // 784,
                        completion_tokens=50, reasoning_tokens=0)  # fmt: skip
        if seen == self.whole:  # the whole page as pass 1 saw it: the misread digit
            return f"{TOP_LINE}\n{BOTTOM_LINE}"
        if "top half" in content[0]["text"]:  # a different image: the digit read right
            return "Leukocyty 6,87 G/l 4,0 - 11"
        if "bottom half" in content[0]["text"]:
            return BOTTOM_LINE
        return "Leukocyty 6,87 G/l 4,0 - 11\n" + BOTTOM_LINE  # a sharper whole page


@pytest.fixture
def tiles(monkeypatch):
    def set_(n: int):
        monkeypatch.setattr(config, "REOCR_SECOND_PASS_TILES", n)

    set_(2)  # the production default
    return set_


# ── (a) the agreement guard catches a disagreement ──────────────────


def test_guard_marks_digits_the_second_pass_reads_differently(monkeypatch, tiles):
    model = SizeModel()
    monkeypatch.setattr(llm, "chat", model)
    rr = reocr.reread("scan.pdf", _scan())
    top, bottom = rr.pages[0].splitlines()
    assert top == f"{TOP_LINE} [?]"  # 6,37 is not in pass 2: not accepted
    assert bottom == BOTTOM_LINE  # every number confirmed by the bottom half
    assert rr.flagged_lines == 1


def test_whole_page_rollback_still_compares_when_inputs_differ(monkeypatch, tiles):
    """With ``REOCR_SECOND_PASS_TILES=1`` the guard itself works: a model
    that really sees a different image is caught."""
    tiles(1)
    monkeypatch.setattr(llm, "chat", SizeModel())
    rr = reocr.reread("scan.pdf", _scan())
    assert rr.flagged_lines == 1
    assert rr.pages[0].splitlines()[0].endswith("[?]")


def test_a_fixed_size_server_made_the_whole_page_pass_a_copy(monkeypatch, tiles):
    """The production failure, reproduced: the server fits 150 and 220 dpi
    pages to the same size, pass 2 repeats pass 1 and nothing is flagged."""
    tiles(1)
    model = SizeModel(server_box=768)
    monkeypatch.setattr(llm, "chat", model)
    rr = reocr.reread("scan.pdf", _scan())
    first, second = model.calls
    assert first["sent"] != second["sent"]  # our side: two different images
    assert first["seen"] == second["seen"]  # the model's side: one image
    p1, p2 = rr.llm_passes
    assert p1["prompt_tokens"] == p2["prompt_tokens"]  # as observed in production
    assert rr.flagged_lines == 0  # the misread digit passed unflagged


def test_halves_stay_independent_behind_a_fixed_size_server(monkeypatch, tiles):
    model = SizeModel(server_box=768)
    monkeypatch.setattr(llm, "chat", model)
    rr = reocr.reread("scan.pdf", _scan())
    whole, top, bottom = (c["seen"] for c in model.calls)
    assert top != whole and bottom != whole  # the model sees different images
    assert rr.flagged_lines == 1
    assert rr.pages[0].splitlines()[0].endswith("[?]")
    whole_rec, top_rec, bottom_rec = rr.llm_passes
    assert top_rec["prompt_tokens"] != whole_rec["prompt_tokens"]
    assert bottom_rec["prompt_tokens"] != whole_rec["prompt_tokens"]


# ── (b) what each pass sends ──────────────────────────────────────────


def test_each_pass_sends_its_own_image_and_prompt(monkeypatch, tiles):
    model = SizeModel()
    monkeypatch.setattr(llm, "chat", model)
    rr = reocr.reread("scan.pdf", _scan())
    whole, top, bottom = model.calls
    first_dpi, second_dpi = reocr.reocr_dpis(2)
    page = pymupdf.open(stream=_scan(), filetype="pdf")[0]
    whole_size = _size(extract._pixmap_png(page, first_dpi))
    tall_w, tall_h = _size(extract._pixmap_png(page, second_dpi))
    assert whole["sent"] == whole_size
    assert top["sent"][0] == bottom["sent"][0] == tall_w  # full second-dpi width
    top_h, bottom_h = top["sent"][1], bottom["sent"][1]
    assert top_h + bottom_h > tall_h > max(top_h, bottom_h)
    assert len({c["text"] for c in model.calls}) == len(CALLS)  # different token prefixes
    n = 1
    assert whole["text"] == f"Page {n} of {n}."  # pass one's request is unchanged
    recs = rr.llm_passes
    assert [(r["width"], r["height"]) for r in recs] == [c["sent"] for c in model.calls]
    assert len({r["image_sha"] for r in recs}) == 3
    assert all(len(r["image_sha"]) == 12 for r in recs)


def test_rollback_sends_the_whole_page_twice_with_the_old_prompt(monkeypatch, tiles):
    tiles(1)
    model = SizeModel()
    monkeypatch.setattr(llm, "chat", model)
    rr = reocr.reread("scan.pdf", _scan())
    n = 1
    assert [c["text"] for c in model.calls] == [f"Page {n} of {n}."] * 2
    assert [r["pass"] for r in rr.llm_passes] == [1, 2]
    assert not any("tile" in r for r in rr.llm_passes)
    assert rr.llm_passes[0]["image_sha"] != rr.llm_passes[1]["image_sha"]


def test_halves_cover_the_page_with_an_overlap():
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=300)
    for y in range(20, 300, 20):
        page.insert_text((20, y), f"Riadok {y} 9,{y}")
    png = page.get_pixmap().tobytes("png")
    doc.close()
    full = pymupdf.Pixmap(png)
    top, bottom = (pymupdf.Pixmap(p) for p in reocr.split_png(png, 2))
    assert top.width == bottom.width == full.width
    overlap = top.height + bottom.height - full.height
    assert overlap >= int(full.height * reocr.TILE_OVERLAP)
    stride = full.stride
    # the top half is the image's first rows, the bottom half its last rows
    assert top.samples == full.samples[: top.height * stride]
    assert bottom.samples == full.samples[(full.height - bottom.height) * stride :]
    assert reocr.split_png(png, 1) == [png]


def test_image_meta_is_counts_only():
    png = pymupdf.open().new_page(width=50, height=40).get_pixmap().tobytes("png")
    meta = reocr.image_meta(png)
    assert (meta["width"], meta["height"]) == _size(png)
    assert set(meta) == {"width", "height", "image_sha"} and len(meta["image_sha"]) == 12
    assert reocr.image_meta(b"not a png")["width"] is None


def test_parallel_passes_send_both_halves(monkeypatch, tiles):
    model = SizeModel()
    monkeypatch.setattr(llm, "chat", model)
    usage: list[dict] = []
    passes = reocr._vision_passes(_scan(2), "pdf", reocr.reocr_dpis(2), client=object(),
                                  parallel=2, usage=usage)  # fmt: skip
    assert [(r["page"], r["pass"], r.get("tile")) for r in usage] == [
        (p, k, t) for p in (1, 2) for k, t in CALLS
    ]
    assert passes[1][0] == "Leukocyty 6,87 G/l 4,0 - 11\n" + BOTTOM_LINE


# ── (c) the orientation call's own record (#882) ──────────────────────


def _orientation_chat(answer: str | None, *, raise_=False):
    def chat(system, content, **kw):
        info = kw.get("info")
        if system == orientation.ORIENTATION_PROMPT:
            if info is not None:
                info.update(finish_reason="stop", prompt_tokens=321, completion_tokens=12,
                            reasoning_tokens=0, raw_content=answer or "")  # fmt: skip
            if raise_:
                raise llm.LocalLLMError("local LLM unreachable: ConnectError")
            return answer
        if info is not None:
            info.update(finish_reason="stop", prompt_tokens=900, completion_tokens=40)
        return TOP_LINE

    return chat


@pytest.mark.parametrize(
    "answer,raise_,valid,confident,error",
    [
        (json.dumps({"orientation": "upright", "confident": True}), False, True, True, None),
        (json.dumps({"orientation": "upright", "confident": False}), False, True, False, None),
        ("upright, I think", False, False, False, None),
        (None, True, False, False, "LocalLLMError"),
    ],
)
def test_orientation_call_is_recorded_counts_only(monkeypatch, tiles, answer, raise_, valid,
                                                  confident, error):  # fmt: skip
    # a portrait A4 page: only "always" asks the model (#882 pre-filter)
    monkeypatch.setattr(config, "OCR_ORIENTATION", "always")
    monkeypatch.setattr(llm, "chat", _orientation_chat(answer, raise_=raise_))
    rr = reocr.reread("scan.pdf", _scan())
    [rec] = [r for r in rr.llm_passes if r.get("kind") == "orientation"]
    assert (rec["page"], rec["pass"], rec["valid"], rec["confident"]) == (1, 0, valid,
                                                                          confident)  # fmt: skip
    assert rec.get("error") == error
    assert rec["prompt_tokens"] == 321 and rec["completion_tokens"] == 12
    assert isinstance(rec["seconds"], float) and rec["width"] and rec["height"]
    assert set(rec) <= {"page", "pass", "kind", "seconds", "prompt_tokens",
                        "completion_tokens", "reasoning_tokens", "finish", "valid",
                        "confident", "width", "height", "error"}  # fmt: skip
    assert "upright" not in json.dumps(rr.llm_passes)  # never the answer
    summary = reocr.usage_summary(rr.llm_passes)
    assert summary["completion_tokens_max"] == 40  # transcription passes only


# ── (d) only pass 1 is ever written; the halves only confirm digits ──

OVERLAP_LINE = "Lab 4.10.2027: WBC 5,10; HGB 128"  # a lab line inside both halves (inline format)
PASS_ONE_TEXT = f"Leukocyty 6,37 G/l 4,0 - 11\n{OVERLAP_LINE}\nTrombocyty 257 G/l 150 - 400"
HALF_MARK = "SYNTHETIC-HALF-ONLY"  # text that only the pass-2 halves ever answer


def _halves_chat(bottom_platelets: str):
    """Pass 1 reads the whole page; both halves read the overlap line (it lies
    inside the shared strip), each half adds a marker line of its own."""

    def chat(system, content, **kw):
        text = content[0]["text"]
        info = kw.get("info")
        if info is not None:
            info.update(finish_reason="stop", prompt_tokens=900, completion_tokens=60)
        if "top half" in text:
            return f"Leukocyty 6,37 G/l 4,0 - 11\n{OVERLAP_LINE}\n{HALF_MARK}"
        if "bottom half" in text:
            return (f"{OVERLAP_LINE}\n"
                    f"Trombocyty {bottom_platelets} G/l 150 - 400\n{HALF_MARK}")  # fmt: skip
        return PASS_ONE_TEXT

    return chat


@pytest.fixture
async def lab_vocab(db):
    from oncofiles.curated.facts_rules import Vocabulary

    return Vocabulary.from_rows(await db.fact_code_vocabulary())


@pytest.mark.parametrize("bottom_platelets", ["257", "251"])  # halves agree / disagree
@pytest.mark.parametrize("review", [False, True])  # rewrite body / review candidate
async def test_written_text_is_pass_one_never_the_joined_halves(
    lab_vocab, monkeypatch, tiles, bottom_platelets, review
):
    """Rewrite body and review candidate both hold the reconciled pass-1 text;
    a lab line inside the halves' overlap is written and parsed exactly once."""
    from oncofiles.curated.facts_rules import extract_lab_facts
    from oncofiles.curated.indexer import parse_sidecar
    from tests.test_curated_reocr import NOW, _setup
    from tests.test_curated_runner import LOGS

    drive, doc, side = _setup()
    doc = {**doc, "gdrive_id": drive.add("scan.pdf", drive.root, _scan())}
    monkeypatch.setattr(llm, "chat", _halves_chat(bottom_platelets))
    if review:  # a blocking reason: the new text is kept as a candidate instead
        monkeypatch.setattr(reocr, "review_reasons", lambda *a, **k: ["text_shrunk"])
    rep = reocr.run_reocr(drive, drive.root, [doc], lab_vocab, dry_run=False, now=NOW,
                          reader=reocr.reread)  # fmt: skip
    d = rep.documents[0]
    agree = bottom_platelets == "257"
    expected = PASS_ONE_TEXT if agree else PASS_ONE_TEXT + " [?]"
    if not review:
        assert d.result == "rewritten"
        written = drive.items[side]["content"].decode()
    else:
        assert d.result == "review"
        names = [n for n in (drive.items[i].get("name", "") for i in drive.items)
                 if n.startswith(reocr.CANDIDATE_PREFIX)]  # fmt: skip
        [name] = names
        written = drive.items[drive.find(drive.folder[LOGS], name)]["content"].decode()
    _, pages = parse_sidecar(reocr.candidate_sidecar_text(written) or written)
    assert pages == [expected]
    assert HALF_MARK not in written
    assert written.count(OVERLAP_LINE) == 1
    facts, _ = extract_lab_facts(pages, lab_vocab, report_date="2027-10-08", inline=True)
    assert [f["code"] for f in facts].count("HGB") == 1  # the overlap line: one fact


def test_ingest_orientation_request_is_unchanged(monkeypatch):
    """Without ``record`` (ingest) no ``info`` is passed: same request as before."""
    seen: list[dict] = []

    def chat(system, content, **kw):
        seen.append(kw)
        return json.dumps({"orientation": "upright", "confident": True})

    monkeypatch.setattr(llm, "chat", chat)
    png = pymupdf.open().new_page(width=50, height=40).get_pixmap().tobytes("png")
    guess = orientation.ask_rotation(png)
    assert guess.rotation == 0 and guess.confident and "info" not in seen[0]
