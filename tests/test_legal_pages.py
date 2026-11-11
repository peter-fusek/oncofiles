"""Legal pages say what the service actually does (Nov 2026)."""

from __future__ import annotations

import pytest

from oncofiles import server


@pytest.fixture(autouse=True)
def _fresh():
    server._PRIVACY_HTML = None
    server._TERMS_HTML = None
    yield
    server._PRIVACY_HTML = None
    server._TERMS_HTML = None


async def _text(route) -> str:
    return (await route(None)).body.decode()


async def test_privacy_matches_the_service():
    html = await _text(server.privacy_policy)
    assert "local AI model" in html and "Slovakia" in html
    assert "never permanently deletes" in html
    assert "only the folder you choose" in html and "access to your Google" in html
    assert "Gmail and Google Calendar</strong> — not used" in html
    assert "Google Analytics" in html and "Resend" in html
    # #700: GA only on public pages, only after consent; never in the logged-in app
    assert "after you accept analytics cookies" in html
    assert "logged-in app does not load Google" in html
    assert "sent to Anthropic (Claude) for metadata" not in html  # retired claim


async def test_terms_match_the_service():
    html = await _text(server.terms_of_service)
    assert "Gmail, and Calendar" not in html
    assert "never permanently deletes files" in html
