"""Lock-in for #501 (partial): inline <script>/<style> blocks have moved to
external files and CSP3 ``*-elem`` directives now block ALL inline blocks.

Scope of this fix is the *block* half of #501. Inline event handlers
(``onclick="…"``) and inline ``style="…"`` attributes still pass via
``script-src-attr`` / ``style-src-attr`` ``'unsafe-inline'`` and are
tracked separately as the second half of the CSP refactor (58 onclick
handlers + 230 inline style attributes — needs its own session).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

DASHBOARD_HTML = Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html"
DASHBOARD_CSS = Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.css"
DASHBOARD_JS = Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.js"
DASHBOARD_GTAG_JS = Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard-gtag.js"


# ── External assets exist and are non-trivial ──────────────────────────


def test_dashboard_css_exists_and_non_empty():
    """The 932-line inline <style> block is now an external file."""
    assert DASHBOARD_CSS.exists(), "dashboard.css missing — extraction step regressed"
    body = DASHBOARD_CSS.read_text()
    # Sanity: must contain CSS that we know was inline before extraction.
    assert ":root" in body
    assert "--bg:" in body
    assert "#next-sync" in body  # last selector in the original block
    assert len(body) > 20_000, f"dashboard.css unexpectedly small: {len(body)} bytes"


def test_dashboard_js_exists_and_non_empty():
    """The 3017-line inline <script> block is now an external file."""
    assert DASHBOARD_JS.exists(), "dashboard.js missing — extraction step regressed"
    body = DASHBOARD_JS.read_text()
    # Sanity: must contain functions we know were inline before extraction.
    assert "function toggleSection" in body
    assert "function logout" in body
    assert "fetch('/api/logout'" in body  # #510 server-side revocation path
    assert len(body) > 100_000, f"dashboard.js unexpectedly small: {len(body)} bytes"


def test_dashboard_gtag_js_exists():
    """The gtag bootstrap is no longer inline either."""
    assert DASHBOARD_GTAG_JS.exists()
    body = DASHBOARD_GTAG_JS.read_text()
    assert "window.dataLayer" in body
    assert "gtag(" in body


# ── dashboard.html: no inline <script> or <style> blocks ──────────────


def test_dashboard_html_has_no_inline_script_blocks():
    """Every <script> tag in dashboard.html now has a `src` attribute —
    no more inline JS bodies that CSP would have to allow."""
    html = DASHBOARD_HTML.read_text()
    # Find every <script ...> tag (non-self-closing).
    for match in re.finditer(r"<script\b([^>]*)>", html, re.IGNORECASE):
        attrs = match.group(1)
        assert "src=" in attrs, (
            f"inline <script> block found in dashboard.html — extract it to an "
            f"external .js file so script-src-elem can drop 'unsafe-inline' "
            f"(#501). Tag attrs: {attrs!r}"
        )


def test_dashboard_html_has_no_inline_style_blocks():
    """No more inline <style>...</style> blocks. (Inline `style=""`
    attributes are intentionally still permitted — see module docstring.)"""
    html = DASHBOARD_HTML.read_text()
    assert "<style>" not in html, (
        "inline <style> block found in dashboard.html — extract to dashboard.css (#501)"
    )
    assert "<style " not in html, (
        "inline <style ...> block found in dashboard.html — extract to dashboard.css"
    )


def test_dashboard_html_links_external_css_and_js():
    html = DASHBOARD_HTML.read_text()
    assert '<link rel="stylesheet" href="/dashboard.css">' in html
    assert '<script src="/dashboard.js" defer></script>' in html
    assert '<script src="/dashboard-gtag.js"></script>' in html


# ── CSP header: inline blocks BLOCKED via *-elem; *-attr still permits ─


def _live_csp_directives() -> dict[str, str]:
    """Pull the actual CSP literal out of the SecurityHeadersMiddleware
    source and split it by directive.

    Reading the source (vs running the middleware against a fake ASGI
    request) keeps the test self-contained — but we extract ONLY the
    literal Python string passed to ``csp = (...)`` so comments mentioning
    directive names (e.g. "script-src-elem") don't pollute the regex.
    """
    from oncofiles.server import SecurityHeadersMiddleware

    src = inspect.getsource(SecurityHeadersMiddleware)
    # Grab everything from `csp = (` to the closing `)`.
    body_match = re.search(r"csp\s*=\s*\(\s*(.*?)\s*\)", src, re.DOTALL)
    assert body_match is not None, "could not locate `csp = (...)` literal"
    body = body_match.group(1)
    # Concatenate all the quoted parts, stripping the quotes themselves.
    pieces = re.findall(r'"([^"]*)"', body)
    csp = "".join(pieces)
    # Split by `;` and drop empties.
    directives: dict[str, str] = {}
    for d in csp.split(";"):
        d = d.strip()
        if not d:
            continue
        name, _, rest = d.partition(" ")
        directives[name] = rest.strip()
    return directives


def test_csp_blocks_inline_script_elem():
    """``script-src-elem`` excludes 'unsafe-inline' — the CSP3 directive
    that controls <script> blocks specifically. Browsers that support it
    now refuse to execute any inline <script> in dashboard responses."""
    directives = _live_csp_directives()
    assert "script-src-elem" in directives, "script-src-elem directive missing"
    assert "'unsafe-inline'" not in directives["script-src-elem"], (
        f"script-src-elem must NOT contain 'unsafe-inline' (#501 partial). "
        f"Got: {directives['script-src-elem']!r}"
    )
    # The directive must still allow 'self' + Google's analytics origins.
    assert "'self'" in directives["script-src-elem"]


def test_csp_blocks_inline_style_elem():
    """``style-src-elem`` excludes 'unsafe-inline' — blocks all <style> blocks."""
    directives = _live_csp_directives()
    assert "style-src-elem" in directives, "style-src-elem directive missing"
    assert "'unsafe-inline'" not in directives["style-src-elem"], (
        f"style-src-elem must NOT contain 'unsafe-inline' (#501 partial). "
        f"Got: {directives['style-src-elem']!r}"
    )
    assert "'self'" in directives["style-src-elem"]


def test_csp_attr_directives_still_permit_inline():
    """``script-src-attr`` and ``style-src-attr`` keep 'unsafe-inline' so
    the dashboard's 58 onclick handlers and 230 inline ``style="…"``
    attributes keep working. Removing this is the second half of #501
    (full attribute-level rewrite, tracked separately)."""
    directives = _live_csp_directives()
    assert "script-src-attr" in directives
    assert "style-src-attr" in directives
    assert "'unsafe-inline'" in directives["script-src-attr"]
    assert "'unsafe-inline'" in directives["style-src-attr"]


# ── Static-route handlers exist and load cached file content ──────────


async def test_dashboard_css_route_serves_extracted_file():
    from unittest.mock import MagicMock

    from oncofiles.server import dashboard_css

    request = MagicMock()
    response = await dashboard_css(request)
    assert response.media_type.startswith("text/css")
    assert response.status_code == 200
    body = response.body.decode()
    assert ":root" in body


async def test_dashboard_js_route_serves_extracted_file():
    from unittest.mock import MagicMock

    from oncofiles.server import dashboard_js

    request = MagicMock()
    response = await dashboard_js(request)
    assert response.media_type.startswith("application/javascript")
    assert response.status_code == 200
    body = response.body.decode()
    assert "function toggleSection" in body


async def test_dashboard_gtag_js_route_serves_extracted_file():
    from unittest.mock import MagicMock

    from oncofiles.server import dashboard_gtag_js

    request = MagicMock()
    response = await dashboard_gtag_js(request)
    assert response.media_type.startswith("application/javascript")
    body = response.body.decode()
    assert "window.dataLayer" in body
