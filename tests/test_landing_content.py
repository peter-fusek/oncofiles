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


async def test_demo_route_serves_the_standalone_demo():
    """#605 + Nov 2026 redesign: /demo is its own page (demo.html + demo.js),
    not the operator dashboard in demo mode. It must not load dashboard.js,
    so it cannot reach an authenticated endpoint."""
    resp = await demo_dashboard(_req("/demo"))
    html = resp.body.decode()
    assert 'data-demo="1"' in html
    assert '<script src="/demo.js" defer></script>' in html
    assert "/dashboard.js" not in html
    assert "{{VERSION}}" not in html
    assert not re.search(r"\son[a-z]+\s*=", html), "no inline event handlers"
    # dashboard.js still honours the flag if anyone serves it with data-demo.
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


# ── Standalone demo (Nov 2026 redesign) ───────────────────────────────

DEMO_HTML = (SRC / "demo.html").read_text(encoding="utf-8")
DEMO_JS = (SRC / "demo.js").read_text(encoding="utf-8")
DEMO_CSS = (SRC / "demo.css").read_text(encoding="utf-8")


def test_demo_js_only_fetches_demo_data():
    assert "var DEMO_ALLOWED_PATHS = ['/api/demo-data'];" in DEMO_JS
    fetched = set(re.findall(r"fetch\(\s*'([^']+)'", DEMO_JS))
    assert fetched == {"/api/demo-data"}
    # The guard wraps fetch before the single data request is made.
    assert DEMO_JS.index("window.fetch = function") < DEMO_JS.index("fetch('/api/demo-data')")
    assert "innerHTML" not in DEMO_JS.replace("there is no innerHTML", "")


def test_demo_page_is_bilingual():
    parser = _TextWithoutI18n()
    body = DEMO_HTML[DEMO_HTML.index("<body>") :]
    parser.feed(body)
    # Brand, language switch label, invented name and version need no translation.
    allowed = {
        "onco",
        "files",
        "EN",
        "Jana Testová",
        "v{{VERSION}}",
        "ChatGPT",
        "Claude",
        "Gemini CLI",
        "+",
        "↑",
        "Oncofiles",
        "~/zdravie",
        "1 MCP server: oncofiles",
        "Jana T.",
        "J",
        "9:41",
        "☰",
        "✎",
        "— ~/zdravie — 96×28",
    }
    assert set(parser.offenders) <= allowed, parser.offenders


async def test_demo_assets_are_served_with_types():
    from oncofiles.server import demo_css, demo_js

    css = await demo_css(_req("/demo.css"))
    js = await demo_js(_req("/demo.js"))
    assert css.media_type.startswith("text/css")
    assert js.media_type.startswith("application/javascript")


async def test_every_demo_value_points_at_its_own_source_line():
    """The demo's promise is provenance: each value's (doc, page, line) must
    resolve to a line in that document that shows exactly this value."""
    data = json.loads((await api_demo_data(_req("/api/demo-data"))).body)
    docs = {d["id"]: d for d in data["documents"]}
    seen = 0
    for series in data["series"]:
        for p in series["points"]:
            doc = docs[p["doc"]]
            page = doc["pages"][p["page"] - 1]
            line = page["lines"][p["line"] - 1]
            assert line["n"] == p["line"]
            assert line["cells"][0] == series["code"]
            assert line["cells"][1] == p["value_text"]
            assert p["state"] in {"machine", "human", "unverified"}
            seen += 1
    assert seen >= 12
    states = {p["state"] for s in data["series"] for p in s["points"]}
    assert states == {"machine", "human", "unverified"}
    assert data["patient"]["invented"] is True


def test_demo_phone_layout_and_motion():
    assert "@media (max-width: 640px)" in DEMO_CSS
    assert "prefers-reduced-motion" in DEMO_CSS
    assert "prefers-color-scheme: dark" in DEMO_CSS


# ── Landing design iteration 2 ────────────────────────────────────────


def test_landing_has_no_oncoteam():
    assert "oncoteam" not in LANDING.lower()


def test_landing_avoids_template_tells():
    style = LANDING[LANDING.index("<style>") : LANDING.index("</style>")]
    assert "border-left" not in style, "no accent stripes on boxes"
    assert not re.search(r"inset\s+\d+px\s+0\s+0", style), "no inset side stripes"
    assert "text-transform: uppercase" not in style
    assert "gradient" not in style
    assert "eyebrow" not in LANDING


def test_landing_hero_pile_animation():
    hero = LANDING[LANDING.index('id="stage"') : LANDING.index('class="stage-cap"')]
    for name in (
        "Preposl.: výsledky",
        "IMG_20270118_lab.jpg",
        "vysledky(1).pdf",
        "CT_brucho_final_v2.pdf",
        "dokument(3).pdf",
    ):
        assert name in hero, name
    assert hero.count('class="paper ') == 5
    assert len(re.findall(r'class="gd-row" data-k="\d"', hero)) == 5
    assert "OCR + AI" in hero
    style = LANDING[LANDING.index("<style>") : LANDING.index("</style>")]
    assert "@keyframes fly" in style and "@keyframes land" in style
    assert "prefers-reduced-motion: reduce" in style


def test_landing_keeps_real_photos_with_honest_caption():
    assert 'src="/assets/pile/02.jpg"' in LANDING
    assert 'src="/assets/pile/04.jpg"' in LANDING
    assert (
        "lokálny model" in LANDING[LANDING.index('id="photos"') : LANDING.index('id="provenance"')]
    )


def test_pile_photos_are_served_as_crops_only():
    from oncofiles.server import _PILE_PHOTO_DIR, _PILE_PHOTO_NAMES

    assert set(_PILE_PHOTO_NAMES) == {"02", "04"}
    for name in _PILE_PHOTO_NAMES.values():
        assert "crop" in name
        assert (_PILE_PHOTO_DIR / name).exists(), name


def test_drive_mock_uses_invented_patient():
    folder = LANDING[LANDING.index('id="folder"') : LANDING.index('id="privacy"')]
    assert "Jana Testová" in folder
    assert "README.md" in folder
    assert ".md</span>" in folder
    names = re.findall(r"(\d{8})_JanaTestova_", LANDING)
    assert names and all(int(n[:4]) >= MIN_SYNTHETIC_YEAR for n in names)


# ── Iteration 3: "ask in your own words" chat frames ──────────────────

CLIENTS = {"chatgpt", "claude", "gemini-cli"}


def _frames(html: str) -> list[tuple[str, str]]:
    return re.findall(r'class="cw [^"]*"[^>]*data-client="([^"]+)" data-verified="([^"]*)"', html)


@pytest.mark.parametrize("page", ["landing", "demo"])
def test_every_client_frame_carries_a_verified_flag(page):
    """A client is shown as working only after an E2E check: each frame has
    exactly one data-verified flag (true/false), and an unverified frame
    shows the "pripravujeme / coming soon" label via CSS."""
    html = LANDING if page == "landing" else DEMO_HTML
    frames = _frames(html)
    assert {c for c, _ in frames} == CLIENTS
    assert len(frames) == len(CLIENTS)
    for client, flag in frames:
        assert flag in {"true", "false"}, (client, flag)
    css = LANDING if page == "landing" else DEMO_CSS
    assert '[data-verified="true"] .soon { display: none; }' in css
    assert html.count('class="soon"') >= len(CLIENTS)


def test_landing_chat_source_chips_resolve_to_provenance_lines():
    chips = re.findall(r'data-action="showSource\|(\w+)"', LANDING)
    assert len(chips) >= 10
    sheet = LANDING[LANDING.index('<div class="sheet"') : LANDING.index('<div class="app"')]
    lines = set(re.findall(r'class="ln[^"]*" data-src="(\w+)"', sheet))
    assert set(chips) <= lines, set(chips) - lines


def test_chat_examples_only_use_real_mcp_tools():
    from oncofiles.server import mcp

    registered = {
        k.partition(":")[2].partition("@")[0]
        for k in mcp._local_provider._components
        if k.startswith("tool:")
    }
    ask = LANDING[LANDING.index('id="ask"') : LANDING.index('id="privacy"')]
    used = set(re.findall(r"Oncofiles · (\w+)", ask))
    used |= set(re.findall(r'<span class="t-ok">✓</span> (\w+)', ask))
    used |= set(re.findall(r"toolLine\('(\w+)'", DEMO_JS))
    used |= set(re.findall(r"tool\('(\w+)'", DEMO_JS))
    assert used, "no tool names found"
    assert used <= registered, used - registered


def test_demo_chat_chips_open_the_document_line():
    assert "'openSource|' + docId + '|' + line" in DEMO_JS
    assert "renderChats();" in DEMO_JS


@pytest.mark.parametrize("page", ["landing", "demo"])
def test_trademark_line_present(page):
    html = LANDING if page == "landing" else DEMO_HTML
    assert "ChatGPT je ochranná známka OpenAI" in html
    assert "Oncofiles is not affiliated with these companies" in html


def test_no_external_images_on_public_pages():
    """Images (incl. any client logos) are self-hosted, never hotlinked."""
    for name, text in (("landing", LANDING), ("demo", DEMO_HTML), ("demo.css", DEMO_CSS)):
        for src in re.findall(r'<img[^>]+src="([^"]+)"', text):
            assert src.startswith("/"), (name, src)
        assert not re.search(r"url\(\s*['\"]?https?:", text), name


def test_landing_chat_carousel_starts_on_claude():
    """Peter (Nov 2026): the chat previews rotate Claude -> ChatGPT -> Gemini
    CLI and always start on Claude; reduced motion means no rotation."""
    tabs = re.findall(r'role="tab" id="tab-(\w+)"[^>]*aria-selected="(true|false)"', LANDING)
    assert tabs[0] == ("claude", "true")
    assert [t for t, sel in tabs if sel == "true"] == ["claude"]
    assert 'class="cw cw-warm is-active" id="chat-claude"' in LANDING
    assert LANDING.count(' is-active" id="chat-') == 1
    assert '<div class="chat-stage" id="chat-stage" aria-live="off">' in LANDING
    assert "var ROTATE_ORDER = ['claude', 'gpt', 'gemini'];" in LANDING
    assert "if (!stageEl || !tablist || reduceMotion) return;" in LANDING
    assert "visibilitychange" in LANDING
    assert not re.search(r"\son[a-z]+\s*=", LANDING)


# ── #700: analytics only with consent on public pages, never on the dashboard ──

CONSENT_JS = (SRC / "consent.js").read_text(encoding="utf-8")


def test_dashboard_has_no_google_analytics():
    from oncofiles.server import _csp_for_path

    for name in ("dashboard.html", "dashboard.js"):
        text = (SRC / name).read_text(encoding="utf-8")
        assert "gtag" not in text, name
        assert "googletagmanager" not in text, name
    assert not (SRC / "dashboard-gtag.js").exists()
    for path in ("/dashboard", "/dashboard/x"):
        csp = _csp_for_path(path)
        assert "googletagmanager.com" not in csp
        assert "google-analytics.com" not in csp


@pytest.mark.parametrize("page", ["landing", "demo"])
def test_public_pages_load_no_analytics_before_consent(page):
    html = LANDING if page == "landing" else DEMO_HTML
    assert not re.search(r"<script[^>]*googletagmanager", html)
    assert "gtag('config'" not in html and "gtag('js'" not in html
    assert '<script src="/consent.js" defer></script>' in html
    # banner markup with two equal choices, privacy link, footer re-open link
    assert re.search(r'id="cookie-banner"[^>]*\shidden>', html), "banner must start hidden"
    assert 'data-consent="accept"' in html and 'data-consent="decline"' in html
    assert 'data-sk="Prijať analytické cookies"' in html and 'data-sk="Odmietnuť"' in html
    assert 'href="/privacy"' in html
    assert 'data-consent="open"' in html and "Nastavenia cookies" in html


def test_consent_js_injects_gtag_only_after_accept():
    body = CONSENT_JS
    start = body.index("function loadAnalytics()")
    end = body.index("function banner()")
    # the only place that mentions the Google tag host is inside loadAnalytics
    assert body.count("googletagmanager.com") == 1
    assert start < body.index("googletagmanager.com") < end
    # loadAnalytics() is called only from accept() and for a stored "granted"
    calls = [m.start() for m in re.finditer(r"loadAnalytics\(\);", body)]
    assert len(calls) == 2
    accept = body.index("function accept()")
    assert accept < calls[0] < body.index("function decline()")
    assert "if (choice === 'granted') loadAnalytics();" in body
    for opt in (
        "anonymize_ip: true",
        "allow_google_signals: false",
        "allow_ad_personalization_signals: false",
    ):
        assert opt in body
    assert "page_location: cleanUrl(window.location.href)" in body
    assert "x.origin + x.pathname" in body
    assert "innerHTML" not in body.replace("no innerHTML", "")


async def test_consent_js_route():
    from oncofiles.server import consent_js

    resp = await consent_js(_req("/consent.js"))
    assert resp.media_type.startswith("application/javascript")


# ── Visual mockups follow the language switch (EN shows English) ──────

_SK_IN_EN = re.compile(
    r"[áäčďéíĺľňóôŕšťúýž]|JanaTestova|NemPrikladov|Testov|Laborat[oó]ri[aá]|Zobrazov", re.I
)
# Language-neutral leaf text allowed in the mockups without data-sk/data-en:
# codes, units, numbers without a decimal comma, tool calls, chrome glyphs.
_NEUTRAL = re.compile(
    r"^(\d+|[\d ]+(kB|g/L)|[\d–→ ]+(g/L|×10⁹/L)?( ↑)?|\d+ g/L ↑|\d+ ×10⁹/L ↑"
    r"|WBC|RBC|HGB|HCT|PLT|CEA|g/L|L/L|µg/L|×10⁹/L|×10¹²/L"
    r"|IMG_20270118_lab\.jpg|OCR \+ AI|J|PDF|README\.md|9:41"
    r"|ChatGPT|Claude|Gemini CLI|Oncofiles|Oncofiles · \w+|\w+|\(oncofiles\)"
    r"|limit: 5|document_id: \d+|codes: [A-Z, ]+|oncofiles: fact_series\(code=\"HGB\"\)"
    r"|[+>›↑☰✎✓,])$"
)


def _mock_regions(html: str) -> list[str]:
    starts = [
        'id="stage"',
        'class="sheet"',
        'class="app" id="prov"',
        'class="folder-shot"',
        'class="readme"',
        'class="chats"',
        'class="transcript"',
    ]
    out = []
    for marker in starts:
        i = html.rindex("<", 0, html.index(marker))
        open_divs: list[str] = []
        j = i
        for m in re.finditer(r"<(?P<close>/?)div\b[^>]*>", html[i:]):
            if m.group("close"):
                open_divs.pop()
            else:
                open_divs.append(m.group(0))
            if not open_divs:
                j = i + m.end()
                break
        out.append(html[i:j])
    return out


def test_landing_mockups_have_no_untranslated_text():
    """Hero pile + Drive mock, provenance sheet, folder mock, README and chat
    frames: every visible text node is either inside an element carrying
    data-sk/data-en or a language-neutral token (code, unit, number)."""
    for region in _mock_regions(LANDING):
        parser = _TextWithoutI18n()
        parser.feed(region)
        loose = [t for t in parser.offenders if not _NEUTRAL.match(t)]
        assert not loose, loose


def test_landing_english_variants_contain_no_slovak():
    """EN mode must not show Slovak filenames, folders, names or diacritics in
    the mockups (Peter Čapkovič in the team line is a real name)."""
    for region in _mock_regions(LANDING):
        for attr in re.findall(r'\sdata-en(?:-label|-title)?="([^"]*)"', region):
            assert not _SK_IN_EN.search(attr), attr


@pytest.mark.parametrize(
    "sk,en",
    [
        ("Preposl.: výsledky", "Fwd: results"),
        ("Laboratórium Príkladov", "Example Laboratory"),
        ("vysledky(1).pdf", "results(1).pdf"),
        ("CT_brucho_final_v2.pdf", "CT_abdomen_final_v2.pdf"),
        ("dokument(3).pdf", "document(3).pdf"),
        ("Imaging / Zobrazovanie", "Imaging"),
        ("Report / Správy", "Reports"),
        ("Labs / Laboratóriá", "Labs"),
        ("Jana Testová", "Jane Doe"),
        ("NEMOCNICA", "EXAMPLE"),
        ("PRÍKLADOV", "HOSPITAL"),
    ],
)
def test_hero_and_folder_mock_pairs(sk, en):
    assert f'data-sk="{sk}" data-en="{en}"' in LANDING


def test_mock_filenames_follow_the_convention_in_both_languages():
    std = re.compile(
        r"^(?P<date>\d{8})_(?P<patient>[A-Za-z]+)_(?P<inst>[A-Za-z]+)_(?P<cat>[A-Za-z]+)"
        r"_(?P<desc>[A-Za-z0-9]+)\.(?P<ext>pdf|jpg|md)$"
    )
    shared = ("date", "cat", "desc", "ext")
    pairs = re.findall(
        r'data-sk="(\d{8}_JanaTestova_[^"]+)" data-en="(\d{8}_JaneDoe_[^"]+)"', LANDING
    )
    assert len(pairs) >= 12
    for sk, en in pairs:
        ms, me = std.match(sk), std.match(en)
        assert ms and me, (sk, en)
        assert ms.group("patient", "inst") == ("JanaTestova", "NemPrikladov")
        assert me.group("patient", "inst") == ("JaneDoe", "ExampleHospital")
        # date, category token, English description and extension are shared
        assert ms.group(*shared) == me.group(*shared)
        assert int(ms.group("date")[:4]) >= MIN_SYNTHETIC_YEAR
    # every visible SK standard name has an EN twin (no bare SK filename left)
    shown = re.findall(r">(\d{8}_JanaTestova_[^<]*)</", LANDING)
    assert shown and all(f'data-sk="{m}" data-en=' in LANDING for m in shown)


def test_landing_language_switch_updates_labels_titles_and_restarts_hero():
    script = LANDING[LANDING.rindex("<script>") :]
    assert "data-' + l + '-label" in script
    assert "data-' + l + '-title" in script
    assert "restartStage();" in script[script.index("function applyLang") :]


def test_no_lock_in_callout_and_eu_first_hosting():
    block = LANDING[LANDING.index('id="no-lock-in"') :]
    block = block[: block.index("</div>")]
    assert "No lock-in" in block and "Žiadne viazanie" in block
    assert ".md" in block
    # honest about what is not in Drive
    assert "our database" in block and "našej databáze" in block
    assert "Slovakia (EU)" not in LANDING and "Slovensku (EÚ)" not in LANDING
    assert "in the EU (Slovakia)" in LANDING and "v EÚ (Slovensko)" in LANDING
    assert '<symbol id="g-eu"' in LANDING
    assert LANDING.count('<use href="#g-eu"/>') >= 2


async def test_demo_payload_has_english_twins():
    data = json.loads((await api_demo_data(_req("/api/demo-data"))).body)
    assert (data["patient"]["name"], data["patient"]["name_en"]) == ("Jana Testová", "Jane Doe")
    for doc in data["documents"]:
        assert "JaneDoe_ExampleHospital" in doc["filename_en"]
        swapped = doc["filename"].replace("JanaTestova_NemPrikladov", "JaneDoe_ExampleHospital")
        assert swapped == doc["filename_en"]
        assert not _SK_IN_EN.search(doc["institution_en"])
        for line in doc["pages"][0]["lines"]:
            if line.get("text"):
                assert line.get("text_en"), line
                assert not _SK_IN_EN.search(line["text_en"]), line["text_en"]
            if "cells" in line:
                en = line["cells_en"]
                assert len(en) == len(line["cells"])
                assert "," not in "".join(en), en
                if line.get("kind") != "th":
                    assert en[0] == line["cells"][0]
                    assert en[1] == line["cells"][1].replace(",", ".")


def test_demo_js_renders_english_twins():
    for call in (
        "loc(data.patient, 'name')",
        "loc(d, 'institution')",
        "loc(ln, 'text')",
        "loc(ln, 'cells')",
    ):
        assert call in DEMO_JS, call
    assert "loc(d, 'filename')" in DEMO_JS and "loc(c.latest, 'filename')" in DEMO_JS
    assert "lang === 'sk' ? 'NEMOCNICA' : 'EXAMPLE'" in DEMO_JS
    assert "v.replace(/,/g, '.')" in DEMO_JS
    assert 'data-sk="Jana T." data-en="Jane D."' in DEMO_HTML
    assert 'data-en=" — ~/health — 96×28"' in DEMO_HTML


# ── Roadmap (honest status labels) ─────────────────────────────────────

_DEVICES = ("Withings", "Apple Health", "Google Health Connect", "Garmin", "Strava")


def _roadmap() -> str:
    start = LANDING.index('<section class="block" id="roadmap">')
    return LANDING[start : LANDING.index("</section>", start)]


def _roadmap_column(status: str) -> str:
    block = _roadmap()
    start = block.index(f'data-status="{status}"')
    return block[start : block.index("</ul>", start)]


def test_roadmap_section_is_bilingual_with_three_statuses():
    block = _roadmap()
    assert 'href="#roadmap"' in LANDING
    assert [m for m in re.findall(r'data-status="([\w-]+)"', block)] == [
        "live",
        "in-progress",
        "planned",
    ]
    for sk, en in (("Funguje", "Live"), ("Rozpracované", "In progress"), ("Plánujeme", "Planned")):
        assert f'data-sk="{sk}" data-en="{en}"' in block
    parser = _TextWithoutI18n()
    parser.feed(block)
    assert not parser.offenders, parser.offenders
    # no images or logos in the roadmap: plain-text names only
    assert "<img" not in block and "<use" not in block


def test_roadmap_device_names_appear_only_under_planned():
    planned = _roadmap_column("planned")
    for name in _DEVICES:
        assert LANDING.count(name) == planned.count(name) >= 2, name  # SK + EN attrs


def test_roadmap_does_not_claim_live_hospital_connections():
    live, wip = _roadmap_column("live"), _roadmap_column("in-progress")
    assert "EHDS" not in live and "EHDS" in wip
    assert (
        "no hospital is connected yet" in wip and "žiadna nemocnica zatiaľ pripojená nie je" in wip
    )
    assert "FHIR R4" in live  # read/search/$everything shipped in #716 phase 3a


# ── Vision (puzzle) and "your dashboard, built on Oncofiles" ───────────


def _section(section_id: str) -> str:
    start = LANDING.index(f'<section class="block" id="{section_id}">')
    return LANDING[start : LANDING.index("</section>", start)]


@pytest.mark.parametrize("section_id", ["vision", "dashboard"])
def test_story_sections_are_bilingual(section_id):
    block = _section(section_id)
    parser = _TextWithoutI18n()
    parser.feed(block)
    loose = [t for t in parser.offenders if not _NEUTRAL.match(t)]
    assert not loose, loose
    for attr in re.findall(r'\sdata-en(?:-label)?="([^"]*)"', block):
        assert not _SK_IN_EN.search(attr), attr


def test_vision_sits_above_the_roadmap_and_labels_connections_as_roadmap():
    assert (
        LANDING.index('id="vision"') < LANDING.index('id="photos"') < LANDING.index('id="roadmap"')
    )
    block = _section("vision")
    for piece in ("pz-chat", "pz-docs", "pz-core", "pz-std", "pz-hosp"):
        assert f'class="pz {piece}"' in block, piece
    hosp = block[block.index('class="pz pz-hosp"') :]
    assert 'data-en="roadmap"' in hosp and "No hospital or app is connected yet." in hosp
    # devices are named only in the roadmap's planned column
    for name in _DEVICES:
        assert name not in block


async def test_dashboard_section_is_a_mock_with_the_invented_patient():
    from oncofiles.server import mcp

    registered = {
        k.partition(":")[2].partition("@")[0]
        for k in mcp._local_provider._components
        if k.startswith("tool:")
    }
    block = _section("dashboard")
    assert "dashboard_feed" in block and "dashboard_feed" in registered
    assert "It runs on Oncofiles." in block and "Beží na Oncofiles." in block
    shot = block[block.index('id="artifact-shot"') :]
    # public page: a mock with the invented demo patient, never a real screenshot
    assert "<img" not in shot
    assert "Example with an invented patient." in shot
    assert 'data-en="Tumour markers and blood count · Jane Doe"' in shot
    assert 'data-en="2.7 unverified"' in shot
    # the mock's CEA values are the demo payload's values
    data = json.loads((await api_demo_data(_req("/api/demo-data"))).body)
    cea = [p["value_text"] for s in data["series"] if s["code"] == "CEA" for p in s["points"]]
    for v in cea[-3:]:
        assert f'data-sk="{v}"' in shot, v
