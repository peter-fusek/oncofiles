"""Dashboard bug reports never publish patient data (#621)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import server

PAYLOAD = {
    "title": "Chart broken",
    "description": "the CEA chart is empty",
    "page_url": "https://oncofiles.com/dashboard?patient=secret-slug#labs",
    "page_state": '{"patient": "Jana Novakova", "CEA": 41.7}',
    "screenshot": "iVBORw0KGgoAAAANSUhEUg==",
    "console_errors": "TypeError: x is undefined",
    "user_agent": "test",
}


def _request():
    req = MagicMock()
    req.json = AsyncMock(return_value=PAYLOAD)
    return req


class _Client:
    def __init__(self, private: bool):
        self.calls = []
        self.private = private

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        self.calls.append(("GET", url, None))
        return MagicMock(status_code=200, json=lambda: {"private": self.private})

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, json))
        return MagicMock(status_code=201, json=lambda: {"html_url": "https://x/1"})

    async def put(self, *a, **k):  # contents upload must never happen
        raise AssertionError("screenshot upload attempted")


async def _call(private: bool, repo: str = "owner/private-bugs"):
    client = _Client(private)
    with (
        patch.object(server, "_check_dashboard_auth", return_value=None),
        patch("oncofiles.config.GITHUB_TOKEN", "t"),
        patch("oncofiles.config.BUG_REPORT_REPO", repo),
        patch("httpx.AsyncClient", lambda **kw: client),
    ):
        resp = await server.api_bug_report(_request())
    return resp, client


async def test_public_target_repo_is_refused_and_nothing_is_posted():
    resp, client = await _call(private=False)
    assert resp.status_code == 503
    assert [c[0] for c in client.calls] == ["GET"]


async def test_private_repo_gets_no_screenshot_page_state_or_query():
    resp, client = await _call(private=True)
    assert resp.status_code == 200
    posted = next(c[2] for c in client.calls if c[0] == "POST")
    text = json.dumps(posted)
    assert "Jana Novakova" not in text and "41.7" not in text  # page state dropped
    assert "iVBORw0K" not in text  # screenshot dropped
    assert "secret-slug" not in text and "/dashboard" in text  # path only


async def test_unconfigured_repo_disables_reports():
    with (
        patch("oncofiles.config.GITHUB_TOKEN", "t"),
        patch("oncofiles.config.BUG_REPORT_REPO", ""),
        patch.object(server, "_check_dashboard_auth", return_value=None),
    ):
        resp = await server.api_bug_report(_request())
    assert resp.status_code == 503


@pytest.mark.parametrize("field", ["page_state:", "screenshot:", "location.href"])
def test_dashboard_never_sends_patient_data(field):
    from pathlib import Path

    js = (Path(server.__file__).parent / "dashboard.js").read_text(encoding="utf-8")
    payload = js[js.index("var payload = {") : js.index("};", js.index("var payload = {"))]
    assert field not in payload
