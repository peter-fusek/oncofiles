"""Unit tests for the curated pipeline's LLM client, extraction and classification (#562).

Synthetic data only. The local model is mocked at the HTTP transport.
"""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from oncofiles import config
from oncofiles.curated import classify, extract, llm, runner
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
    assert extract.extract("f", b"", "application/vnd.google-apps.form").method == "none"


def test_google_native_export_is_read_as_plain_text():
    doc = extract.extract(
        "d.pdf", "\ufeffSpráva 05.08.2026".encode(), "application/vnd.google-apps.document"
    )
    assert doc.ok and doc.method == "plain" and doc.engine == "google-export"
    assert doc.text == "Správa 05.08.2026"  # BOM from Drive's export stripped
    sheet = extract.extract("s", b"a,b\n1,2", "application/vnd.google-apps.spreadsheet")
    assert sheet.text.startswith("a,b")


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


def test_propose_retries_once_with_double_budget_when_truncated(monkeypatch):
    calls = []

    def fake_chat(system, user, **k):
        calls.append(k)
        if len(calls) == 1:
            raise llm.LocalLLMError("local LLM answer truncated: reasoning used the whole budget")
        return json.dumps(GOOD)

    monkeypatch.setattr(llm, "chat", fake_chat)
    assert classify.propose(CONTRACT, TEXT)["doc_type"] == "Report"
    assert calls[0] == {} and calls[1]["max_tokens"] == 2 * llm.REASONING_BUDGET
    assert calls[1]["timeout"] > 0


def test_propose_does_not_retry_other_llm_errors(monkeypatch):
    calls = []

    def fake_chat(system, user, **k):
        calls.append(1)
        raise llm.LocalLLMError("local LLM HTTP 500")

    monkeypatch.setattr(llm, "chat", fake_chat)
    with pytest.raises(llm.LocalLLMError):
        classify.propose(CONTRACT, TEXT)
    assert len(calls) == 1


# ── #578: documents naming another family member ────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Pacient: Peter NOVÁK\nKontrolné vyšetrenie 26.05.2026", "Peter NOVÁK"),
        ("Adresa: Mgr. Tomáš Novák, Hlavná 1", "Tomáš Novák"),
        ("NOVÁK Michal, nar. 2010", "NOVÁK Michal"),
        ("Pacientka: Jana Nováková\nmanžel Peter Novák", None),  # patient named
        ("Kontrolné vyšetrenie bez mena 26.05.2026", None),  # nobody named
        ("Pacientka: NOVÁKOVÁ JANA", None),
    ],
)
def test_other_person_named(text, expected):
    assert classify.other_person_named("Jana Nováková", text) == expected


def test_document_of_another_person_goes_to_review():
    text = "Pacient: Peter NOVÁK\nLekárska správa 26.05.2026 MUDr. Jana Kováčová NOU"
    raw = {**GOOD, "doc_date": "2026-05-26"}
    cls = classify.validate(CONTRACT, raw, text, today=date(2026, 11, 8))
    assert cls.review_needed
    assert any("another person (Peter NOVÁK)" in r for r in cls.reasons)


def test_caregiver_material_may_name_the_caregiver(monkeypatch):
    # the synthetic contract has no Reference type; exercise the exemption itself
    monkeypatch.setattr(classify, "CAREGIVER_DOC_TYPES", {"Treatment"})
    text = "NCCN Guidelines, poznámky: Peter Novák, 26.05.2026"
    raw = {**GOOD, "doc_type": "Treatment", "doc_date": "2026-05-26"}
    cls = classify.validate(CONTRACT, raw, text, today=date(2026, 11, 8))
    assert not any("another person" in r for r in cls.reasons)


def test_chat_logs_token_usage_but_never_text(local_llm, caplog):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "SECRET ANSWER"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 812,
                    "completion_tokens": 340,
                    "completion_tokens_details": {"reasoning_tokens": 300},
                },
            },
        )

    caplog.set_level("INFO", logger="oncofiles.curated.llm")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm.chat("sys", "SECRET PROMPT", client=client, max_tokens=12000)
    line = next(r.getMessage() for r in caplog.records if "local llm call" in r.getMessage())
    assert "prompt_tokens=812" in line and "completion_tokens=340" in line
    assert "reasoning_tokens=300" in line and "max_tokens=12000" in line
    assert "finish=stop" in line
    assert "SECRET" not in caplog.text


# ── context window clamp (32K fixed in LM Studio) ──


def test_clamp_keeps_prompt_plus_budget_inside_the_window():
    assert llm.clamp_budget(12_000, 4_000) == 12_000
    assert (
        llm.clamp_budget(24_000, 8_007)
        == config.LOCAL_LLM_CONTEXT_WINDOW - 8_007 - llm.CONTEXT_MARGIN
    )
    assert llm.clamp_budget(24_000, 40_000) == 0


def test_estimate_counts_text_and_images():
    assert llm.estimate_prompt_tokens("a" * 300, "b" * 300) == 200
    parts = [{"type": "text", "text": "x" * 30}, {"type": "image_url"}, {"type": "image_url"}]
    assert llm.estimate_prompt_tokens("", parts) == 10 + 2 * llm.IMAGE_TOKENS


def test_chat_sends_clamped_max_tokens(local_llm):
    seen = []
    system = "s" * 3 * 20_000  # ~20K tokens of prompt
    llm.chat(system, "u", client=_client("ok", seen=seen), max_tokens=24_000)
    sent = json.loads(seen[0].content)["max_tokens"]
    assert sent + 20_000 + llm.CONTEXT_MARGIN <= config.LOCAL_LLM_CONTEXT_WINDOW + 1
    assert sent < 24_000


def test_chat_refuses_a_prompt_that_leaves_no_room(local_llm):
    seen = []
    with pytest.raises(llm.LocalLLMError, match="truncated: prompt"):
        llm.chat("s" * 3 * 31_000, "u", client=_client("ok", seen=seen))
    assert seen == []  # never sent


def test_propose_skips_the_retry_when_the_window_has_no_more_room(monkeypatch):
    calls = []

    def fake_chat(system, user, **k):
        calls.append(k)
        raise llm.LocalLLMError("local LLM answer truncated: reasoning used the whole budget")

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(
        config, "LOCAL_LLM_CONTEXT_WINDOW", 4_000
    )  # tiny window: 2x budget clamps to 1x
    with pytest.raises(llm.LocalLLMError):
        classify.propose(CONTRACT, TEXT)
    assert len(calls) == 1


def test_context_overflow_from_lm_studio_is_a_per_document_failure(local_llm):
    def handler(request):
        return httpx.Response(
            400,
            json={
                "error": "The number of tokens to keep from the initial prompt is "
                "greater than the context length"
            },
        )

    with pytest.raises(llm.LocalLLMError) as exc:
        llm.chat("s", "u", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "truncated" in str(exc.value)
    assert not runner._llm_down(str(exc.value))  # review path, the run continues


def test_other_http_errors_still_mean_the_model_is_down(local_llm):
    def handler(request):
        return httpx.Response(400, json={"error": "model not loaded"})

    with pytest.raises(llm.LocalLLMError) as exc:
        llm.chat("s", "u", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert runner._llm_down(str(exc.value))


def test_context_window_comes_from_config(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_CONTEXT_WINDOW", 65_536)
    assert llm.clamp_budget(24_000, 30_000) == 24_000


def test_propose_does_not_retry_a_context_overflow(monkeypatch):
    calls = []

    def fake_chat(system, user, **k):
        calls.append(k)
        raise llm.LocalLLMError("local LLM answer truncated: context overflow (HTTP 400)")

    monkeypatch.setattr(llm, "chat", fake_chat)
    with pytest.raises(llm.LocalLLMError):
        classify.propose(CONTRACT, TEXT)
    assert len(calls) == 1


# ── #689: Qwen /no_think soft switch ─────────────────────────────────


def _sent_user(seen: list) -> list | str:
    return json.loads(seen[-1].content)["messages"][-1]["content"]


def test_no_think_appended_once_to_text_when_thinking_off(local_llm):
    seen: list = []
    llm.chat("sys", "Transcribe.", client=_client("ok", seen=seen), thinking=False)
    assert _sent_user(seen) == "Transcribe. /no_think"
    body = json.loads(seen[-1].content)
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][0]["content"].endswith("\n/no_think")  # system keeps it
    # Already present -> never a second time.
    llm.chat("sys", "Transcribe. /no_think", client=_client("ok", seen=seen), thinking=False)
    assert _sent_user(seen).count("/no_think") == 1


def test_no_think_is_a_new_text_part_after_images(local_llm):
    seen: list = []
    img = llm.image_part(b"\x89PNG synthetic")
    instr = {"type": "text", "text": "Synthetic instruction for test page."}
    content = [instr, img]
    llm.chat("sys", content, client=_client("ok", seen=seen), thinking=False)
    sent = _sent_user(seen)
    assert sent[0] == instr  # instruction unchanged
    assert sent[1]["type"] == "image_url"
    assert sent[2] == {"type": "text", "text": " /no_think"}
    assert len(sent) == 3
    assert content == [instr, img]  # caller not mutated
    # Image-only content gets the text part too.
    llm.chat("sys", [img], client=_client("ok", seen=seen), thinking=False)
    assert _sent_user(seen)[-1] == {"type": "text", "text": " /no_think"}
    # Already carrying it -> never twice.
    llm.chat("sys", sent, client=_client("ok", seen=seen), thinking=False)
    assert sum("/no_think" in p.get("text", "") for p in _sent_user(seen)) == 1


@pytest.mark.parametrize("thinking", [True, None])
def test_no_think_never_appended_unless_thinking_off(local_llm, thinking):
    seen: list = []
    img = llm.image_part(b"\x89PNG synthetic")
    llm.chat("sys", "Transcribe.", client=_client("ok", seen=seen), thinking=thinking)
    assert _sent_user(seen) == "Transcribe."
    llm.chat("sys", [img], client=_client("ok", seen=seen), thinking=thinking)
    assert _sent_user(seen) == [img]


def test_no_think_soft_switch_env_off(local_llm, monkeypatch):
    monkeypatch.setattr(config, "LLM_NO_THINK_SOFT_SWITCH", False)
    seen: list = []
    llm.chat("sys", "Transcribe.", client=_client("ok", seen=seen), thinking=False)
    assert _sent_user(seen) == "Transcribe."
    assert json.loads(seen[-1].content)["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize(
    "answer",
    [
        "<think></think>\n\nLine one\nLine two",
        "<think>\n\n</think>\nLine one\nLine two",
        "Line one\nLine two /no_think",
        "<think></think>/no_think\nLine one\nLine two",
    ],
)
def test_empty_think_block_and_no_think_echo_are_stripped(local_llm, answer):
    out = llm.chat("sys", "Transcribe.", client=_client(answer), thinking=False)
    assert out == "Line one\nLine two"


def test_parse_json_tolerates_empty_think_block():
    assert llm.parse_json_object('<think>\n</think>\n{"a": 1} /no_think') == {"a": 1}
