"""Smoke tests for the EU / SK positioning landing pages (#447 Phase A)."""

from __future__ import annotations

import json
import re

import pytest
from starlette.requests import Request

from oncofiles.server import eu_page, sitemap_xml, sk_page


def _fake_request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


async def _render(handler) -> str:
    response = await handler(_fake_request("/"))
    assert response.status_code == 200
    return response.body.decode()


@pytest.mark.parametrize(
    "handler,marker",
    [
        (eu_page, "European cancer patients"),
        (sk_page, "slovenských onkologických"),
    ],
)
async def test_page_renders(handler, marker):
    html = await _render(handler)
    assert marker in html
    assert "<!DOCTYPE html>" in html
    assert "Oncofiles" in html


async def test_eu_mentions_key_competitors_and_gap():
    html = await _render(eu_page)
    # The positioning hinges on the EEA/CH/UK gap — if any of these strings
    # disappear the page stops being useful for the intended narrative.
    assert "ChatGPT Health" in html
    assert "HealthEx" in html
    assert "EEA" in html


async def test_sk_mentions_sk_specific_context():
    html = await _render(sk_page)
    # SK press angles require these national touchpoints.
    assert "NOU" in html
    assert "OÚSA" in html
    assert "eZdravie" in html or "OnkoAsist" in html
    # Press block is the whole reason /sk exists separately from /onkologia.
    assert "Pre novinárov" in html


@pytest.mark.parametrize(
    "handler,canonical_path,alternate_path",
    [
        (eu_page, "/eu", "/sk"),
        (sk_page, "/sk", "/eu"),
    ],
)
async def test_canonical_and_hreflang_links(handler, canonical_path, alternate_path):
    html = await _render(handler)
    assert f'<link rel="canonical" href="https://oncofiles.com{canonical_path}">' in html
    # hreflang alternates must round-trip so Google shows the right language.
    assert f'href="https://oncofiles.com{alternate_path}"' in html
    # x-default always pins to the home page.
    assert 'hreflang="x-default"' in html


async def test_jsonld_is_valid_and_describes_software_application():
    html = await _render(eu_page)
    match = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, flags=re.DOTALL)
    assert match is not None
    payload = json.loads(match.group(1))
    graph = payload["@graph"]
    app = next(n for n in graph if n["@type"] == "SoftwareApplication")
    assert app["name"] == "Oncofiles"
    assert app["applicationCategory"] == "HealthApplication"
    # MedicalWebPage anchor survives so Google can surface the rich result.
    web = next(n for n in graph if n["@type"] == "WebPage")
    assert web["@id"] == "https://oncofiles.com/eu"
    assert web["inLanguage"] == "en"
    # areaServed names the EU scope — this is the whole positioning pitch.
    names = {a.get("name") for a in app.get("areaServed", [])}
    assert "European Union" in names
    assert "Slovakia" in names


async def test_sitemap_lists_new_pages_with_hreflang():
    response = await sitemap_xml(_fake_request("/sitemap.xml"))
    xml = response.body.decode()
    assert "https://oncofiles.com/eu" in xml
    assert "https://oncofiles.com/sk" in xml
    # Both new URLs carry hreflang alternates back to each other.
    eu_block = xml[xml.index("https://oncofiles.com/eu") : xml.index("</url>", xml.index("/eu"))]
    assert 'href="https://oncofiles.com/eu"' in eu_block
    assert 'href="https://oncofiles.com/sk"' in eu_block


async def test_pages_cross_link_to_each_other():
    eu_html = await _render(eu_page)
    sk_html = await _render(sk_page)
    assert 'href="/sk"' in eu_html
    assert 'href="/eu"' in sk_html
