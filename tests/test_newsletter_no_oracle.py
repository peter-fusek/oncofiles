"""The waitlist is not a membership oracle (#879).

Nobody may learn from a public route whether an address is on the waitlist,
and no e-mail address may travel in a URL the landing builds. The only public
waitlist route is ``POST /api/newsletter/subscribe``; it answers identically
for a new and an already-subscribed address.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"
LANDING = (SRC / "landing.html").read_text(encoding="utf-8")
# Every browser-side source the app serves (landing, dashboard, demo, atlas).
CLIENT_FILES = sorted(
    p for p in SRC.rglob("*") if p.suffix in {".html", ".js"} and "__pycache__" not in p.parts
)


@pytest.fixture
def _clear_rate_limits():
    from oncofiles.server import _newsletter_rate

    _newsletter_rate.clear()
    yield
    _newsletter_rate.clear()


def _subscribe_request(db, email: str, client_ip: str = "127.0.0.1") -> MagicMock:
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = client_ip

    async def _json():
        return {"email": email, "source": "landing"}

    request.json = _json
    request.app = MagicMock()
    request.app.state.fastmcp_server._lifespan_result = {"db": db}
    return request


# ── routes ───────────────────────────────────────────────────────────────


def test_only_public_waitlist_route_is_post_subscribe():
    from oncofiles import server

    waitlist = {
        (r.path, frozenset(r.methods or ()))
        for r in server.mcp._additional_http_routes
        if "newsletter" in getattr(r, "path", "") or "waitlist" in getattr(r, "path", "")
    }
    assert waitlist == {("/api/newsletter/subscribe", frozenset({"POST"}))}


def test_check_route_is_gone():
    from oncofiles import server

    paths = {getattr(r, "path", "") for r in server.mcp._additional_http_routes}
    assert "/api/newsletter/check" not in paths
    assert not hasattr(server, "api_newsletter_check")
    assert not hasattr(server, "_newsletter_check_rate")


def test_no_code_reads_the_subscriber_list():
    """No module SELECTs from newsletter_subscribers (only the INSERT exists).

    A read path is how a membership oracle comes back; one that is genuinely
    needed must be admin-only and land here as a reviewed exception.
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"SELECT[^;\"']*?FROM\s+newsletter_subscribers", text, re.I | re.S):
            offenders.append(f"{path.relative_to(SRC)}: {m.group(0)[:60]!r}")
    assert not offenders, offenders


# ── subscribe answers the same for new and existing ─────────────────────


@pytest.mark.usefixtures("_clear_rate_limits")
async def test_subscribe_response_identical_for_new_and_existing(db, monkeypatch):
    from oncofiles import server

    async def _noop(*_a, **_k):
        return None

    # No outgoing mail in tests (names differ before/after #878).
    for name in ("_notify_new_subscriber", "_send_subscriber_welcome"):
        monkeypatch.setattr(server, name, _noop, raising=False)

    new = await server.api_newsletter_subscribe(_subscribe_request(db, "first@example.com"))
    again = await server.api_newsletter_subscribe(_subscribe_request(db, "first@example.com"))
    other = await server.api_newsletter_subscribe(_subscribe_request(db, "second@example.com"))

    for resp in (again, other):
        assert resp.status_code == new.status_code == 200
        assert resp.body == new.body
        assert dict(resp.headers) == dict(new.headers)
    # The body never carries membership words.
    assert not re.search(rb"subscribed|already|exist|new", new.body, re.I)


# ── landing / client JS ──────────────────────────────────────────────────


def test_landing_has_no_membership_precheck():
    assert "newsletter/check" not in LANDING
    assert "already on the list" not in LANDING.lower()
    assert "Už ste na zozname" not in LANDING


@pytest.mark.parametrize("path", CLIENT_FILES, ids=lambda p: str(p.relative_to(SRC)))
def test_no_client_code_calls_newsletter_check(path):
    assert "newsletter/check" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path",
    [p for p in CLIENT_FILES if p.name in {"landing.html", "dashboard.js", "dashboard.html"}],
    ids=lambda p: p.name,
)
def test_no_email_in_any_url_built_by_client_js(path):
    """No `?email=` / `&email=` query and no fetch URL concatenated with an email."""
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"[?&]e-?mail=", text, re.I), "e-mail address in a query string"
    for m in re.finditer(r"fetch\(\s*([^,)]*)", text):
        url_expr = m.group(1)
        assert "email" not in url_expr.lower(), f"fetch URL built from an e-mail: {url_expr!r}"


def test_landing_posts_email_in_body_only():
    start = LANDING.index("function nlSubmit(form)")
    body = LANDING[start : LANDING.index("\n    }\n", start)]
    fetches = re.findall(r"fetch\(\s*'([^']*)'", body)
    assert fetches == ["/api/newsletter/subscribe"]
    assert "method: 'POST'" in body
    assert "JSON.stringify({ email: email" in body
