"""Dashboard chat with server-side history (#1023 follow-up to #1022).

``POST /api/chat`` with ``conversation_id``, the conversation list, one
conversation, delete (trash) and restore, and the UI locks. Synthetic data
only: invented names, example.test addresses, dates in 2027+. The chat core is
a scripted fake; the network guard is active.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

import pytest
from starlette.exceptions import HTTPException
from starlette.responses import StreamingResponse

from oncofiles import chat_dashboard, chat_history, config, secrets_keys, server
from oncofiles.chat import core
from oncofiles.chat.core import ChatEvent
from tests import test_1023_dashboard_chat as _base
from tests.test_1023_dashboard_chat import (
    A_EMAIL,
    A_UUID,
    ADMIN_EMAIL,
    B_UUID,
    HTML,
    JS,
    SECRET_A,
    SECRET_EVIDENCE,
    SECRET_Q,
    _body,
    _caller,
    _fake_answer,
    _lines,
    _request,
)

# the fixtures of the base chat tests (autouse state reset + the two patients)
_chat_state = _base._chat_state
chat_db = _base.chat_db

SECRET_Q2 = "Druha otazka o Zuzke Vymyslenej a hodnote 8,8"
OTHER_CID = "11111111-2222-4333-8444-555555555555"
GROUND = {"checked": 913, "replaced": 0}


@pytest.fixture(autouse=True)
def _history_key(monkeypatch):
    monkeypatch.setenv(secrets_keys.CHAT_HISTORY_SECRET_ENV, "synthetic-chat-ui-secret-1023")
    chat_history._reset_for_tests()
    chat_dashboard._reset_history_for_tests()
    yield


def _no_key(monkeypatch):
    monkeypatch.delenv(secrets_keys.CHAT_HISTORY_SECRET_ENV, raising=False)
    monkeypatch.delenv(secrets_keys.HOLDING_STORE_SECRET_ENV, raising=False)


async def _ask(db, monkeypatch, body, seen=None, email=A_EMAIL):
    monkeypatch.setattr(core, "answer", _fake_answer(seen=seen))
    with _caller(email):
        resp = await server.api_chat(_request(db, body))
    if not isinstance(resp, StreamingResponse):
        return resp, None
    return resp, await _lines(resp)


def _done(lines):
    return next(ln["data"] for ln in lines if ln["type"] == "done")


async def _list(db, *, trash=False, patient="alma-vzorova", email=A_EMAIL, **kw):
    q = {"patient_id": patient, "trash": "1" if trash else "0"}
    with _caller(email, **kw):
        return await server.api_chat_conversations(_request(db, query=q))


async def _open(db, cid, *, patient="alma-vzorova", email=A_EMAIL, **kw):
    q = {"patient_id": patient, "conversation_id": cid}
    with _caller(email, **kw):
        return await server.api_chat_conversation(_request(db, query=q))


async def _write(db, action, cid, *, patient="alma-vzorova", email=A_EMAIL, origin=None, **kw):
    route = (
        server.api_chat_conversation_delete
        if action == "delete"
        else server.api_chat_conversation_restore
    )
    body = {"patient_id": patient, "conversation_id": cid}
    with _caller(email, **kw):
        return await route(_request(db, body, origin=origin))


async def _rows(db) -> int:
    async with db.db.execute("SELECT COUNT(*) FROM chat_messages") as cur:
        return int((await cur.fetchone())[0])


async def _foreign_conversation(db) -> str:
    cid = chat_history.new_conversation_id()
    await chat_history.store_message(
        db,
        patient_id=B_UUID,
        conversation_id=cid,
        channel="dashboard",
        role="user",
        text="Otazka pacienta B",
    )
    return cid


# ── flag ──


@pytest.mark.parametrize(
    "route",
    [
        "api_chat_conversations",
        "api_chat_conversation",
        "api_chat_conversation_delete",
        "api_chat_conversation_restore",
    ],
)
async def test_flag_off_answers_like_an_unknown_path(chat_db, monkeypatch, route):
    monkeypatch.setattr(config, "CHAT_ENABLED", False)
    request = _request(chat_db, {"patient_id": "alma-vzorova"})
    with _caller(None), pytest.raises(HTTPException) as exc:
        await getattr(server, route)(request)
    assert exc.value.status_code == 404
    request.body.assert_not_called()


# ── stored turns and reload ──


async def test_new_conversation_is_stored_and_reloaded(chat_db, monkeypatch):
    seen: list = []
    resp, lines = await _ask(chat_db, monkeypatch, _body(), seen)
    done = _done(lines)
    cid = done["conversation_id"]
    assert chat_history.canonical_conversation_id(cid) == cid
    assert done["history_stored"] is True
    assert seen[0]["messages"] == [{"role": "user", "content": SECRET_Q}]
    assert await _rows(chat_db) == 2

    listing = json.loads((await _list(chat_db)).body)
    assert listing["history_available"] is True
    assert listing["trash_after_days"] == 90 and listing["purge_after_trash_days"] == 30
    [conv] = listing["conversations"]
    assert conv["conversation_id"] == cid
    assert conv["title"] == SECRET_Q[: chat_history.TITLE_CHARS]
    assert conv["turns"] == 2 and conv["last_at"]
    assert set(conv) == {"conversation_id", "title", "last_at", "turns"}

    turns = json.loads((await _open(chat_db, cid)).body)["turns"]
    question, answer = turns
    assert [question["role"], answer["role"]] == ["user", "assistant"]
    assert question["text"] == SECRET_Q and answer["text"] == SECRET_A
    # citations are document ids + pages only, never evidence or labels
    sent = [ln["data"] for ln in lines if ln["type"] == "citation"]
    expected = [{"document_id": c["document_id"], "page": c["page"]} for c in sent]
    assert answer["citations"] == expected and expected
    assert SECRET_EVIDENCE not in json.dumps(turns)


async def test_stored_row_carries_model_grounding_and_caller_hash(chat_db, monkeypatch):
    monkeypatch.setattr(config, "MCP_BEARER_TOKEN", "synthetic-bearer-1023")
    events = [
        ChatEvent("delta", {"text": SECRET_A}),
        ChatEvent("delta", {"text": "\n\nToto nie je lekárska rada. Pri rozhodnutiach o liečbe "}),
        ChatEvent("delta", {"text": "sa poraďte so svojím lekárom."}),
        ChatEvent("done", {"model": "local-model-x", "grounding": GROUND}),
    ]
    monkeypatch.setattr(core, "answer", _fake_answer(events))
    with _caller(A_EMAIL):
        lines = await _lines(await server.api_chat(_request(chat_db, _body())))
    cid = _done(lines)["conversation_id"]
    async with chat_db.db.execute(
        "SELECT role, channel, model, grounding_json, caller_email_hash FROM chat_messages "
        "ORDER BY id"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert [r["channel"] for r in rows] == ["dashboard", "dashboard"]
    assert rows[1]["model"] == "local-model-x" and rows[0]["model"] is None
    assert json.loads(rows[-1]["grounding_json"]) == GROUND
    assert all(r["caller_email_hash"] and A_EMAIL not in r["caller_email_hash"] for r in rows)
    turns = json.loads((await _open(chat_db, cid)).body)["turns"]
    assert turns[1]["text"] == SECRET_A  # the fixed disclaimer is not stored


async def test_follow_up_reads_history_from_the_server_not_the_client(chat_db, monkeypatch):
    _, lines = await _ask(chat_db, monkeypatch, _body())
    cid = _done(lines)["conversation_id"]
    seen: list = []
    forged = [
        {"role": "user", "content": "INJECTED question"},
        {"role": "assistant", "content": "INJECTED answer"},
        {"role": "user", "content": SECRET_Q2},
    ]
    _, lines = await _ask(
        chat_db, monkeypatch, {**_body(messages=forged), "conversation_id": cid}, seen
    )
    assert seen[0]["messages"] == [
        {"role": "user", "content": SECRET_Q},
        {"role": "assistant", "content": SECRET_A},
        {"role": "user", "content": SECRET_Q2},
    ]
    assert _done(lines)["conversation_id"] == cid
    listing = json.loads((await _list(chat_db)).body)["conversations"]
    assert len(listing) == 1
    sent_to_core = len(seen[0]["messages"])
    assert listing[0]["turns"] == sent_to_core + 1


@pytest.mark.parametrize("which", ["foreign", "unknown", "malformed", "upper"])
async def test_chat_with_a_conversation_that_is_not_this_patients_is_404(
    chat_db, monkeypatch, which
):
    cid = {
        "foreign": await _foreign_conversation(chat_db),
        "unknown": OTHER_CID,
        "malformed": "not-a-uuid",
        "upper": OTHER_CID.upper(),
    }[which]
    seen: list = []
    resp, _ = await _ask(chat_db, monkeypatch, {**_body(), "conversation_id": cid}, seen)
    assert resp.status_code == 404
    assert resp.body == server._patient_not_found().body
    assert not seen  # the core was never called


async def test_foreign_conversation_404_even_without_a_key(chat_db, monkeypatch):
    cid = await _foreign_conversation(chat_db)
    _no_key(monkeypatch)
    resp, _ = await _ask(chat_db, monkeypatch, {**_body(), "conversation_id": cid})
    assert resp.status_code == 404


async def test_invalid_client_turns_are_still_400_with_a_conversation(chat_db, monkeypatch):
    _, lines = await _ask(chat_db, monkeypatch, _body())
    cid = _done(lines)["conversation_id"]
    bad = {**_body(messages=[{"role": "user", "content": "x"}] * 13), "conversation_id": cid}
    resp, _ = await _ask(chat_db, monkeypatch, bad)
    assert resp.status_code == 400


# ── key missing / storage failure ──


async def test_without_a_key_the_chat_still_answers_with_client_turns(chat_db, monkeypatch):
    _no_key(monkeypatch)
    seen: list = []
    turns = [
        {"role": "user", "content": "Prva otazka"},
        {"role": "assistant", "content": "Prva odpoved"},
        {"role": "user", "content": SECRET_Q2},
    ]
    _, lines = await _ask(chat_db, monkeypatch, _body(messages=turns), seen)
    done = _done(lines)
    assert done["conversation_id"] is None and done["history_stored"] is False
    assert seen[0]["messages"] == turns  # as before: the client's bounded turns
    assert await _rows(chat_db) == 0
    listing = json.loads((await _list(chat_db)).body)
    assert listing == {
        "conversations": [],
        "history_available": False,
        "trash_after_days": 90,
        "purge_after_trash_days": 30,
    }
    assert chat_dashboard.history_counters()["history_unavailable"] == 1


async def test_storage_failure_never_breaks_the_answer(chat_db, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError(SECRET_A)

    monkeypatch.setattr(chat_history, "store_message", boom)
    caplog.set_level(logging.INFO)
    _, lines = await _ask(chat_db, monkeypatch, _body())
    assert [ln["type"] for ln in lines][-1] == "done"
    done = _done(lines)
    assert done["conversation_id"] is None and done["history_stored"] is False
    assert chat_dashboard.history_counters() == {"store_failed": 1}
    assert "store failed (RuntimeError)" in caplog.text
    assert SECRET_A not in caplog.text


async def test_failure_on_an_existing_conversation_keeps_its_id(chat_db, monkeypatch):
    _, lines = await _ask(chat_db, monkeypatch, _body())
    cid = _done(lines)["conversation_id"]

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(chat_history, "store_message", boom)
    _, lines = await _ask(chat_db, monkeypatch, {**_body(), "conversation_id": cid})
    assert _done(lines)["conversation_id"] == cid
    assert _done(lines)["history_stored"] is False


async def test_error_turn_stores_nothing(chat_db, monkeypatch):
    events = [ChatEvent("error", {"code": "model_busy", "message": "x"})]
    monkeypatch.setattr(core, "answer", _fake_answer(events))
    with _caller(A_EMAIL):
        await _lines(await server.api_chat(_request(chat_db, _body())))
    assert await _rows(chat_db) == 0


def test_build_messages_stays_inside_the_core_bounds():
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "text": "č" * 5000} for i in range(40)
    ] + [{"role": "assistant", "text": None}]
    question = {"role": "user", "content": "Posledna otazka"}
    msgs = chat_dashboard.build_messages(history, question)
    assert msgs[-1] == question
    assert core.validate(msgs, "sk") == msgs
    assert len(msgs) <= core.MAX_MESSAGES


def test_days_until_the_purge():
    now = datetime.fromisoformat("2031-09-17T05:43:00+00:00")

    def at(days):
        return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    assert chat_dashboard._days_left(at(0), now) == 30
    assert chat_dashboard._days_left(at(10.5), now) == 20
    assert chat_dashboard._days_left(at(29.9), now) == 1
    assert chat_dashboard._days_left(at(45), now) == 0
    assert chat_dashboard._days_left(None, now) is None
    assert chat_dashboard._days_left("garbage", now) is None


# ── trash and restore ──


async def test_trash_and_restore_round_trip(chat_db, monkeypatch):
    _, lines = await _ask(chat_db, monkeypatch, _body())
    cid = _done(lines)["conversation_id"]

    resp = await _write(chat_db, "delete", cid, origin="https://oncofiles.test")
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"status": "trashed", "messages": 2}
    assert json.loads((await _list(chat_db)).body)["conversations"] == []
    [trashed] = json.loads((await _list(chat_db, trash=True)).body)["conversations"]
    assert trashed["conversation_id"] == cid
    assert trashed["purge_in_days"] == 30 and trashed["deleted_at"]
    assert trashed["title"] == SECRET_Q[: chat_history.TITLE_CHARS]
    # a trashed conversation cannot be opened or continued
    assert (await _open(chat_db, cid)).status_code == 404
    resp, _ = await _ask(chat_db, monkeypatch, {**_body(), "conversation_id": cid})
    assert resp.status_code == 404
    # deleting twice is "not found"
    assert (await _write(chat_db, "delete", cid)).status_code == 404

    resp = await _write(chat_db, "restore", cid)
    assert json.loads(resp.body) == {"status": "restored", "messages": 2}
    assert json.loads((await _list(chat_db, trash=True)).body)["conversations"] == []
    assert len(json.loads((await _open(chat_db, cid)).body)["turns"]) == 2
    assert (await _write(chat_db, "restore", cid)).status_code == 404


# ── ACL on every new route ──

_REFUSED = [
    pytest.param({"patient": "bruno-ukazka"}, id="foreign-slug"),
    pytest.param({"patient": B_UUID}, id="foreign-uuid"),
    pytest.param({"patient": "nikto-taky"}, id="unknown"),
    pytest.param({"email": ADMIN_EMAIL, "admin": True}, id="admin-non-caregiver"),
    pytest.param({"email": None, "bearer": True}, id="operator-bearer"),
]


def _same_404(resp):
    assert resp.status_code == 404
    assert resp.body == server._patient_not_found().body


@pytest.mark.parametrize("who", _REFUSED)
async def test_list_acl(chat_db, monkeypatch, who):
    await _ask(chat_db, monkeypatch, _body())
    _same_404(await _list(chat_db, **who))
    _same_404(await _list(chat_db, trash=True, **who))


@pytest.mark.parametrize("who", _REFUSED)
async def test_open_acl(chat_db, monkeypatch, who):
    _, lines = await _ask(chat_db, monkeypatch, _body())
    _same_404(await _open(chat_db, _done(lines)["conversation_id"], **who))


@pytest.mark.parametrize("action", ["delete", "restore"])
@pytest.mark.parametrize("who", _REFUSED)
async def test_write_acl(chat_db, monkeypatch, who, action):
    _, lines = await _ask(chat_db, monkeypatch, _body())
    cid = _done(lines)["conversation_id"]
    if action == "restore":
        await _write(chat_db, "delete", cid)
    _same_404(await _write(chat_db, action, cid, **who))
    # nothing moved
    state = "1" if action == "restore" else "0"
    convs = json.loads(
        (await _list(chat_db, trash=state == "1")).body,
    )["conversations"]
    assert [c["conversation_id"] for c in convs] == [cid]


@pytest.mark.parametrize("which", ["foreign", "unknown", "malformed"])
async def test_open_and_write_another_patients_conversation_is_404(chat_db, which):
    cid = {
        "foreign": await _foreign_conversation(chat_db),
        "unknown": OTHER_CID,
        "malformed": "../etc",
    }[which]
    _same_404(await _open(chat_db, cid))
    _same_404(await _write(chat_db, "delete", cid))
    _same_404(await _write(chat_db, "restore", cid))
    if which == "foreign":  # B's conversation is untouched
        rows = await chat_history.conversation_messages(
            chat_db, patient_id=B_UUID, conversation_id=cid
        )
        assert len(rows) == 1


async def test_own_patient_by_uuid_is_fine(chat_db, monkeypatch):
    await _ask(chat_db, monkeypatch, _body())
    assert len(json.loads((await _list(chat_db, patient=A_UUID)).body)["conversations"]) == 1


async def test_writes_refuse_another_origin(chat_db, monkeypatch):
    _, lines = await _ask(chat_db, monkeypatch, _body())
    cid = _done(lines)["conversation_id"]
    resp = await _write(chat_db, "delete", cid, origin="https://evil.example")
    assert resp.status_code == 403
    assert len(json.loads((await _list(chat_db)).body)["conversations"]) == 1


async def test_writes_unauthenticated_401_and_bad_body_400(chat_db):
    with _caller(None):
        resp = await server.api_chat_conversation_delete(_request(chat_db, {}))
    assert resp.status_code == 401
    with _caller(A_EMAIL):
        resp = await server.api_chat_conversation_delete(_request(chat_db, b"[not json"))
    assert resp.status_code == 400
    with _caller(A_EMAIL):
        resp = await server.api_chat_conversation_restore(_request(chat_db, b"x" * 5000))
    assert resp.status_code == 413


async def test_admin_refusal_on_new_routes_is_audited(chat_db, monkeypatch):
    await _ask(chat_db, monkeypatch, _body())
    await _list(chat_db, email=ADMIN_EMAIL, admin=True)
    async with chat_db.db.execute(
        "SELECT target_patient_id FROM dashboard_access_log WHERE event = 'acl_refused'"
    ) as cur:
        targets = [r[0] for r in await cur.fetchall()]
    assert A_UUID in targets


# ── logs ──


async def test_no_text_in_logs(chat_db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _, lines = await _ask(chat_db, monkeypatch, _body())
    cid = _done(lines)["conversation_id"]
    await _ask(chat_db, monkeypatch, {**_body(messages=[{"role": "user", "content": SECRET_Q2}]),
                                      "conversation_id": cid})  # fmt: skip
    await _list(chat_db)
    await _open(chat_db, cid)
    await _write(chat_db, "delete", cid)
    await _list(chat_db, trash=True)
    await _write(chat_db, "restore", cid)
    await _open(chat_db, OTHER_CID)
    text = caplog.text
    # (the conversation id appears in aiosqlite's DEBUG lines of SQL parameters;
    # it is an opaque uuid, never text)
    for secret in (SECRET_Q, SECRET_Q2, SECRET_A, SECRET_EVIDENCE, "Zuzka", "Tajna", A_EMAIL):
        assert secret not in text, secret
    assert "chat conversations: trash=0 count=1" in text
    assert "chat conversation delete: status=trashed messages=4" in text


def test_no_logger_call_in_the_adapter_receives_text():
    src = (server.__file__.rsplit("/", 1)[0] + "/chat_dashboard.py").strip()
    body = open(src, encoding="utf-8").read()  # noqa: SIM115
    for call in re.findall(r"logger\.\w+\((.*?)\)\n", body, re.S):
        for banned in ("question", "answer_text", "plan.messages", "title", "turns["):
            assert banned not in call, call


# ── UI locks ──


def _section() -> str:
    m = re.search(r'<section class="panel chat-card".*?</section>', HTML, re.S)
    assert m
    return m.group(0)


def _chat_js() -> str:
    start = JS.index("// ── Chat over this patient's records (#1023)")
    return JS[start : JS.index("// ── Demo data (public /demo)")]


def test_conversation_markup_is_bilingual_and_has_no_inline_handlers():
    sec = _section()
    convs = sec[sec.index('id="chat-convs"') : sec.index('id="chat-log"')]
    assert 'data-action="newChatConversation"' in convs
    assert 'id="chat-trash" class="chat-trash" hidden' in sec
    for tag in re.findall(r"<(?:p|h3|button|summary)\b[^>]*>", convs):
        assert "data-sk=" in tag and "data-en=" in tag, tag
    assert "90" in convs and "30" in convs  # the retention rule is stated
    assert not re.search(r"\son[a-z]+=", sec)


def test_conversation_js_rules():
    block = _chat_js()
    assert "innerHTML" not in block
    assert "Promise.all" not in block
    assert "confirm(" not in block and "alert(" not in block
    for fn in (
        "function loadChatConversations(",
        "function renderChatConversations(",
        "function openChatConversation(",
        "function newChatConversation(",
        "function deleteChatConversation(",
        "function restoreChatConversation(",
    ):
        assert fn in block, fn
    # delete: an armed second click
    start = block.index("function deleteChatConversation(")
    delete = block[start : start + 1500]
    assert "chatDeleteArmed !== id" in delete and "setTimeout" in delete
    assert "'Naozaj zmazať? Kliknite znova', 'Delete? Click again'" in block
    assert "'/api/chat/conversations?trash=0'" in block
    assert "'/api/chat/conversations?trash=1'" in block
    assert "payload.conversation_id = sentConversation" in block
    assert "d.conversation_id" in block
    assert "conversation_gone: ['" in block
    # the purge countdown is shown in the trash
    assert "c.purge_in_days" in block and "skDays(" in block
    # every action goes through the dispatcher
    for action in ("openChatConversation|", "deleteChatConversation|", "restoreChatConversation|"):
        assert f"'data-action', '{action}'" in block, action


def test_demo_blocks_the_writes_and_has_twins():
    blocked = JS[JS.index("var DEMO_BLOCKED_ACTIONS = {") : JS.index("window._dispatch")]
    assert "deleteChatConversation: 1" in blocked and "restoreChatConversation: 1" in blocked
    assert "newChatConversation" not in blocked and "openChatConversation" not in blocked
    route = JS[JS.index("function demoRoute(") : JS.index("var demoNoticeTimer")]
    assert "path === '/api/chat/conversations'" in route
    assert "path === '/api/chat/conversation'" in route
    assert "d.chat_conversations_trash" in route and "d.chat_conversation_turns" in route


async def test_demo_twins_have_the_route_shapes():
    from starlette.requests import Request

    req = Request({"type": "http", "method": "GET", "path": "/api/demo-data", "headers": []})
    data = json.loads((await server.api_demo_data(req)).body)["dashboard"]
    live = data["chat_conversations"]
    assert set(live) == {
        "conversations",
        "history_available",
        "trash_after_days",
        "purge_after_trash_days",
    }
    [conv] = live["conversations"]
    assert {"conversation_id", "title", "last_at", "turns"} <= set(conv)
    assert conv["last_at"] >= "2027"
    assert data["chat_conversations_trash"]["conversations"] == []
    turns = data["chat_conversation_turns"][conv["conversation_id"]]["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert all(set(c) == {"document_id", "page"} for t in turns for c in t["citations"])
