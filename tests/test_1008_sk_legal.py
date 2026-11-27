"""#1008: /privacy and /terms exist in English and Slovak, with the same structure.

The claim tests (test_857, test_856, test_legal_pages) check what each page says;
this file checks that the two languages cannot drift apart structurally: the
same section ids, the same numbers, both linked to each other, both in the
sitemap, and Slovak UI text linking the Slovak page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from oncofiles import server
from oncofiles.server import (
    privacy_policy,
    privacy_policy_sk,
    sitemap_xml,
    terms_of_service,
    terms_of_service_sk,
)

SRC = Path(server.__file__).parent

PAIRS = [
    (privacy_policy, privacy_policy_sk, "/privacy", "/sk/privacy"),
    (terms_of_service, terms_of_service_sk, "/terms", "/sk/terms"),
]


@pytest.fixture(autouse=True)
def _fresh():
    server._PRIVACY_HTML = None
    server._TERMS_HTML = None
    yield
    server._PRIVACY_HTML = None
    server._TERMS_HTML = None


async def _html(route) -> str:
    return (await route(None)).body.decode()


def _ids(html: str) -> list[str]:
    return re.findall(r'<h[23] id="([a-z-]+)"', html)


def _numbers(html: str) -> list[str]:
    """Every number in the visible body (tags and URLs dropped, months as words)."""
    body = html.split("<body>", 1)[1]
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"https?://\S+", " ", body)
    return sorted(re.findall(r"\d+", body))


@pytest.mark.parametrize("en,sk,en_path,sk_path", PAIRS)
async def test_same_sections_in_both_languages(en, sk, en_path, sk_path):
    en_html, sk_html = await _html(en), await _html(sk)
    assert _ids(en_html) == _ids(sk_html) and _ids(en_html)
    assert len(re.findall(r"<h2 ", en_html)) == len(re.findall(r"<h2 ", sk_html))
    assert en_html.count("<li>") == sk_html.count("<li>")


@pytest.mark.parametrize("en,sk,en_path,sk_path", PAIRS)
async def test_same_numbers_in_both_languages(en, sk, en_path, sk_path):
    """Days, MB, counts, versions and dates: a number changed in one language
    only would make the two pages promise different things."""
    assert _numbers(await _html(en)) == _numbers(await _html(sk))


@pytest.mark.parametrize("en,sk,en_path,sk_path", PAIRS)
async def test_pages_link_each_other_and_declare_language(en, sk, en_path, sk_path):
    en_html, sk_html = await _html(en), await _html(sk)
    assert '<html lang="en">' in en_html and '<html lang="sk">' in sk_html
    for html in (en_html, sk_html):
        assert f'hreflang="en" href="https://oncofiles.com{en_path}"' in html
        assert f'hreflang="sk" href="https://oncofiles.com{sk_path}"' in html
    assert f'<a href="{sk_path}" hreflang="sk"' in en_html
    assert f'<a href="{en_path}" hreflang="en"' in sk_html
    # inside its text, the Slovak page links Slovak pages only (the switch is the exception)
    assert not re.search(r'href="/(terms|privacy)"(?! hreflang="en")', sk_html)
    assert "__CONTACT__" not in sk_html and "mailto:" in sk_html


async def test_english_pages_are_english_only():
    for route in (privacy_policy, terms_of_service):
        _head, _sep, body = (await _html(route)).partition("<h1>")
        assert 'lang="sk"' not in body


async def test_sitemap_lists_both_languages_with_alternates():
    xml = (await sitemap_xml(None)).body.decode()
    for _en, _sk, en_path, sk_path in PAIRS:
        for loc in (en_path, sk_path):
            assert f"<loc>https://oncofiles.com{loc}</loc>" in xml
        assert xml.count(f'hreflang="sk" href="https://oncofiles.com{sk_path}"') == 2


def test_slovak_ui_links_the_slovak_pages():
    for name in ("landing.html", "dashboard.html", "demo.html", "demo_cookie_banner.html"):
        html = (SRC / name).read_text()
        for m in re.finditer(r'<a href="(/sk)?/(privacy|terms)[^"]*"[^>]*>', html):
            tag = m.group(0)
            assert "data-sk-href=" in tag and "data-en-href=" in tag, (name, tag)
    gate = (SRC / "dashboard.html").read_text()
    sk_attr = re.search(r'class="gate-legal" data-html data-sk="([^"]*)"', gate)[1]
    assert "href='/sk/privacy'" in sk_attr and "href='/sk/terms'" in sk_attr
    js = (SRC / "dashboard.js").read_text()
    assert "[data-sk-href][data-en-href]" in js
    assert "[data-sk-href][data-en-href]" in (SRC / "demo.js").read_text()


def test_slovak_mail_lines_link_the_slovak_terms():
    from oncofiles import inbound_email

    assert inbound_email.TERMS_URL_SK.endswith("/sk/terms")
    assert inbound_email.TERMS_SENTENCE_SK.endswith(inbound_email.TERMS_URL_SK)
    assert inbound_email.TERMS_SENTENCE_EN.endswith(inbound_email.TERMS_URL)
    welcome = (SRC / "inbound_welcome.py").read_text()
    assert "inbound_email.TERMS_URL_SK" in welcome
