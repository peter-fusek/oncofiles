"""Dashboard chat (#1023): routes, ACL, limits, framing, UI locks.

Synthetic data only: invented names, example.test addresses, dates in 2027+.
The chat core is replaced by a scripted fake where the route is under test;
the network guard is active.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, StreamingResponse

from oncofiles import chat_dashboard, config, dashboard_audit, ratelimit, server
from oncofiles.chat import core
from oncofiles.chat.core import ChatEvent
from oncofiles.curated import llm
from oncofiles.database import Database
from tests.helpers import PRIMARY_TEST_UUID

A_UUID = PRIMARY_TEST_UUID
B_UUID = "00000000-0000-4000-8000-000000001023"
A_EMAIL = "carer-a@example.test"
B_EMAIL = "carer-b@example.test"
ADMIN_EMAIL = "admin-x@example.test"
SECRET_Q = "Ako sa ma Zuzka Vymyslena dnes 7.7 g/l"
SECRET_A = "Odpoved obsahuje hodnotu 123,4 a vetu Tajna veta"
SECRET_EVIDENCE = "HGB 123,4 g/l zo dna 2027-03-11"

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
HTML = (SRC / "dashboard.html").read_text(encoding="utf-8")
JS = (SRC / "dashboard.js").read_text(encoding="utf-8")
CSS = (SRC / "dashboard.css").read_text(encoding="utf-8")


# ── fixtures ──


@pytest.fixture(autouse=True)
def _chat_state(monkeypatch):
    core.reset_state()
    chat_dashboard.limiter.reset()
    dashboard_audit._reset_for_tests()
    monkeypatch.setattr(config, "CHAT_ENABLED", True)
    for name in (
        "CHAT_TURNS_PER_MIN",
        "CHAT_TURNS_PER_HOUR",
        "CHAT_IP_TURNS_PER_MIN",
        "CHAT_IP_TURNS_PER_HOUR",
        "TRUSTED_PROXIES",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    core.reset_state()
    chat_dashboard.limiter.reset()


@pytest.fixture
async def chat_db(db: Database) -> Database:
    await db.db.execute(
        "UPDATE patients SET slug = ?, display_name = ?, caregiver_email = ? WHERE patient_id = ?",
        ("alma-vzorova", "Alma Vzorová", A_EMAIL, A_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (B_UUID, "bruno-ukazka", "Bruno Ukážka", B_EMAIL),
    )
    await db.db.commit()
    return db


def _fake_answer(events=None, seen=None):
    async def answer(db, *, patient_id, principal, messages, lang):
        if seen is not None:
            seen.append({"patient_id": patient_id, "principal": principal, "messages": messages})
        for ev in events or [
            ChatEvent("status", {"status": "queued"}),
            ChatEvent("status", {"status": "tool:fact_series"}),
            ChatEvent("delta", {"text": SECRET_A}),
            ChatEvent(
                "citation",
                {
                    "document_id": 7,
                    "page": 1,
                    "label": "HGB",
                    "date": "2027-03-11",
                    "evidence": SECRET_EVIDENCE,
                    "verification": "unverified",
                    "unverified": True,
                },
            ),
            ChatEvent("done", {"route": "router:labs", "citations": 1}),
        ]:
            yield ev

    return answer


def _body(patient="alma-vzorova", messages=None, lang="sk") -> dict:
    return {
        "patient_id": patient,
        "lang": lang,
        "messages": messages or [{"role": "user", "content": SECRET_Q}],
    }


def _request(
    db: Database,
    body: dict | bytes | None = None,
    *,
    query: dict | None = None,
    origin: str | None = None,
    ip: str = "203.0.113.10",
    content_length: int | None = None,
) -> MagicMock:
    raw = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
    request = MagicMock()
    request.query_params = query or {}
    request.path_params = {}
    headers = {"authorization": "Bearer session:synthetic", "host": "oncofiles.test"}
    headers["content-length"] = str(len(raw) if content_length is None else content_length)
    if origin is not None:
        headers["origin"] = origin
    request.headers = headers
    request.scope = {"client": (ip, 4242), "headers": [], "endpoint": server.api_chat}
    request.app.state.fastmcp_server._lifespan_result = {"db": db, "files": None}
    request.body = AsyncMock(return_value=raw)
    return request


@contextmanager
def _caller(email: str | None, *, bearer: bool = False, admin: bool = False):
    unauthorized = JSONResponse({"error": "unauthorized"}, status_code=401)
    with (
        patch.object(server, "_get_dashboard_email", return_value=None if bearer else email),
        patch.object(server, "_check_bearer", return_value=None if bearer else unauthorized),
        patch.object(server, "_is_admin_email", return_value=admin),
    ):
        yield


async def _lines(resp: StreamingResponse) -> list[dict]:
    raw = b""
    async for chunk in resp.body_iterator:
        raw += chunk if isinstance(chunk, bytes) else chunk.encode()
    return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]


async def _refusals(db: Database) -> list[dict]:
    async with db.db.execute(
        "SELECT event, target_patient_id FROM dashboard_access_log WHERE event = 'acl_refused'"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ── the core uses the interactive lane ──


def test_core_calls_the_model_on_the_interactive_lane(monkeypatch):
    seen = {}

    def fake_chat(system, user, **kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr(llm, "chat", fake_chat)
    assert core._call_llm("system", "user", {}) == "ok"
    assert seen["lane"] == "interactive"
    assert seen["thinking"] is False


# ── flag ──


@pytest.mark.parametrize("route", ["api_chat", "api_chat_suggestions"])
async def test_flag_off_answers_like_an_unknown_path(chat_db, monkeypatch, route):
    monkeypatch.setattr(config, "CHAT_ENABLED", False)
    monkeypatch.setattr(core, "answer", _fake_answer())
    request = _request(chat_db, _body())
    with _caller(None), pytest.raises(HTTPException) as exc:  # even unauthenticated
        await getattr(server, route)(request)
    assert exc.value.status_code == 404
    request.body.assert_not_called()


async def test_flag_off_hides_the_section(chat_db, monkeypatch):
    monkeypatch.setattr(config, "CHAT_ENABLED", False)
    with _caller(A_EMAIL):
        data = json.loads((await server.api_list_patients(_request(chat_db))).body)
    assert data["chat_enabled"] is False
    monkeypatch.setattr(config, "CHAT_ENABLED", True)
    with _caller(A_EMAIL):
        data = json.loads((await server.api_list_patients(_request(chat_db))).body)
    assert data["chat_enabled"] is True
    with _caller(None, bearer=True):
        data = json.loads((await server.api_list_patients(_request(chat_db))).body)
    assert data["chat_enabled"] is False  # the operator bearer has no chat


# ── ACL ──


async def test_caregiver_gets_the_stream(chat_db, monkeypatch):
    seen: list = []
    monkeypatch.setattr(core, "answer", _fake_answer(seen=seen))
    with _caller(A_EMAIL):
        resp = await server.api_chat(_request(chat_db, _body()))
    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "application/x-ndjson"
    assert resp.headers["cache-control"] == "no-store"
    lines = await _lines(resp)
    assert [ln["type"] for ln in lines] == ["status", "status", "delta", "citation", "done"]
    assert all(set(ln) == {"type", "data"} for ln in lines)
    assert lines[3]["data"]["unverified"] is True
    assert seen[0]["patient_id"] == A_UUID  # the slug resolved to the UUID
    assert seen[0]["principal"] == A_EMAIL
    assert seen[0]["messages"] == [{"role": "user", "content": SECRET_Q}]


@pytest.mark.parametrize("patient", ["bruno-ukazka", B_UUID, "nikto-taky", "missing-1023"])
async def test_foreign_and_unknown_get_the_same_404(chat_db, monkeypatch, patient):
    monkeypatch.setattr(core, "answer", _fake_answer())
    with _caller(A_EMAIL):
        resp = await server.api_chat(_request(chat_db, _body(patient)))
    assert resp.status_code == 404
    assert resp.body == server._patient_not_found().body
    assert dict(resp.headers) == dict(server._patient_not_found().headers)


async def test_admin_who_is_not_the_caregiver_is_refused_and_audited(chat_db, monkeypatch):
    monkeypatch.setattr(core, "answer", _fake_answer())
    with _caller(ADMIN_EMAIL, admin=True):
        resp = await server.api_chat(_request(chat_db, _body()))
    assert resp.status_code == 404
    assert resp.body == server._patient_not_found().body
    rows = await _refusals(chat_db)
    assert {"event": "acl_refused", "target_patient_id": A_UUID} in rows


async def test_admin_who_is_the_caregiver_may_chat(chat_db, monkeypatch):
    monkeypatch.setattr(core, "answer", _fake_answer())
    with _caller(A_EMAIL, admin=True):
        resp = await server.api_chat(_request(chat_db, _body()))
    assert isinstance(resp, StreamingResponse)


async def test_operator_bearer_is_refused(chat_db, monkeypatch):
    monkeypatch.setattr(core, "answer", _fake_answer())
    with _caller(None, bearer=True):
        resp = await server.api_chat(_request(chat_db, _body()))
    assert resp.status_code == 404
    assert resp.body == server._patient_not_found().body


async def test_unauthenticated_is_401(chat_db):
    with _caller(None):
        resp = await server.api_chat(_request(chat_db, _body()))
    assert resp.status_code == 401


async def test_foreign_refusal_is_audited(chat_db, monkeypatch):
    monkeypatch.setattr(core, "answer", _fake_answer())
    with _caller(A_EMAIL):
        await server.api_chat(_request(chat_db, _body("bruno-ukazka")))
    assert {"event": "acl_refused", "target_patient_id": B_UUID} in await _refusals(chat_db)


# ── origin, validation, busy, limits, body cap ──


async def test_cross_origin_is_refused(chat_db, monkeypatch):
    monkeypatch.setattr(core, "answer", _fake_answer())
    with _caller(A_EMAIL):
        resp = await server.api_chat(_request(chat_db, _body(), origin="https://evil.example"))
    assert resp.status_code == 403
    with _caller(A_EMAIL):
        ok = await server.api_chat(_request(chat_db, _body(), origin="https://oncofiles.test"))
    assert isinstance(ok, StreamingResponse)


@pytest.mark.parametrize(
    "body",
    [
        _body(lang="de"),
        _body(messages=[{"role": "assistant", "content": "x"}]),
        _body(messages=[{"role": "user", "content": "x"}] * 13),
        _body(messages=[{"role": "system", "content": "x"}]),
        {"patient_id": "alma-vzorova", "lang": "sk"},
    ],
)
async def test_invalid_requests_are_400(chat_db, monkeypatch, body):
    monkeypatch.setattr(core, "answer", _fake_answer())
    with _caller(A_EMAIL):
        resp = await server.api_chat(_request(chat_db, body))
    assert resp.status_code == 400


async def test_non_json_body_is_400(chat_db):
    with _caller(A_EMAIL):
        resp = await server.api_chat(_request(chat_db, b"{not json"))
    assert resp.status_code == 400


async def test_busy_principal_gets_409(chat_db, monkeypatch):
    monkeypatch.setattr(core, "answer", _fake_answer())
    core._running.add(A_EMAIL)
    with _caller(A_EMAIL):
        resp = await server.api_chat(_request(chat_db, _body()))
    assert resp.status_code == 409
    assert json.loads(resp.body)["error"] == "busy"


async def test_turn_limit_per_principal(chat_db, monkeypatch):
    monkeypatch.setattr(core, "answer", _fake_answer())
    monkeypatch.setenv("CHAT_TURNS_PER_MIN", "2")
    for _ in range(2):
        with _caller(A_EMAIL):
            assert isinstance(await server.api_chat(_request(chat_db, _body())), StreamingResponse)
    with _caller(A_EMAIL):
        resp = await server.api_chat(_request(chat_db, _body(), ip="198.51.100.7"))
    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) >= 1
    assert json.loads(resp.body)["error"] == "rate_limited"


async def test_turn_limit_per_ip(chat_db, monkeypatch):
    monkeypatch.setattr(core, "answer", _fake_answer())
    monkeypatch.setenv("CHAT_IP_TURNS_PER_MIN", "1")
    with _caller(A_EMAIL):
        assert isinstance(await server.api_chat(_request(chat_db, _body())), StreamingResponse)
    with _caller(B_EMAIL):  # another caregiver from the same address
        resp = await server.api_chat(_request(chat_db, _body("bruno-ukazka")))
    assert resp.status_code == 429


async def test_refused_turns_are_not_counted(chat_db, monkeypatch):
    monkeypatch.setattr(core, "answer", _fake_answer())
    monkeypatch.setenv("CHAT_TURNS_PER_MIN", "1")
    with _caller(A_EMAIL):
        assert (await server.api_chat(_request(chat_db, _body(lang="xx")))).status_code == 400
        assert isinstance(await server.api_chat(_request(chat_db, _body())), StreamingResponse)


def test_turn_limiter_hour_window_and_reset():
    now = [1000.0]
    lim = chat_dashboard.TurnLimiter(clock=lambda: now[0])
    with patch.dict("os.environ", {"CHAT_TURNS_PER_MIN": "0", "CHAT_TURNS_PER_HOUR": "3"}):
        assert all(lim.take("p:x", "192.0.2.1") == 0 for _ in range(3))
        wait = lim.take("p:x", "192.0.2.1")
        assert 3500 < wait <= 3600
        now[0] += 3601
        assert lim.take("p:x", "192.0.2.1") == 0


async def test_route_caps_the_body(chat_db):
    with _caller(A_EMAIL):
        resp = await server.api_chat(_request(chat_db, _body(), content_length=40 * 1024))
    assert resp.status_code == 413
    big = json.dumps(_body(messages=[{"role": "user", "content": "x" * 40_000}])).encode()
    with _caller(A_EMAIL):
        resp = await server.api_chat(_request(chat_db, big, content_length=10))
    assert resp.status_code == 413


async def test_rate_limiter_caps_the_chat_body():
    called = []

    async def app(scope, receive, send):
        called.append(scope["path"])

    mw = ratelimit.RateLimitMiddleware(app, ratelimit.RateLimitConfig())
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat",
        "client": ("192.0.2.9", 1),
        "headers": [(b"content-length", str(33 * 1024).encode())],
    }
    await mw(scope, receive, send)
    assert sent[0]["status"] == 413
    assert not called
    assert ratelimit.classify("/api/chat") == ratelimit.API
    assert ratelimit.RateLimitConfig().chat_max_body_bytes == chat_dashboard.MAX_BODY_BYTES


# ── framing and logs ──


async def test_error_event_is_framed_and_logged_by_code(chat_db, monkeypatch, caplog):
    events = [
        ChatEvent("status", {"status": "queued"}),
        ChatEvent("error", {"code": "model_busy", "message": "Model je zaneprázdnený."}),
    ]
    monkeypatch.setattr(core, "answer", _fake_answer(events))
    caplog.set_level(logging.INFO)
    with _caller(A_EMAIL):
        resp = await server.api_chat(_request(chat_db, _body()))
    lines = await _lines(resp)
    assert lines[-1] == {
        "type": "error",
        "data": {"code": "model_busy", "message": "Model je zaneprázdnený."},
    }
    assert "error=model_busy" in caplog.text


async def test_no_text_in_logs(chat_db, monkeypatch, caplog):
    monkeypatch.setattr(core, "answer", _fake_answer())
    caplog.set_level(logging.DEBUG)
    with _caller(A_EMAIL):
        resp = await server.api_chat(_request(chat_db, _body()))
    await _lines(resp)
    with _caller(A_EMAIL):
        await server.api_chat(_request(chat_db, _body("bruno-ukazka")))
    text = caplog.text
    for secret in (SECRET_Q, SECRET_A, SECRET_EVIDENCE, "Zuzka", "Tajna", "123,4", A_EMAIL):
        assert secret not in text, secret
    assert "chat http: outcome=done" in text


def test_no_logger_call_in_the_adapter_receives_text():
    src = (SRC / "chat_dashboard.py").read_text(encoding="utf-8")
    for call in re.findall(r"logger\.\w+\((.*?)\)\n", src, re.S):
        for banned in ("messages", "ev.data", "content", "text"):
            assert banned not in call, call


# ── suggestions ──


async def test_suggestions_from_the_patients_data(chat_db, monkeypatch):
    monkeypatch.setattr(
        chat_db,
        "fact_catalog_rows",
        AsyncMock(
            return_value=[
                {"code": "HGB", "category": "lab", "count": 3, "last_at": "2027-05-02"},
                {"code": "PLT", "category": "lab", "count": 1, "last_at": "2027-04-01"},
                {"code": "EGFR_GENE", "category": "genetic_variant", "count": 1, "last_at": "x"},
            ]
        ),
    )
    monkeypatch.setattr(
        chat_db, "timeline_rows", AsyncMock(return_value=[{"kind": "planned", "at": "2099-01-01"}])
    )
    with _caller(A_EMAIL):
        resp = await server.api_chat_suggestions(
            _request(chat_db, query={"patient_id": "alma-vzorova"})
        )
    assert resp.status_code == 200
    items = json.loads(resp.body)["suggestions"]
    assert 3 <= len(items) <= 5
    assert all(set(q) == {"sk", "en"} and q["sk"] and q["en"] for q in items)
    joined = " ".join(q["en"] for q in items)
    assert "latest HGB" in joined and "PLT" in joined and "next planned date" in joined
    assert "EGFR_GENE" not in joined


async def test_suggestions_without_data_still_three(chat_db):
    with _caller(A_EMAIL):
        resp = await server.api_chat_suggestions(
            _request(chat_db, query={"patient_id": "alma-vzorova"})
        )
    assert len(json.loads(resp.body)["suggestions"]) >= 3


async def test_suggestions_make_no_model_call(chat_db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no model call")

    monkeypatch.setattr(llm, "chat", boom)
    with _caller(A_EMAIL):
        resp = await server.api_chat_suggestions(
            _request(chat_db, query={"patient_id": "alma-vzorova"})
        )
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "email,admin,bearer,patient",
    [
        (A_EMAIL, False, False, "bruno-ukazka"),
        (A_EMAIL, False, False, "nikto-taky"),
        (ADMIN_EMAIL, True, False, "alma-vzorova"),
        (None, False, True, "alma-vzorova"),
    ],
)
async def test_suggestions_acl(chat_db, email, admin, bearer, patient):
    with _caller(email, admin=admin, bearer=bearer):
        resp = await server.api_chat_suggestions(_request(chat_db, query={"patient_id": patient}))
    assert resp.status_code == 404
    assert resp.body == server._patient_not_found().body


# ── UI locks (the style of test_dashboard_redesign.py) ──


def _section() -> str:
    m = re.search(r'<section class="panel chat-card".*?</section>', HTML, re.S)
    assert m
    return m.group(0)


def test_chat_section_markup():
    sec = _section()
    assert 'id="chat-card" hidden' in sec
    assert 'role="log" aria-live="polite"' in sec
    assert 'data-action="sendChat"' in sec and 'data-action="stopChat"' in sec
    assert 'data-sk="Odpovedá len z dokumentov:" data-en="Answers only from the records of:"' in sec
    assert "data-sk-placeholder=" in sec and "data-en-placeholder=" in sec
    for tag in re.findall(r"<(?:p|h2|button|label)\b[^>]*>", sec):
        if 'id="chat-state"' in tag:  # filled by the script with setI18n
            continue
        assert "data-sk=" in tag and "data-en=" in tag, tag
    assert not re.search(r"\son[a-z]+=", sec)  # no inline handlers


def test_chat_js_rules():
    start = JS.index("// ── Chat over this patient's records (#1023)")
    block = JS[start : JS.index("// ── Demo data (public /demo)")]
    assert "innerHTML" not in block
    assert "Promise.all" not in block
    assert "textContent" in block
    # Enter goes through the dispatcher, so the demo block applies to it too
    assert "window._dispatch('sendChat', input)" in block
    assert "if (DEMO) { demoNotice(); return; }" in block
    assert "chatEnabled && !isAdminMode" in block
    assert "new AbortController()" in block and "function stopChat(" in block
    assert "openQuickLook(docId, el)" in block
    for code in ("model_busy", "model_unavailable", "busy", "refused", "rate_limited"):
        assert re.search(rf"\b{code}: \['", block), code


def test_demo_blocks_the_chat_and_has_a_suggestions_twin():
    block = JS[JS.index("var DEMO_BLOCKED_ACTIONS = {") : JS.index("window._dispatch")]
    assert "sendChat: 1" in block
    assert "if (path === '/api/chat/suggestions') return d.chat_suggestions;" in JS
    assert "var DEMO_ALLOWED_PATHS = ['/api/demo-data'];" in JS


async def test_demo_twin_has_the_route_shape():
    from starlette.requests import Request

    req = Request({"type": "http", "method": "GET", "path": "/api/demo-data", "headers": []})
    data = json.loads((await server.api_demo_data(req)).body)["dashboard"]
    items = data["chat_suggestions"]["suggestions"]
    assert 3 <= len(items) <= 5 and all(set(q) == {"sk", "en"} for q in items)
    assert data["patients"]["chat_enabled"] is True


def test_chat_states_have_shapes_not_only_colour():
    assert ".chat-cite.is-unverified { border-style: dashed; }" in CSS
    start = JS.index("function chatCitationChip(")
    assert "state-mark progress" in JS[start : start + 1500]
    assert "state-mark attention" in JS[JS.index("function chatError(") :][:600]


def test_dashboard_csp_is_unchanged():
    assert server._STRICT_DASHBOARD_CSP == (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' "
        "https://accounts.google.com https://apis.google.com; "
        "script-src-elem 'self' "
        "https://accounts.google.com https://apis.google.com; "
        "script-src-attr 'none'; "
        "style-src 'self' 'unsafe-inline' https://accounts.google.com; "
        "style-src-elem 'self' https://accounts.google.com; "
        "style-src-attr 'unsafe-inline'; "
        "font-src 'self' data:; "
        "img-src 'self' data: https:; "
        "connect-src 'self' "
        "https://accounts.google.com https://oauth2.googleapis.com "
        "https://www.googleapis.com; "
        "frame-src https://accounts.google.com https://content.googleapis.com "
        "https://drive.google.com https://docs.google.com; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self' https://accounts.google.com; "
        "upgrade-insecure-requests"
    )
