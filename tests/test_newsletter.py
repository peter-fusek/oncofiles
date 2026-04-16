"""Tests for the newsletter subscribe endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def _clear_rate_limits():
    """Clear newsletter rate limits before each test."""
    from oncofiles.server import _newsletter_rate

    _newsletter_rate.clear()
    yield
    _newsletter_rate.clear()


def _make_request(body: dict, client_ip: str = "127.0.0.1") -> MagicMock:
    """Create a minimal mock request for the newsletter endpoint."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = client_ip

    async def _json():
        return body

    request.json = _json
    # Wire up app.state for DB access
    request.app = MagicMock()
    return request


@pytest.mark.usefixtures("_clear_rate_limits")
async def test_newsletter_valid_email(db):
    """Valid email signup returns ok: true."""
    from oncofiles.server import api_newsletter_subscribe

    request = _make_request({"email": "test@example.com"})
    request.app.state.fastmcp_server._lifespan_result = {"db": db}

    response = await api_newsletter_subscribe(request)
    assert response.status_code == 200
    body = response.body
    import json

    data = json.loads(body)
    assert data["ok"] is True

    # Verify row was inserted
    async with db.db.execute(
        "SELECT email, source, status FROM newsletter_subscribers WHERE email = ?",
        ("test@example.com",),
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0]["email"] == "test@example.com"
    assert rows[0]["source"] == "landing"
    assert rows[0]["status"] == "pending"


@pytest.mark.usefixtures("_clear_rate_limits")
async def test_newsletter_invalid_email(db):
    """Invalid email returns 400."""
    from oncofiles.server import api_newsletter_subscribe

    request = _make_request({"email": "not-an-email"})
    request.app.state.fastmcp_server._lifespan_result = {"db": db}

    response = await api_newsletter_subscribe(request)
    assert response.status_code == 400


@pytest.mark.usefixtures("_clear_rate_limits")
async def test_newsletter_missing_email(db):
    """Missing email field returns 400."""
    from oncofiles.server import api_newsletter_subscribe

    request = _make_request({})
    request.app.state.fastmcp_server._lifespan_result = {"db": db}

    response = await api_newsletter_subscribe(request)
    assert response.status_code == 400


@pytest.mark.usefixtures("_clear_rate_limits")
async def test_newsletter_duplicate_email(db):
    """Duplicate email signup is idempotent — returns ok: true."""
    from oncofiles.server import api_newsletter_subscribe

    request1 = _make_request({"email": "dupe@example.com"})
    request1.app.state.fastmcp_server._lifespan_result = {"db": db}
    resp1 = await api_newsletter_subscribe(request1)
    assert resp1.status_code == 200

    request2 = _make_request({"email": "dupe@example.com"})
    request2.app.state.fastmcp_server._lifespan_result = {"db": db}
    resp2 = await api_newsletter_subscribe(request2)
    assert resp2.status_code == 200

    # Only one row in DB
    async with db.db.execute(
        "SELECT COUNT(*) as cnt FROM newsletter_subscribers WHERE email = ?",
        ("dupe@example.com",),
    ) as cursor:
        rows = await cursor.fetchall()
    assert rows[0]["cnt"] == 1


@pytest.mark.usefixtures("_clear_rate_limits")
async def test_newsletter_cors_header(db):
    """Response includes Access-Control-Allow-Origin header."""
    from oncofiles.server import api_newsletter_subscribe

    request = _make_request({"email": "cors@example.com"})
    request.app.state.fastmcp_server._lifespan_result = {"db": db}

    response = await api_newsletter_subscribe(request)
    assert response.headers.get("access-control-allow-origin") == "*"


@pytest.mark.usefixtures("_clear_rate_limits")
async def test_newsletter_notification_noop_when_no_env(db, monkeypatch):
    """Notification helper is a silent no-op when RESEND_API_KEY is unset."""
    from oncofiles.server import _notify_new_subscriber

    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("NOTIFY_FROM_EMAIL", raising=False)
    monkeypatch.delenv("NOTIFY_TO_EMAIL", raising=False)

    # Should return without raising and without making any HTTP call.
    await _notify_new_subscriber("x@y.com", "test")


@pytest.mark.usefixtures("_clear_rate_limits")
async def test_newsletter_notification_fires_on_new_row(db, monkeypatch):
    """A new subscription schedules a notification; duplicates do not."""
    import asyncio

    from oncofiles import server

    calls: list[tuple[str, str]] = []

    async def _fake_notify(email: str, source: str) -> None:
        calls.append((email, source))

    monkeypatch.setattr(server, "_notify_new_subscriber", _fake_notify)

    request = _make_request({"email": "notify@example.com", "source": "test"})
    request.app.state.fastmcp_server._lifespan_result = {"db": db}
    await server.api_newsletter_subscribe(request)

    # Give the fire-and-forget task a tick to run
    await asyncio.sleep(0)
    assert calls == [("notify@example.com", "test")]

    # Duplicate submission should NOT trigger another notification
    request2 = _make_request({"email": "notify@example.com", "source": "test"})
    request2.app.state.fastmcp_server._lifespan_result = {"db": db}
    await server.api_newsletter_subscribe(request2)
    await asyncio.sleep(0)
    assert calls == [("notify@example.com", "test")]
