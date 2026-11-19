"""/demo is indexable and the sitemap lists only content pages (2026-11-19).

Search Console reported /demo as "Excluded by 'noindex' tag" once the landing
nav linked to it. Maintainer decision: index it, and drop the technical
/health and the sign-in /dashboard from the sitemap.
"""

from __future__ import annotations

import json
import re

from starlette.requests import Request

from oncofiles.server import demo_dashboard, robots_txt, sitemap_xml


def _req(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


async def _demo_html() -> str:
    return (await demo_dashboard(_req("/demo"))).body.decode()


async def test_demo_has_no_noindex_meta_or_header():
    resp = await demo_dashboard(_req("/demo"))
    html = resp.body.decode()
    assert "noindex" not in html.lower()
    assert not re.search(r'<meta\s+name="robots"', html, re.I)
    assert "x-robots-tag" not in {k.lower() for k in resp.headers}


async def test_demo_served_through_the_app_has_no_x_robots_tag():
    """The security-header middleware must not add a noindex either."""
    from oncofiles.server import SecurityHeadersMiddleware

    async def app(scope, receive, send):
        resp = await demo_dashboard(_req("/demo"))
        await resp(scope, receive, send)

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/demo", "headers": []}
    await SecurityHeadersMiddleware(app)(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert "noindex" not in headers.get("x-robots-tag", "").lower()
    assert "content-security-policy" in headers


async def test_demo_has_canonical_description_and_social_tags():
    html = await _demo_html()
    assert '<link rel="canonical" href="https://oncofiles.com/demo">' in html
    assert '<link rel="alternate" hreflang="sk" href="https://oncofiles.com/demo">' in html
    assert '<link rel="alternate" hreflang="en" href="https://oncofiles.com/demo?lang=en">' in html
    desc = re.search(r'<meta name="description" content="([^"]+)">', html)
    assert desc and len(desc.group(1)) > 50
    for prop in ("og:title", "og:description", "og:url", "og:image", "og:type"):
        assert f'<meta property="{prop}"' in html, prop
    assert '<meta property="og:url" content="https://oncofiles.com/demo">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html


async def test_demo_json_ld_is_a_webpage_of_the_site():
    html = await _demo_html()
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert len(blocks) == 1
    page = json.loads(blocks[0])
    assert page["@type"] == "WebPage"
    assert page["url"] == "https://oncofiles.com/demo"
    assert page["isPartOf"]["url"] == "https://oncofiles.com/"


async def test_demo_keeps_the_invented_patient_disclaimer_visible():
    html = await _demo_html()
    assert 'class="demo-strip"' in html
    assert "vymyslená pacientka" in html
    assert "No real data." in html


async def test_sitemap_lists_demo_but_not_health_or_dashboard():
    xml = (await sitemap_xml(_req("/sitemap.xml"))).body.decode()
    locs = re.findall(r"<loc>([^<]+)</loc>", xml)
    assert "https://oncofiles.com/demo" in locs
    assert "https://oncofiles.com/health" not in locs
    assert "https://oncofiles.com/dashboard" not in locs
    assert "/health" not in xml
    assert "/dashboard" not in xml


async def test_robots_does_not_block_demo():
    text = (await robots_txt(_req("/robots.txt"))).body.decode()
    disallowed = [
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.lower().startswith("disallow:")
    ]
    for rule in disallowed:
        assert not "/demo".startswith(rule), rule
