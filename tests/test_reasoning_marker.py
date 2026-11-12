"""#770: reasoning-off marker for the LM Studio Prompt Template rule.

With ``LLM_REASONING_MARKER`` off (default) every request must stay
byte-identical to the pre-#770 one. With it on (or the extractors' explicit
per-call override), thinking=False carries the marker only at the end of the
system message and no ``/no_think`` anywhere; thinking on / default sends the
system prompt bare. The per-call override touches only that job's calls.

Synthetic data only; the local model is mocked at the HTTP transport.
"""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import httpx
import pytest

from oncofiles import config
from oncofiles.cloud_guard import local_only
from oncofiles.curated import classify, extract, llm, llm_extract, llm_extract_call, reocr
from oncofiles.curated import llm_extract_clinical as lxc
from oncofiles.curated.contract import parse_contract

MARKER = "[[reasoning:off]]"
URL = "http://llm.test/v1"
CONTRACT = parse_contract(
    (Path(__file__).parent / "fixtures" / "curated" / "README_oncology.md").read_text()
)
TEXT = "LEKÁRSKA SPRÁVA\nNález zo dňa 05.08.2027\nMUDr. Jana Kováčová\nNOU"
PAGE = "Hemoglobin 140 g/L\nLeukocyty 7,2 G/l"
ANSWER = json.dumps({"facts": [], "items": []})


@pytest.fixture
def local_llm(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", URL)
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_NO_THINK_SOFT_SWITCH", True)
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", False)


@pytest.fixture
def bodies(monkeypatch):
    """Every request body llm.chat sends (also with its own httpx client)."""
    seen: list[bytes] = []
    real = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        msg = {"content": ANSWER}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})

    monkeypatch.setattr(httpx, "Client", lambda **_kw: real(transport=httpx.MockTransport(handler)))
    return seen


def _legacy_body(system, user, *, thinking=None, max_tokens=llm.REASONING_BUDGET, **extra):
    """The request body exactly as llm.chat built it before #770."""
    payload = {
        "model": "test-model",
        "temperature": extra.pop("temperature", 0.0),
        "max_tokens": llm.clamp_budget(max_tokens, llm.estimate_prompt_tokens(system, user)),
        "reasoning_effort": "low",
        "messages": [
            {"role": "system", "content": system + "\n/no_think"},
            {"role": "user", "content": user},
        ],
    }
    if thinking is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
    if thinking is False:
        if isinstance(user, str):
            payload["messages"][-1]["content"] = user + " /no_think"
        else:
            payload["messages"][-1]["content"] = [*user, {"type": "text", "text": " /no_think"}]
    return httpx.Request("POST", URL + "/chat/completions", json=payload).content


IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
USERS = ["Page 1:\nHemoglobin 140 g/L", [{"type": "text", "text": "Page 1 of 1."}, IMAGE]]


# ── flag off: byte-identical to before ───────────────────────────────


@pytest.mark.parametrize("thinking", [True, False, None])
@pytest.mark.parametrize("user", USERS, ids=["str", "list"])
@pytest.mark.parametrize("explicit", [None, False])
def test_flag_off_payload_is_byte_identical(local_llm, bodies, thinking, user, explicit):
    llm.chat("SYS", user, thinking=thinking, reasoning_marker=explicit)
    assert bodies == [_legacy_body("SYS", user, thinking=thinking)]


# ── flag on: classify, re-OCR, extract.py, extractors ────────────────


def _messages(body: bytes) -> list[dict]:
    return json.loads(body)["messages"]


def _texts(messages: list[dict]) -> list[str]:
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append(content)
        else:
            out.extend(p.get("text", "") for p in content if p.get("type") == "text")
    return out


def _clean(body: bytes) -> bool:
    return not any(MARKER in t or "/no_think" in t for t in _texts(_messages(body)))


def _marker_only_at_system_end(body: bytes, system: str) -> bool:
    messages = _messages(body)
    first = messages[0]["content"]
    rest = _texts(messages[1:])
    return (
        isinstance(first, str)
        and first == system + "\n" + MARKER
        and first.count(MARKER) == 1
        and "/no_think" not in first
        and not any(MARKER in t or "/no_think" in t for t in rest)
    )


@pytest.mark.parametrize("thinking", [True, None])
@pytest.mark.parametrize("user", USERS, ids=["str", "list"])
def test_flag_on_thinking_on_or_default_sends_bare_prompts(
    local_llm, bodies, monkeypatch, thinking, user
):
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", True)
    llm.chat("SYS", user, thinking=thinking)
    first = _messages(bodies[0])[0]
    assert _clean(bodies[0])
    assert first == {"role": "system", "content": "SYS"}  # bare system prompt


@pytest.mark.parametrize("user", USERS, ids=["str", "list"])
def test_flag_on_thinking_off_marker_only_at_end_of_system(local_llm, bodies, monkeypatch, user):
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", True)
    llm.chat("SYS", user, thinking=False)
    assert _marker_only_at_system_end(bodies[0], "SYS")
    body = json.loads(bodies[0])
    assert body["messages"][1]["content"] == user  # user turn untouched
    assert body["chat_template_kwargs"] == {"enable_thinking": False}  # still sent


def test_flag_on_classify_and_extract_py_are_clean(local_llm, bodies, monkeypatch):
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", True)
    classify.propose(CONTRACT, TEXT)
    extract._vision_pages([b"\x89PNG synthetic"])
    assert len(bodies) == 2 and all(_clean(b) for b in bodies)


def _scan_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_flag_on_reocr_follows_reocr_thinking(local_llm, bodies, monkeypatch):
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", True)
    client = httpx.Client()  # the patched factory: mocked transport
    monkeypatch.setattr(config, "REOCR_THINKING", "on")
    reocr._vision_passes(_scan_pdf(), "pdf", [100], client)
    assert len(bodies) == 1 and _clean(bodies[0])
    monkeypatch.setattr(config, "REOCR_THINKING", "off")
    reocr._vision_passes(_scan_pdf(), "pdf", [100], client)
    assert len(bodies) == 2
    assert _marker_only_at_system_end(bodies[1], reocr.REOCR_SYSTEM_PROMPT)


@pytest.mark.parametrize("module", [llm_extract, lxc], ids=["lab", "clinical"])
def test_flag_on_extractors(local_llm, bodies, monkeypatch, module):
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", True)
    kw = {"known": frozenset()} if module is llm_extract else {}
    with local_only("test"):
        module.extract_page(PAGE, page_number=1, cfg=llm_extract_call.settings("on"), **kw)
        res = module.extract_page(PAGE, page_number=1, cfg=llm_extract_call.settings("off"), **kw)
    assert len(bodies) == 2
    assert _clean(bodies[0])
    assert _marker_only_at_system_end(bodies[1], module.SYSTEM_PROMPT)
    assert res.diagnostics()["reasoning_marker"] is True


@pytest.mark.parametrize("module", [llm_extract, lxc], ids=["lab", "clinical"])
def test_extractor_override_off_wins_over_env_on(local_llm, bodies, monkeypatch, module):
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", True)
    cfg = llm_extract_call.settings("off", reasoning_marker="off")
    kw = {"known": frozenset()} if module is llm_extract else {}
    with local_only("test"):
        module.extract_page(PAGE, page_number=1, cfg=cfg, **kw)
    assert MARKER not in bodies[0].decode()
    assert _messages(bodies[0])[0]["content"].endswith("\n/no_think")


# ── per-call override: settings, validation, write refusal ───────────


def test_settings_resolve_override_over_env(monkeypatch):
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", False)
    assert llm_extract_call.settings().reasoning_marker is False
    assert llm_extract_call.settings(reasoning_marker="on").reasoning_marker is True
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", True)
    assert llm_extract_call.settings().reasoning_marker is True
    assert llm_extract_call.settings(reasoning_marker="off").reasoning_marker is False
    assert llm_extract_call.settings(reasoning_marker="on").report()["reasoning_marker"] == "on"
    with pytest.raises(ValueError, match="reasoning_marker"):
        llm_extract_call.settings(reasoning_marker="yes")


def test_override_refused_on_write():
    for value in ("on", "off"):
        with pytest.raises(ValueError, match="dry run"):
            llm_extract_call.refuse_overrides_on_write(write=True, reasoning_marker=value)
        llm_extract_call.refuse_overrides_on_write(write=False, reasoning_marker=value)


# ── the override never leaks into a concurrent call ──────────────────


def test_concurrent_override_touches_only_its_own_job(local_llm, bodies, monkeypatch):
    """One extractor call with the override on and one classify call run at the
    same time: only the first payload carries the marker, the second is the
    flag-off payload byte for byte."""
    classify.propose(CONTRACT, TEXT)  # flag-off reference, sequential
    reference = bodies.pop()
    barrier = threading.Barrier(2, timeout=10)
    real = httpx._client.Client

    def handler(request: httpx.Request) -> httpx.Response:
        barrier.wait()  # both requests are in flight together
        bodies.append(request.content)
        msg = {"content": ANSWER}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})

    monkeypatch.setattr(httpx, "Client", lambda **_kw: real(transport=httpx.MockTransport(handler)))
    cfg = llm_extract_call.settings("off", reasoning_marker="on")
    errors: list[BaseException] = []

    def extractor():
        try:
            with local_only("test"):
                llm_extract.extract_page(PAGE, page_number=1, cfg=cfg, known=frozenset())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def classifier():
        try:
            classify.propose(CONTRACT, TEXT)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=extractor), threading.Thread(target=classifier)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert not errors and len(bodies) == 2
    ext = [b for b in bodies if llm_extract.SYSTEM_PROMPT[:40] in _messages(b)[0]["content"]]
    cls = [b for b in bodies if b not in ext]
    assert len(ext) == 1 and len(cls) == 1
    assert _marker_only_at_system_end(ext[0], llm_extract.SYSTEM_PROMPT)
    assert cls[0] == reference
    assert config.LLM_REASONING_MARKER is False


# ── the marker literal lives only in llm.py ──────────────────────────


def test_marker_string_only_in_llm_py():
    src = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
    hits = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "reasoning:off" in path.read_text(encoding="utf-8")
    )
    assert hits == ["curated/llm.py"]
    # ... and there only as the one constant, used by name everywhere else
    tree = ast.parse((src / "curated" / "llm.py").read_text(encoding="utf-8"))
    consts = [n for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and "reasoning:off" in n.value]  # fmt: skip
    assert len(consts) == 1 and llm.REASONING_OFF_MARKER == MARKER
