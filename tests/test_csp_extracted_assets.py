"""Lock-in for #501 (partial): inline <script>/<style> blocks have moved to
external files and CSP3 ``*-elem`` directives now block ALL inline blocks.

Scope of this fix is the *block* half of #501. Inline event handlers
(``onclick="…"``) and inline ``style="…"`` attributes still pass via
``script-src-attr`` / ``style-src-attr`` ``'unsafe-inline'`` and are
tracked separately as the second half of the CSP refactor (58 onclick
handlers + 230 inline style attributes — needs its own session).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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


def test_dashboard_html_has_no_inline_event_handlers():
    """#525: every interactive element now uses ``data-action`` /
    ``data-change`` / ``data-input`` instead of ``onclick=`` /
    ``onchange=`` / ``oninput=`` / ``onmouseover=`` / ``onmouseout=``.
    The single global dispatcher in dashboard.js routes events.
    """
    html = DASHBOARD_HTML.read_text()
    # Every standard JS event-handler attribute is forbidden.
    handler_attrs = re.findall(
        r"\bon(click|change|input|submit|load|error|mouseover|mouseout|"
        r'mousedown|mouseup|keydown|keyup|keypress|focus|blur|drop|dragover)="',
        html,
    )
    assert handler_attrs == [], (
        f"inline event handlers found in dashboard.html: {handler_attrs!r} (#525)"
    )


def test_dashboard_js_includes_action_dispatcher():
    """The dispatcher must register click/change/input listeners and parse
    the ``fnName|arg1|arg2|@this|@value`` spec language."""
    js = DASHBOARD_JS.read_text()
    assert "window._dispatch" in js
    assert "data-action" in js
    assert "data-change" in js
    assert "data-input" in js
    # The @this / @value sentinels MUST be supported so handlers that need
    # the event target keep working.
    assert "@this" in js
    assert "@value" in js


# ── CSP header: inline blocks BLOCKED via *-elem; *-attr still permits ─


def _csp_directives(path: str = "/dashboard") -> dict[str, str]:
    """Resolve the CSP that the SecurityHeadersMiddleware would emit for
    ``path`` and split it by directive.

    Per-route CSP (#534): ``/dashboard*`` gets the strict #501/#525 policy,
    every other HTML route gets the pre-#501 permissive policy. Default is
    /dashboard so the existing #501 lock-in tests keep asserting the strict
    shape without needing parameter updates.
    """
    from oncofiles.server import _csp_for_path

    csp = _csp_for_path(path)
    directives: dict[str, str] = {}
    for d in csp.split(";"):
        d = d.strip()
        if not d:
            continue
        name, _, rest = d.partition(" ")
        directives[name] = rest.strip()
    return directives


# Backward-compat alias for any external callers that imported the old name.
_live_csp_directives = _csp_directives


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


def test_csp_blocks_inline_event_handlers():
    """``script-src-attr 'none'`` (#525, 2026-04-29) — the 50 inline event
    handlers (onclick, onchange, oninput, onmouseover/out) were converted
    to data-action / data-change / data-input + a delegated dispatcher in
    dashboard.js. Inline event handlers must now be REJECTED by the
    browser, not just permitted as a back-compat fallback."""
    directives = _live_csp_directives()
    assert "script-src-attr" in directives
    assert directives["script-src-attr"] == "'none'", (
        f"script-src-attr must be 'none' (#525). Got: {directives['script-src-attr']!r}"
    )


def test_csp_style_attr_still_permits_inline():
    """``style-src-attr`` still permits 'unsafe-inline' — the ~240 inline
    ``style="…"`` attributes haven't been migrated yet. Inline styles are
    inert data (no JS execution), so the security cost is low; full
    migration to CSS classes is a follow-up."""
    directives = _live_csp_directives()
    assert "style-src-attr" in directives
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


# ── Per-route CSP regression lock (#534) ──────────────────────────────
#
# `ea2496d` (#501) globalised the strict CSP3 directives to every text/html
# response even though only `dashboard.html` had its inline blocks
# extracted. The marketing landing page, gloww, /onkologia, /oncology, /eu,
# /sk, /privacy, /terms and /oauth/callback all silently broke for ~7 days
# until a human noticed the rendering. These tests lock the per-route split:
# /dashboard* keeps the strict policy; every other HTML route falls back to
# the pre-#501 permissive policy that allows their inline <style>, inline
# <script> (gtag bootstrap, lang switch, JSON-LD), and inline event-handler
# attributes (CTA tab buttons) to keep working.


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/onkologia",
        "/oncology",
        "/eu",
        "/sk",
        "/privacy",
        "/terms",
        "/gloww",
        "/oauth/callback",
    ],
)
def test_public_html_csp_permits_inline_blocks(path: str) -> None:
    """Public HTML routes must NOT carry the strict #501 *-elem directives —
    those would block the inline <script>/<style>/onclick that the public
    pages still depend on. Falling back to the legacy `script-src` /
    `style-src` 'unsafe-inline' shape is the documented hotfix shape (#534)."""
    directives = _csp_directives(path)
    assert "script-src-elem" not in directives, (
        f"{path}: script-src-elem must NOT appear on public routes — its "
        f"presence would override the legacy script-src and block inline "
        f"<script> blocks (gtag bootstrap, JSON-LD, lang switch). #534."
    )
    assert "style-src-elem" not in directives, (
        f"{path}: style-src-elem must NOT appear on public routes — its "
        f"presence would override the legacy style-src and block the "
        f"inline <style> block that owns the entire page layout. #534."
    )
    assert "script-src-attr" not in directives, (
        f"{path}: script-src-attr 'none' must NOT appear on public routes — "
        f"it would block the inline onclick CTAs on the landing/gloww pages. #534."
    )
    # Legacy directives must still be present and permit inline.
    assert "'unsafe-inline'" in directives.get("script-src", ""), (
        f"{path}: script-src must include 'unsafe-inline' for inline <script> blocks."
    )
    assert "'unsafe-inline'" in directives.get("style-src", ""), (
        f"{path}: style-src must include 'unsafe-inline' for the inline <style> block."
    )


def test_dashboard_csp_stays_strict_after_split() -> None:
    """The /dashboard* routes MUST keep the #501/#525 strict shape. This is
    the regression-other-direction test — if someone widens the public CSP
    branch to also cover /dashboard, the dashboard XSS hardening that #501
    + #525 closed would silently regress."""
    directives = _csp_directives("/dashboard")
    assert directives.get("script-src-attr") == "'none'", (
        f"/dashboard: script-src-attr must remain 'none' (#525). "
        f"Got: {directives.get('script-src-attr')!r}"
    )
    assert "'unsafe-inline'" not in directives.get("script-src-elem", ""), (
        "/dashboard: script-src-elem must NOT permit 'unsafe-inline' (#501)."
    )
    assert "'unsafe-inline'" not in directives.get("style-src-elem", ""), (
        "/dashboard: style-src-elem must NOT permit 'unsafe-inline' (#501)."
    )
