"""Landing page + public demo honesty locks (Nov 2026 refresh, #605).

The landing page is public marketing copy for a product that handles real
patients' records. These tests pin the claims that were false or unsafe
before the refresh so they cannot creep back in.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from starlette.requests import Request

from oncofiles.server import _load_landing_html, api_demo_data, demo_dashboard

SRC = Path(__file__).parent.parent / "src" / "oncofiles"
LANDING = (SRC / "landing.html").read_text(encoding="utf-8")
MIN_SYNTHETIC_YEAR = 2027  # CLAUDE.md: invented fixtures use year 2027 or later


def _json_ld_blocks(html: str) -> list:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    return [json.loads(b) for b in blocks]


def test_landing_has_no_redaction_placeholders():
    assert "_REDACTED]" not in LANDING


def test_landing_does_not_name_the_founders_wife():
    # Maintainer decision: the founder story says "manželka" / "my wife" only.
    assert "Erik" not in LANDING


@pytest.mark.parametrize(
    "claim",
    [
        "každých 5 minút",
        "every 5 minutes",
        "Skenuje Gmail aj Kalendár",
        "Všetko zostáva vo vašom Google Drive",
        "Everything stays in your Google Drive",
        "83 nástrojov",
        "Claude Vision",
        "[MEDICATION",
        "[BIOMARKER",
        "200 dokumentov",
        "200 documents",
    ],
)
def test_landing_has_no_retired_claims(claim):
    assert claim not in LANDING


def test_landing_has_no_inline_event_handlers():
    assert not re.search(r"\son[a-z]+\s*=", LANDING), "use data-action + the page dispatcher"


def test_landing_has_one_h1_and_waitlist_cta():
    assert len(re.findall(r"<h1[\s>]", LANDING)) == 1
    assert "spúšťame v novembri 2026" in LANDING
    assert 'data-nl-source="waitlist"' in LANDING


def test_landing_json_ld_is_valid_and_says_500():
    blocks = _json_ld_blocks(_load_landing_html())
    assert blocks, "JSON-LD missing"
    flat = json.dumps(blocks)
    types = {item["@type"] for block in blocks for item in block}
    assert {"SoftwareApplication", "FAQPage"} <= types
    assert "500 documents" in flat
    assert "200 documents" not in flat


def test_every_visible_bilingual_element_has_both_languages():
    sk = len(re.findall(r"\sdata-sk=", LANDING))
    en = len(re.findall(r"\sdata-en=", LANDING))
    assert sk == en


def _req(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


async def test_demo_route_marks_demo_mode():
    """#605: /demo used to inject an inline <script> by replacing the first
    bare `<script>` tag — dashboard.html has none since #525, so the demo
    silently showed the login gate."""
    resp = await demo_dashboard(_req("/demo"))
    html = resp.body.decode()
    assert '<html data-demo="1"' in html
    js = (SRC / "dashboard.js").read_text(encoding="utf-8")
    assert "getAttribute('data-demo') === '1'" in js


async def test_demo_data_is_synthetic():
    resp = await api_demo_data(_req("/api/demo-data"))
    body = resp.body.decode()
    assert "REDACTED" not in body
    years = {int(y) for y in re.findall(r'"(\d{4})-\d\d-\d\d', body)}
    assert years, "demo payload should carry dates"
    assert min(years) >= MIN_SYNTHETIC_YEAR, "demo dates must be synthetic (2027+)"


def test_dashboard_demo_copy_is_honest():
    html = (SRC / "dashboard.html").read_text(encoding="utf-8")
    assert "demo with real data" not in html
    assert "s reálnymi dátami" not in html
    assert "158" not in html


# ── Demo mode never reaches an authenticated endpoint ─────────────────

DASHBOARD_JS = (SRC / "dashboard.js").read_text(encoding="utf-8")


def test_demo_mode_fetch_chokepoint():
    """In demo mode dashboard.js replaces window.fetch with a wrapper that
    refuses every same-origin request except /api/demo-data. It must be
    installed before any code that can call fetch (init, timers, listeners)."""
    assert "var DEMO_ALLOWED_PATHS = ['/api/demo-data'];" in DASHBOARD_JS
    chokepoint = DASHBOARD_JS.index("window.fetch = function (input, init)")
    first_call = min(
        DASHBOARD_JS.index(m)
        for m in ("fetch('/", "apiFetch('/", "coFetch('/")
        if m in DASHBOARD_JS
    )
    assert chokepoint < first_call
    assert chokepoint < DASHBOARD_JS.index("(function init()")


def test_only_demo_data_is_reachable_in_demo_mode():
    """Inventory of literal endpoints fetched by dashboard.js: the allowlist
    of the demo chokepoint may contain exactly /api/demo-data."""
    endpoints = set(re.findall(r"(?:fetch|apiFetch|coFetch)\(\s*'(/[a-z\-/]+)", DASHBOARD_JS))
    assert "/api/demo-data" in endpoints
    start = DASHBOARD_JS.index("DEMO_ALLOWED_PATHS = [")
    allowed = DASHBOARD_JS[start : DASHBOARD_JS.index("];", start)]
    assert {e for e in endpoints if f"'{e}'" in allowed} == {"/api/demo-data"}
    assert len(endpoints) > 10, "inventory regex stopped matching fetch calls"


@pytest.mark.parametrize(
    "guard",
    [
        "if (window.DEMO_MODE) return loadDemoData();",
        "async function loadPatients() {\n      if (window.DEMO_MODE) return;",
        "if (!section || window.DEMO_MODE) return;",
        "var detailPromise = window.DEMO_MODE",
    ],
)
def test_demo_guards_on_entry_points(guard):
    # Belt and braces next to the chokepoint: what a demo visitor can trigger
    # (filter pills and Refresh -> refresh(), patient load, curated poll, doc
    # detail) short-circuits to demo data instead of producing requests.
    assert guard in DASHBOARD_JS


async def test_demo_data_needs_no_auth_but_api_documents_does():
    from unittest.mock import patch

    from oncofiles.server import api_documents

    with (
        patch("oncofiles.server.MCP_BEARER_TOKEN", "test-secret"),
        patch("oncofiles.server.MCP_TRANSPORT", "streamable-http"),
    ):
        demo = await api_demo_data(_req("/api/demo-data"))
        assert demo.status_code == 200
        docs = await api_documents(_req("/api/documents"))
        assert docs.status_code == 401


async def test_og_image_png_headers():
    from oncofiles.server import og_image_png

    resp = await og_image_png(_req("/og-image.png"))
    assert resp.media_type == "image/png"
    assert "max-age" in resp.headers.get("cache-control", "")
    assert resp.body[:8] == b"\x89PNG\r\n\x1a\n"


# ── Demo honesty + i18n (#605 visual review) ──────────────────────────

DASHBOARD_HTML = (SRC / "dashboard.html").read_text(encoding="utf-8")
DASHBOARD_CSS = (SRC / "dashboard.css").read_text(encoding="utf-8")


async def test_demo_payload_has_no_cloud_pipeline_telemetry():
    """The demo used to show a prompt log with per-call tokens and USD cost
    for a cloud model. Processing is local now, so the payload must not
    carry that story."""
    resp = await api_demo_data(_req("/api/demo-data"))
    data = json.loads(resp.body)
    assert "prompt_log" not in data
    assert "analytics" not in data
    body = resp.body.decode()
    assert "claude-haiku" not in body
    assert "estimated_cost_usd" not in body


def test_demo_hides_operator_panels_and_shows_processing_note():
    for panel in (
        "sync-history-section",
        "prompt-log-section",
        "analytics-section",
        "schedules-section",
        "manifest-section",
    ):
        assert f'id="{panel}"' in DASHBOARD_HTML, panel
        assert f"'{panel}'" in DASHBOARD_JS, f"{panel} not in the demo hide list"
    assert 'id="demo-processing-note"' in DASHBOARD_HTML
    assert "lokálny model" in DASHBOARD_HTML
    assert "renderPromptLog(data.prompt_log)" not in DASHBOARD_JS


def test_demo_doc_detail_has_id_and_date():
    # The detail renderer reads data.id and data.document_date; the demo
    # objects used to carry neither ("ID #undefined", "Dátum -").
    assert "detail.id = doc.id;" in DASHBOARD_JS
    assert "detail.document_date = detail.date || doc.date;" in DASHBOARD_JS
    assert "id: doc.id, document_date: doc.date," in DASHBOARD_JS
    assert "All data stays in your Google Drive" not in DASHBOARD_JS


class _TextWithoutI18n(HTMLParser):
    """Collect visible text nodes whose nearest element chain has no data-sk."""

    VOID = {"br", "img", "input", "meta", "link", "hr"}

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.offenders: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        a = dict(attrs)
        covered = "data-sk" in a or (self.stack and self.stack[-1][1])
        skip = "demo-chat-tool" in (a.get("class") or "")  # tool calls are code
        self.stack.append((tag, bool(covered or skip)))

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def handle_data(self, data):
        if data.strip() and not (self.stack and self.stack[-1][1]):
            self.offenders.append(data.strip()[:60])


@pytest.mark.parametrize(
    "block_id,terminator",
    [
        ("demo-processing-note", "<!--"),
        ("demo-conversations", "<!--"),
        ("demo-banner", "</div>"),
    ],
)
def test_demo_blocks_are_bilingual(block_id, terminator):
    start = DASHBOARD_HTML.index(f'id="{block_id}"')
    start = DASHBOARD_HTML.rindex("<", 0, start)
    end = DASHBOARD_HTML.index(terminator, start)
    parser = _TextWithoutI18n()
    parser.feed(DASHBOARD_HTML[start:end])
    assert not parser.offenders, parser.offenders


def test_dynamic_dashboard_labels_are_bilingual():
    # Funnel rows and remedy rows are built in JS; they must carry both
    # languages so applyDashLang() can switch them.
    assert "setI18n(label, st.sk, st.label);" in DASHBOARD_JS
    assert "setI18n(lbl, g.sk, g.label);" in DASHBOARD_JS
    assert "{ label: 'OCR', sk: 'OCR'" in DASHBOARD_JS
    assert 'data-sk="Spracovanie dokumentov"' in DASHBOARD_HTML


def test_dashboard_phone_layout_does_not_widen_the_page():
    # #605: at 375 px the header account row, sync rows and analytics cards
    # pushed scrollWidth to 483. The phone block keeps them inside the viewport.
    block = DASHBOARD_CSS[DASHBOARD_CSS.index("@media (max-width: 600px)") :]
    for rule in (
        ".header-top .header-account { margin-left: 0;",
        ".sync-row { flex-wrap: wrap;",
        "#analytics-cards { grid-template-columns: repeat(2, 1fr) !important; }",
        ".remedy-row { flex-wrap: wrap;",
    ):
        assert rule in block, rule
