"""Unit tests for the curated pipeline's LLM client, extraction and classification (#562).

Synthetic data only. The local model is mocked at the HTTP transport.
"""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from oncofiles import config
from oncofiles.curated import classify, extract, llm
from oncofiles.curated.contract import parse_contract

CONTRACT = parse_contract(
    (Path(__file__).parent / "fixtures" / "curated" / "README_oncology.md").read_text()
)
TEXT = "LEKÁRSKA SPRÁVA\nNález zo dňa 05.08.2026\nMUDr. Jana Kováčová\nNOU"
GOOD = {
    "doc_date": "2026-08-05",
    "doc_type": "Report",
    "institution": "NOU",
    "doctor": "MUDr. Jana Kováčová",
    "hospital": "KlinikaKlinickejOnkologie",
    "multiple_documents": False,
    "confidence": 0.9,
}


@pytest.fixture
def local_llm(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-vision-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "k")


def _client(answer: str, status: int = 200, seen: list | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={"choices": [{"message": {"content": answer}}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


# ── llm ──────────────────────────────────────────────────────────────


def test_chat_strips_think_and_sends_no_think(local_llm):
    seen: list = []
    out = llm.chat("sys", "hi", client=_client("<think>hmm</think>\nanswer", seen=seen))
    assert out == "answer"
    body = json.loads(seen[0].content)
    assert body["model"] == "test-vision-model"
    assert body["messages"][0]["content"].endswith("/no_think")
    assert seen[0].headers["authorization"] == "Bearer k"


def test_chat_errors_are_typed(local_llm):
    with pytest.raises(llm.LocalLLMError, match="HTTP 500"):
        llm.chat("s", "u", client=_client("x", status=500))


def test_chat_requires_config(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "")
    with pytest.raises(llm.LocalLLMError, match="not set"):
        llm.chat("s", "u")


@pytest.mark.parametrize(
    "answer",
    ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! {"a": 1} done', '<think>x</think>{"a":1}'],
)
def test_parse_json_object(answer):
    assert llm.parse_json_object(answer) == {"a": 1}


def test_parse_json_object_rejects_garbage():
    with pytest.raises(llm.LocalLLMError):
        llm.parse_json_object("no json here")


# ── extract ──────────────────────────────────────────────────────────


def _pdf(text: str) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_text_layer_pdf_needs_no_llm(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: pytest.fail("LLM must not be called"))
    ex = extract.extract("a.pdf", _pdf("Lekarska sprava " * 20))
    assert ex.ok and ex.method == "text_layer" and ex.pages == 1


def test_scanned_pdf_goes_to_local_vision(monkeypatch, local_llm):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "[handwritten]\nDekurz 05.08.2026")
    ex = extract.extract("scan.pdf", _pdf(""))
    assert ex.ok and ex.method == "vision" and ex.handwritten
    assert ex.engine == "test-vision-model"
    assert "Dekurz 05.08.2026" in ex.text


def test_min_text_chars_threshold(monkeypatch, local_llm):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "ocr")
    assert extract.extract("a.pdf", _pdf("x" * 150), min_text_chars=200).method == "vision"


def test_llm_failure_is_reported_not_raised(monkeypatch, local_llm):
    def boom(*a, **k):
        raise llm.LocalLLMError("local LLM unreachable: ConnectError")

    monkeypatch.setattr(llm, "chat", boom)
    ex = extract.extract("scan.pdf", _pdf(""))
    assert not ex.ok and "unreachable" in ex.error


def test_plain_text_and_unsupported():
    assert extract.extract("n.md", "# poznámka".encode()).method == "plain"
    assert not extract.extract("x.zip", b"PK").ok
    assert extract.extract("d", b"", "application/vnd.google-apps.document").method == "none"


def test_corrupt_pdf_is_reported():
    ex = extract.extract("bad.pdf", b"not a pdf")
    assert not ex.ok


# ── classify.validate ───────────────────────────────────────────────

TODAY = date(2026, 11, 7)


def test_good_classification_builds_fields():
    c = classify.validate(CONTRACT, GOOD, TEXT, today=TODAY)
    assert not c.review_needed, c.reasons
    assert c.fields.date == "20260805" and c.fields.patient == "JanaNovakova"
    assert c.fields.doctor == "Kovacova"
    assert any("first-time hospital" in f for f in c.flags)


@pytest.mark.parametrize(
    "patch,reason",
    [
        ({"doc_date": "1970-02-03"}, "birth date"),
        ({"doc_date": "2027-01-01"}, "future"),
        ({"doc_date": "2026-08-06"}, "does not appear"),
        ({"doc_date": "05.08.2026"}, "no valid clinical date"),
        ({"doc_date": None}, "no valid clinical date"),
        ({"doc_type": "Consultation"}, "not in contract"),
        ({"institution": None}, "institution unknown"),
        ({"multiple_documents": True}, "several documents"),
        ({"confidence": 0.3}, "confidence"),
    ],
)
def test_hard_rules_force_review(patch, reason):
    text = TEXT + "\n03.02.1970 01.01.2027" if "birth" in reason or "future" in reason else TEXT
    c = classify.validate(CONTRACT, {**GOOD, **patch}, text, today=TODAY)
    assert c.review_needed
    assert any(reason in r for r in c.reasons), c.reasons


def test_unknown_type_falls_back_to_other_for_the_sidecar():
    c = classify.validate(CONTRACT, {**GOOD, "doc_type": "Nonsense"}, TEXT, today=TODAY)
    assert c.doc_type == "Other" and c.review_needed


@pytest.mark.parametrize(
    "printed", ["05.08.2026", "5.8.2026", "05. 08. 2026", "2026-08-05", "05/08/2026", "05.08.26"]
)
def test_date_in_text_variants(printed):
    assert classify.date_in_text("2026-08-05", f"Dátum: {printed}")


def test_propose_uses_contract_and_returns_json(monkeypatch):
    captured = {}

    def fake_chat(system, user, **k):
        captured["system"], captured["user"] = system, user
        return json.dumps(GOOD)

    monkeypatch.setattr(llm, "chat", fake_chat)
    assert classify.propose(CONTRACT, TEXT)["doc_type"] == "Report"
    assert "JanaNovakova" in captured["system"] and "Labs" in captured["system"]
    assert captured["user"].startswith("TEXT:")


def test_text_layer_pdf_is_never_rendered(monkeypatch):
    monkeypatch.setattr(extract, "_render_pdf_pages", lambda c: pytest.fail("must not render"))
    assert extract.extract("a.pdf", _pdf("Lekarska sprava " * 20)).method == "text_layer"


def test_huge_page_is_rendered_under_pixel_cap():
    import pymupdf

    doc = pymupdf.open()
    doc.new_page(width=72 * 200, height=72 * 200)  # 200 x 200 inch page
    pix = pymupdf.Pixmap(extract._pixmap_png(doc[0]))
    assert pix.width * pix.height <= extract.MAX_PAGE_PIXELS


def test_oversized_file_refused_before_parsing():
    ex = extract.extract("big.pdf", b"x" * (extract.MAX_FILE_BYTES + 1))
    assert not ex.ok and "larger than" in ex.error


def test_absurd_page_size_gets_a_fractional_zoom_within_the_cap():
    seen = {}

    class Pix:
        def tobytes(self, fmt):
            return b"png"

    class Page:
        rect = type("R", (), {"width": 1e9, "height": 1e9})()

        def get_pixmap(self, matrix):
            seen["zoom"] = matrix.a
            return Pix()

    extract._pixmap_png(Page())
    px = (1e9 * seen["zoom"]) ** 2
    assert seen["zoom"] < 1e-4 and px <= extract.MAX_PAGE_PIXELS * 1.01


@pytest.mark.parametrize(
    "doctor,expected,snapped",
    [
        ("MUDr. Jana Kováčová", "Kovacova", False),  # exact
        ("MUDr. Jana Kovačova", "Kovacova", False),  # diacritics only
        ("MUDr. Jana Kovacsova", "Kovacova", True),  # OCR typo → snapped
        ("MUDr. Peter Zeleny", "Zeleny", False),  # genuinely new → kept, flagged first-time
    ],
)
def test_vocabulary_snap(doctor, expected, snapped):
    c = classify.validate(CONTRACT, {**GOOD, "doctor": doctor}, TEXT, today=TODAY)
    assert c.fields.doctor == expected
    assert any("snapped" in f for f in c.flags) is snapped


@pytest.mark.parametrize(
    "iso,printed",
    [
        ("2024-01-02", "Dátum: 12.1.2024"),  # 2.1.2024 inside 12.1.2024
        ("2020-01-02", "Dátum: 02.01.2024"),  # 02.01.20 inside 02.01.2024
        ("2026-08-05", "Č. 105.08.2026"),
    ],
)
def test_date_in_text_needs_digit_boundaries(iso, printed):
    """Review #3: a substring match must not validate a wrong year or day."""
    assert not classify.date_in_text(iso, printed)


# ── 2026-11-07 live finding: Qwen reasoning ate the whole token budget ──


def test_chat_sends_large_budget_and_low_reasoning(local_llm):
    seen: list = []
    llm.chat("s", "u", client=_client("{}", seen=seen))
    body = json.loads(seen[0].content)
    assert body["max_tokens"] >= 8000 and body["reasoning_effort"] == "low"


def test_chat_reports_truncation_by_reasoning(local_llm):
    def handler(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        )

    with pytest.raises(llm.LocalLLMError, match="truncated"):
        llm.chat("s", "u", client=httpx.Client(transport=httpx.MockTransport(handler)))
