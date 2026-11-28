"""#1024: the chat on /demo answers from a scripted set, never with a request.

The style of ``test_dashboard_redesign.py``: string locks over the shipped
dashboard files plus the ``/api/demo-data`` payload itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from starlette.requests import Request

from oncofiles import demo_dashboard, server

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
HTML = (SRC / "dashboard.html").read_text(encoding="utf-8")
JS = (SRC / "dashboard.js").read_text(encoding="utf-8")


async def _dashboard() -> dict:
    req = Request({"type": "http", "method": "GET", "path": "/api/demo-data", "headers": []})
    return json.loads((await server.api_demo_data(req)).body)["dashboard"]


def _chat_js() -> str:
    start = JS.index("// ── Chat over this patient's records (#1023)")
    return JS[start : JS.index("// ── Demo data (public /demo)")]


def _fn(name: str) -> str:
    """The source of one top-level function of dashboard.js (up to the next one)."""
    start = JS.index(f"function {name}(")
    nxt = re.compile(r"\n    (?:async )?function \w+\(|\n    // ── ")
    m = nxt.search(JS, start + 10)
    return JS[start : m.start() if m else len(JS)]


# ── the payload ──


async def test_demo_payload_has_the_scripted_chat_with_sk_en_twins():
    d = await _dashboard()
    script = d["chat_script"]
    suggestions = d["chat_suggestions"]["suggestions"]
    answers = script["answers"]
    assert 5 <= len(suggestions) <= 6
    assert len(answers) == len(suggestions)  # answers in the order of the suggestions
    for q in suggestions:
        assert set(q) == {"sk", "en"} and q["sk"] and q["en"] and q["sk"] != q["en"]
    for a in answers:
        assert set(a) == {"status", "text", "text_en", "citations"}
        assert a["text"] and a["text_en"] and a["text"] != a["text_en"]
        for c in a["citations"]:
            assert c["label"] and c["label_en"]
            assert c["date"] >= "2027"
        # the status lines name real chat tools
        from oncofiles.chat.tools import CHAT_TOOLS

        assert set(a["status"]) <= set(CHAT_TOOLS)
    free = script["free_text"]
    assert free["text"] and free["text_en"]
    assert "lokálny model" in free["text"] and "local model" in free["text_en"]
    assert "navrhnuté otázky" in free["text"] and "suggested questions" in free["text_en"]


async def test_the_scripted_set_covers_the_required_kinds():
    texts = [a["text_en"] for a in (await _dashboard())["chat_script"]["answers"]]
    joined = " ".join(texts)
    assert "reference range" in joined  # the latest value with its printed range
    assert "dipped" in joined  # a trend
    assert "last report" in joined
    assert "Example Hospital" in joined  # documents of an institution
    assert "next visit" in joined  # the next planned date
    assert "not in this patient's records" in joined  # one not-in-records answer
    # the not-in-records answer cites nothing
    assert any(not a["citations"] for a in (await _dashboard())["chat_script"]["answers"])


async def test_every_citation_exists_in_the_demo_documents():
    d = await _dashboard()
    ids = {r["id"] for r in d["documents"]["documents"] if isinstance(r["id"], int)}
    cited = {c["document_id"] for a in d["chat_script"]["answers"] for c in a["citations"]}
    for turns in d["chat_conversation_turns"].values():
        cited |= {c["document_id"] for t in turns["turns"] for c in t["citations"]}
    assert cited and cited <= ids
    # each citation has a detail (the quick look opens it)
    assert all(str(i) in d["details"] for i in cited)


async def test_the_past_conversations_replay_scripted_answers():
    d = await _dashboard()
    convs = d["chat_conversations"]["conversations"]
    assert 1 <= len(convs) <= 2
    assert [c["last_at"] for c in convs] == sorted((c["last_at"] for c in convs), reverse=True)
    texts = {a["text"] for a in d["chat_script"]["answers"]}
    for c in convs:
        assert c["title"] and c["title_en"] and c["last_at"] >= "2027"
        turns = d["chat_conversation_turns"][c["conversation_id"]]["turns"]
        assert turns[1]["text"] in texts
        assert all(t["created_at"] >= "2027" for t in turns)


def test_the_script_is_invented():
    blob = json.dumps(demo_dashboard.CHAT_SCRIPT, ensure_ascii=False)
    blob += json.dumps(demo_dashboard.CHAT_FREE_TEXT, ensure_ascii=False)
    years = set(re.findall(r"\b(?:19|20)\d\d\b", blob))
    assert years and all(y >= "2027" for y in years), years
    # no other person or institution than the invented ones
    assert "Nemocnic" in blob and "Example Hospital" in blob


# ── the page ──


def test_send_chat_in_demo_never_reaches_the_chat_api():
    send = _fn("sendChat")
    demo_line = "if (DEMO) { await demoSendChat(); return; }"
    assert demo_line in send
    assert send.index(demo_line) < send.index("fetch('/api/chat'")
    engine = "".join(
        _fn(n)
        for n in (
            "demoSendChat",
            "demoChatAnswer",
            "demoChatScript",
            "demoChunks",
            "demoSleep",
            "demoReducedMotion",
        )
    )
    for forbidden in ("fetch(", "apiFetch(", "XMLHttpRequest", "/api/chat", "sendBeacon"):
        assert forbidden not in engine, forbidden
    assert "demoData.dashboard.chat_script" in engine


def test_send_chat_is_allowed_in_the_demo_only_for_the_scripted_path():
    blocked = JS[JS.index("var DEMO_BLOCKED_ACTIONS = {") : JS.index("window._dispatch")]
    assert "sendChat: 1" not in blocked
    # saved conversations stay read-only in the demo
    assert "deleteChatConversation: 1" in blocked and "restoreChatConversation: 1" in blocked


def test_the_fetch_chokepoint_still_allows_only_the_demo_data():
    assert "var DEMO_ALLOWED_PATHS = ['/api/demo-data'];" in JS
    assert JS.count("DEMO_ALLOWED_PATHS = ") == 1
    choke = JS[JS.index("if (DEMO) {\n      // Chokepoint") : JS.index("var DEMO_BLOCKED_ACTIONS")]
    assert "window.fetch = function" in choke
    assert "XMLHttpRequest.prototype.open = function" in choke
    assert choke.count("DEMO_ALLOWED_PATHS.indexOf(target.pathname) === -1") == 2


def test_the_demo_routes_have_twins():
    route = _fn("demoRoute")
    assert "path === '/api/chat/suggestions'" in route
    assert "path === '/api/chat/conversations'" in route
    assert "path === '/api/chat/conversation'" in route
    assert "/api/chat'" not in route.replace("/api/chat/", "")  # no twin of POST /api/chat


def test_the_free_text_answer_and_streaming_rules():
    engine = _fn("demoChatAnswer")
    assert "script.free_text" in engine
    assert "chatSuggestionsData" in engine  # a suggestion matches by its SK or EN text
    send = _fn("demoSendChat")
    assert "DEMO_CHUNK_MIN_MS" in send and "DEMO_CHUNK_MAX_MS" in send
    assert "var DEMO_CHUNK_MIN_MS = 20;" in JS and "var DEMO_CHUNK_MAX_MS = 40;" in JS
    # every answer gets the sources line and the disclaimer
    assert "chatAppendSources(answer," in send
    assert "This is not medical advice." in _fn("chatAppendSources")
    # Stop works for the simulated stream too
    assert "new AbortController()" in send and "chatAbort = ac" in send
    # the finished answer carries both languages
    assert "setI18n(answer.body, sk, en)" in send


def test_reduced_motion_shows_the_text_at_once():
    assert "matchMedia('(prefers-reduced-motion: reduce)')" in _fn("demoReducedMotion")
    send = _fn("demoSendChat")
    assert "if (demoReducedMotion()) {" in send
    branch = send[send.index("if (demoReducedMotion()) {") :]
    assert branch.index("answer.body.textContent = shown") < branch.index("} else {")


def test_chat_is_shown_in_the_demo_with_the_patient_name():
    vis = _fn("updateChatVisibility")
    assert "(chatEnabled || DEMO) && !isAdminMode" in vis
    assert "setI18n(nameEl," in vis and "display_name_en" in vis


def test_i18n_and_no_unsafe_dom():
    block = _chat_js()
    assert "innerHTML" not in block
    assert "insertAdjacentHTML" not in block and "outerHTML" not in block
    assert "Promise.all" not in block
    assert "confirm(" not in block and "alert(" not in block
    # the citation chip shows the English label twin in EN
    assert "c.label_en" in _fn("chatCitationChip")
    sec = re.search(r'<section class="panel chat-card".*?</section>', HTML, re.S).group(0)
    assert not re.search(r"\son[a-z]+=", sec)
    for tag in re.findall(r"<(?:p|h2|h3|button|label|summary)\b[^>]*>", sec):
        if 'id="chat-state"' in tag:  # filled by the script with setI18n
            continue
        assert "data-sk=" in tag and "data-en=" in tag, tag
