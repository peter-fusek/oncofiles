"""#593 re-read A/B knobs: REOCR_DPI and adaptive second passes.

Synthetic data only (invented values, dates 2027+); the model is mocked.
The invariant under test: adaptive mode never confirms a value that the
fixed two-pass rule would not confirm.
"""

from __future__ import annotations

import random

import pytest

from oncofiles import config
from oncofiles.curated import extract as extract_mod
from oncofiles.curated import llm, reocr
from oncofiles.curated import reread_verify as rv
from oncofiles.curated.facts_rules import (
    Vocabulary,
    extract_lab_facts,
    parse_bory,
    parse_markdown_table,
    parse_okb,
)
from tests.test_curated_reocr import _scan_pdf

COWORK = {"unverified_reasons": ["cowork_ocr"]}
DAY = "2027-05-11"


@pytest.fixture
async def vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


@pytest.fixture
def model(monkeypatch):
    """A fake local model: the answer is looked up by (page, dpi) in ``script``."""
    state = {"script": {}, "calls": []}

    def fake_chat(system, content, **kw):
        page = int(content[0]["text"].split()[1])
        dpi = content[1]["dpi"]
        state["calls"].append((page, dpi))
        return state["script"][(page, dpi)]

    monkeypatch.setattr(extract_mod, "_pixmap_png", lambda page, dpi=None: f"{dpi}".encode())
    monkeypatch.setattr(llm, "image_part", lambda png: {"type": "image", "dpi": int(png)})
    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "model_id", lambda: "qwen-local")
    return state


def _script(first: list[str], second: list[str], dpis=(150, 220)) -> dict:
    out = {}
    for i, (a, b) in enumerate(zip(first, second, strict=True), start=1):
        out[(i, dpis[0])], out[(i, dpis[1])] = a, b
    return out


def _old(i, code, value, page):
    return {"id": i, "code": code, "value_canonical": value, "occurred_at": DAY,
            "page_number": page, "meta": dict(COWORK), "verification": "unverified"}  # fmt: skip


def _confirmed(rr, olds, vocab, old_page_count) -> list[int]:
    new, _ = extract_lab_facts(rr.pages, vocab, report_date=DAY, inline=True)
    return rv.assess(olds, new, rr.pages, old_page_count=old_page_count)["confirmed"]


# ── defaults: byte-identical ──


def test_env_defaults_are_todays_values():
    assert reocr.reocr_dpis(2) == [extract_mod.RENDER_DPI, 220] == [150, 220]
    assert reocr.reocr_dpis(2, "reread") == [150, 220]
    assert reocr.reocr_dpis(1) == [150]
    with pytest.raises(ValueError):
        reocr.reocr_dpis(2, "other")
    assert config.REOCR_PASSES_MODE == "fixed2"


def test_defaults_read_every_page_twice_in_the_old_order(model):
    first = ["Lab 11.5.2027: WBC 6,12; PLT 233", "Bez nálezu.", "HGB 120"]
    second = ["Lab 11.5.2027: WBC 6,72; PLT 233", "Bez nálezu.", "HGB 120"]
    model["script"] = _script(first, second)
    rr = reocr.reread("scan.pdf", _scan_pdf(3), second_pass_pages={1})  # ignored unless adaptive
    assert model["calls"] == [(1, 150), (1, 220), (2, 150), (2, 220), (3, 150), (3, 220)]
    expected, flagged = reocr.reconcile(first, second)  # the pre-#593 computation
    assert rr.pages == expected and rr.flagged_lines == flagged == 1
    assert rr.method == "vision2" and rr.dpi == [150, 220]
    assert rr.passes_per_page == [2] * 3 and rr.skipped_second_pass_pages == []


def test_reconcile_passes_a_skipped_page_through_unchanged():
    pages, flagged = reocr.reconcile(["WBC 6,12\nPLT 233", "HGB 120"], [None, "HGB 121"])
    assert pages == ["WBC 6,12\nPLT 233", "HGB 120 [?]"] and flagged == 1


# ── REOCR_DPI reaches the renderer ──


def test_dpi_settings_reach_the_renderer(model, monkeypatch):
    rendered = []
    monkeypatch.setattr(
        extract_mod, "_pixmap_png", lambda page, dpi=None: rendered.append(dpi) or f"{dpi}".encode()
    )
    monkeypatch.setattr(config, "REOCR_DPI", 110)
    monkeypatch.setattr(config, "REOCR_SECOND_PASS_DPI", 160)
    model["script"] = _script(["WBC 6,12"] * 2, ["WBC 6,12"] * 2, dpis=(110, 160))
    rr = reocr.reread("scan.pdf", _scan_pdf(2))
    assert rendered == [110, 160, 110, 160] and rr.dpi == [110, 160]


def _rendered(model, monkeypatch, purpose: str) -> list:
    rendered = []
    monkeypatch.setattr(
        extract_mod, "_pixmap_png", lambda page, dpi=None: rendered.append(dpi) or f"{dpi}".encode()
    )
    model["script"] = {(1, d): "WBC 6,12" for d in (100, 130, 150, 220)}
    reocr.reread("scan.pdf", _scan_pdf(1), purpose=purpose)
    return rendered


def test_reread_dpi_never_changes_reocr_rendering(model, monkeypatch):
    """The re-OCR rewrite path writes its transcript to Drive: the verify-only
    A/B knob (REREAD_*) must never reach it, and vice versa."""
    monkeypatch.setattr(config, "REREAD_DPI", 100)
    monkeypatch.setattr(config, "REREAD_SECOND_PASS_DPI", 130)
    assert _rendered(model, monkeypatch, "reocr") == [150, 220]
    assert _rendered(model, monkeypatch, "reread") == [100, 130]


def test_reocr_dpi_never_changes_reread_rendering(model, monkeypatch):
    monkeypatch.setattr(config, "REOCR_DPI", 100)
    monkeypatch.setattr(config, "REOCR_SECOND_PASS_DPI", 130)
    assert _rendered(model, monkeypatch, "reread") == [150, 220]
    assert _rendered(model, monkeypatch, "reocr") == [100, 130]


def test_the_purpose_reaches_the_child(monkeypatch):
    import json
    import subprocess

    from oncofiles.curated.reocr import Reread

    sent = []

    def fake_run(cmd, input, **kw):  # noqa: A002
        sent.append(json.loads(input.split(b"\n", 1)[0]))
        out = json.dumps({**Reread(["x"], "vision2", "qwen-local").__dict__, "peak_rss_mb": 1})
        return subprocess.CompletedProcess(cmd, 0, out.encode(), b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    reocr.reread_isolated("scan.pdf", b"%PDF", purpose="reread")
    reocr.reread_isolated("scan.pdf", b"%PDF")
    assert [h["purpose"] for h in sent] == ["reread", "reocr"]


def test_the_verify_only_reread_asks_for_reread_dpis():
    import inspect

    from oncofiles.tools import curated as tool

    assert 'purpose="reread"' in inspect.getsource(tool._reread_one)


async def test_the_rewrite_path_reads_with_reocr_dpis(vocab):
    """run_reocr calls its reader without a purpose: the "reocr" default."""
    from datetime import UTC, datetime

    from tests.test_curated_reocr import _setup

    drive, doc, _ = _setup()
    seen = []

    def reader(*a, **kw):
        seen.append(kw)
        return reocr.Reread([], "none", "-", error="stop here")

    reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=True, reader=reader,
                    now=datetime(2027, 5, 3, tzinfo=UTC))  # fmt: skip
    assert seen and all(kw.get("purpose", "reocr") == "reocr" for kw in seen)


def test_the_real_renderer_scales_with_the_dpi():
    import pymupdf

    with pymupdf.open(stream=_scan_pdf(1), filetype="pdf") as doc:
        small = pymupdf.Pixmap(extract_mod._pixmap_png(doc[0], 100))
        large = pymupdf.Pixmap(extract_mod._pixmap_png(doc[0], 200))
    assert large.width == pytest.approx(2 * small.width, abs=2)


def test_the_child_inherits_the_dpi_and_mode_settings():
    from oncofiles.curated.reocr_child import child_env

    knobs = {"REOCR_DPI": "160", "REOCR_SECOND_PASS_DPI": "230", "REREAD_DPI": "120",
             "REREAD_SECOND_PASS_DPI": "180", "REOCR_PASSES_MODE": "adaptive"}  # fmt: skip
    assert child_env({**knobs, "MCP_BEARER_TOKEN": "x"}) == knobs


# ── adaptive ──


def test_adaptive_skips_only_a_page_without_digits(model, monkeypatch):
    monkeypatch.setattr(config, "REOCR_PASSES_MODE", "adaptive")
    first = ["Lab 11.5.2027: WBC 6,12; PLT 233", "Bez patologického nálezu.", "HGB 120"]
    second = ["Lab 11.5.2027: WBC 6,72; PLT 233", "Bez nálezu.", "HGB 121"]
    model["script"] = _script(first, second)
    rr = reocr.reread("scan.pdf", _scan_pdf(3))
    assert (2, 220) not in model["calls"]
    assert "".join(map(str, rr.passes_per_page)) == "212"
    assert rr.skipped_second_pass_pages == [2]
    assert (rr.pages, rr.flagged_lines) == reocr.reconcile(first, second)  # same text


def test_adaptive_in_parallel_mode_keeps_both_passes_of_digit_free_pages(model, monkeypatch):
    """Deciding after pass 1 would serialise every page; parallel keeps fixed2."""
    monkeypatch.setattr(config, "REOCR_PASSES_MODE", "adaptive")
    model["script"] = _script(["Bez nálezu."], ["Bez nálezu."])
    rr = reocr.reread("scan.pdf", _scan_pdf(1), parallel=2)
    assert sorted(model["calls"]) == [(1, 150), (1, 220)] and rr.passes_per_page == [2]


def test_pages_with_candidates():
    olds = [_old(1, "WBC", 6.12, 2), _old(2, "PLT", 233.0, 3)]
    pages = 3
    assert rv.pages_with_candidates(olds, old_page_count=pages) == {2, pages}
    assert rv.pages_with_candidates(olds, old_page_count=1) is None  # unpaged: any page
    unknown = _old(3, "HGB", 120.0, None)
    assert rv.pages_with_candidates([*olds, unknown], old_page_count=pages) is None
    machine = {**_old(4, "HGB", 120.0, 1), "meta": {}}  # not a candidate
    assert rv.pages_with_candidates([*olds, machine], old_page_count=pages) == {2, pages}


async def test_a_page_without_candidates_is_read_once(model, monkeypatch, vocab):
    monkeypatch.setattr(config, "REOCR_PASSES_MODE", "adaptive")
    first = ["Prepúšťacia správa, strana 7 z 9.", "Lab 11.5.2027: WBC 6,12; PLT 233"]
    second = ["Prepúšťacia správa, strana 1 z 9.", "Lab 11.5.2027: WBC 6,12; PLT 233"]
    model["script"] = _script(first, second)
    olds = [_old(1, "WBC", 6.12, 2), _old(2, "PLT", 233.0, 2)]
    wanted = rv.pages_with_candidates(olds, old_page_count=2)
    rr = reocr.reread("scan.pdf", _scan_pdf(2), second_pass_pages=wanted)
    assert rr.skipped_second_pass_pages == [1] and (1, 220) not in model["calls"]
    assert _confirmed(rr, olds, vocab, len(first)) == [o["id"] for o in olds]


async def test_a_page_sharing_a_number_with_a_confirmable_page_is_read_twice(
    model, monkeypatch, vocab
):
    """The inline parser drops a value already seen on an earlier page, so a
    page read once could change what a later page yields: it is read twice."""
    monkeypatch.setattr(config, "REOCR_PASSES_MODE", "adaptive")
    first = ["Lab 11.5.2027: WBC 6,12; PLT 233", "Lab 11.5.2027: WBC 6,12; PLT 233"]
    second = ["Lab 11.5.2027: WBC 6,72; PLT 233", "Lab 11.5.2027: WBC 6,12; PLT 233"]
    model["script"] = _script(first, second)
    olds = [_old(1, "WBC", 6.12, 2), _old(2, "PLT", 233.0, 2)]
    rr = reocr.reread("scan.pdf", _scan_pdf(2), second_pass_pages={2})
    assert (1, 220) in model["calls"] and rr.skipped_second_pass_pages == []
    assert (rr.pages, rr.flagged_lines) == reocr.reconcile(first, second)
    monkeypatch.setattr(config, "REOCR_PASSES_MODE", "fixed2")
    fixed = reocr.reread("scan.pdf", _scan_pdf(2))
    assert _confirmed(rr, olds, vocab, 2) == _confirmed(fixed, olds, vocab, 2)


_VALUES = ["6,12", "6,72", "233", "120", "4,1", "11"]


def _page(rng: random.Random) -> str:
    kind = rng.choice(["lab", "lab", "prose", "prose_digits", "okb"])
    if kind == "lab":
        a, b, c = (rng.choice(_VALUES) for _ in range(3))
        return f"Lab 11.5.2027: WBC {a}; PLT {b}; HGB {c}"
    if kind == "okb":
        return (
            "Odobrané: 11.05.2027 07:10\n"
            "Test                                Hodnota    Jednotky    Ref. medze\n"
            f"CEA v sére                          {rng.choice(_VALUES)}       ng/ml     0.0 - 5.0\n"
        )
    if kind == "prose_digits":
        return f"Kontrola o {rng.choice(_VALUES)} týždňov."
    return "Bez patologického nálezu."


def _perturb(text: str, rng: random.Random) -> str:
    if rng.random() < 0.5:
        return text
    for v in _VALUES:
        if v in text:
            return text.replace(v, rng.choice(_VALUES), 1)
    return text


async def test_adaptive_never_confirms_what_fixed2_would_not(model, monkeypatch, vocab):
    """Randomised: same model answers, both modes, both parallel settings."""
    rng = random.Random(593)
    skipped_total = 0
    confirmed_total = 0
    for _ in range(300):
        n = rng.randint(2, 4)
        first = [_page(rng) for _ in range(n)]
        second = [_perturb(p, rng) for p in first]
        model["script"] = _script(first, second)
        cand_pages = [p + 1 for p in rng.sample(range(n), rng.randint(1, n))]
        olds = []
        for page in cand_pages:
            for code in ("WBC", "PLT", "HGB", "CEA"):
                if rng.random() < 0.6:
                    on_page = [v for v in _VALUES if v in first[page - 1]]
                    v = rng.choice(on_page if on_page and rng.random() < 0.8 else _VALUES)
                    olds.append(_old(len(olds) + 1, code, float(v.replace(",", ".")), page))
        if not olds:
            continue
        wanted = rv.pages_with_candidates(olds, old_page_count=n)
        parallel = rng.choice([1, 2])
        monkeypatch.setattr(config, "REOCR_PASSES_MODE", "fixed2")
        fixed = reocr.reread("scan.pdf", _scan_pdf(n), parallel=parallel)
        monkeypatch.setattr(config, "REOCR_PASSES_MODE", "adaptive")
        adapt = reocr.reread("scan.pdf", _scan_pdf(n), parallel=parallel,
                             second_pass_pages=wanted)  # fmt: skip
        skipped_total += len(adapt.skipped_second_pass_pages)
        got_fixed = set(_confirmed(fixed, olds, vocab, n))
        got_adapt = set(_confirmed(adapt, olds, vocab, n))
        assert got_adapt <= got_fixed, (first, second, olds)
        assert got_adapt == got_fixed  # exact: nothing lost either
        confirmed_total += len(got_fixed)
        for p in adapt.skipped_second_pass_pages:  # a page read once confirms nothing
            assert p not in (wanted or set()) or not reocr._NUM.search(first[p - 1])
    assert skipped_total > 0 and confirmed_total > 50  # both paths are exercised


async def test_rewrite_path_ignores_page_selection(model, monkeypatch):
    """run_reocr writes the text back: only the exact no-digit skip applies."""
    monkeypatch.setattr(config, "REOCR_PASSES_MODE", "adaptive")
    model["script"] = _script(["WBC 6,12", "HGB 120"], ["WBC 6,72", "HGB 120"])
    rr = reocr.reread("scan.pdf", _scan_pdf(2))  # run_reocr never passes pages
    assert rr.skipped_second_pass_pages == [] and rr.pages[0].endswith("[?]")


# ── the parser property the deferral guard relies on ──

_BORY = (
    "Odber: 11.05.2027 07:10,\n"
    "Krvný obraz: Leukocyty [WBC] ...7.31 ▲  [10^9/l] {3.80 - 10.70 } ; "
    "Hemoglobín [HGB] ...118.0 ▼  [g/l] {120.0 - 155.0 }\n"
    "Moč: U- pH ...6.0 {5.5 - 6.5 } ; U- Krv ...NEGAT [arb]\n"
)
_OKB = (
    "Odobrané: 11.05.2027 07:10\n"
    "Test                                Hodnota    Jednotky    Ref. medze\n"
    "CEA v sére                          3.9        ng/ml     0.0 - 5.0\n"
    "Kreatínin v sére                    61.2       µmol/l    44.0 - 71.0\n"
)
_MD = (
    "Odobrané: 11.05.2027 07:10\n| Test | Hodnota | Jednotky | Ref. medze |\n|---|---|---|---|\n"
    "| WBC | 5.02 | G/l | 3.5 - 10 |\n| HGB | 131 | g/l | 120 - 156 |\n"
)


def _marked(text: str) -> str:
    return "\n".join(f"{ln} [?]" if reocr._NUM.search(ln) else ln for ln in text.splitlines())


async def test_markers_never_create_table_rows(vocab):
    for parser, text in ((parse_bory, _BORY), (parse_okb, _OKB)):
        plain, marked = parser([text], vocab), parser([_marked(text)], vocab)
        assert plain and len(marked) == len(plain), parser.__name__
    plain, marked = parse_markdown_table([_MD], vocab), parse_markdown_table([_marked(_MD)], vocab)
    assert plain and len(marked) <= len(plain)
