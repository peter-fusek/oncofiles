"""Lock-in for #695: web fonts are self-hosted, never loaded from Google Fonts.

Loading fonts from fonts.googleapis.com / fonts.gstatic.com sends every
visitor's IP to Google on each page view (GDPR case law: LG München, 2022) and
contradicts the "self-hosted in the EU" message. The woff2 files, their
@font-face sheet and the OFL licences live in ``src/oncofiles/fonts/`` and are
served by the ``/fonts/{name}`` route.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.requests import Request

SRC = Path(__file__).parent.parent / "src" / "oncofiles"
FONTS_DIR = SRC / "fonts"
FONTS_CSS = FONTS_DIR / "fonts.css"

GOOGLE_FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")
LATIN_EXT_START = "U+0100-02BA"  # first range of Google's latin-ext subset


def _req(path: str, **path_params: str) -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": path, "headers": [], "path_params": path_params}
    )


# ── No Google Fonts anywhere in the served code ──────────────────────────


def test_no_source_file_references_google_fonts():
    """Every HTML template, CSS, JS and Python file in src/ (the topic pages
    and legal pages are f-strings in server.py) is font-CDN free."""
    offenders = []
    for path in SRC.rglob("*"):
        if path.suffix not in {".html", ".css", ".js", ".py", ".txt", ".json", ".svg"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for host in GOOGLE_FONT_HOSTS:
            if host in text:
                offenders.append(f"{path.relative_to(SRC)}: {host}")
    assert not offenders, f"Google Fonts reference(s) back in src/: {offenders}"


@pytest.mark.parametrize("path", ["/", "/dashboard", "/demo", "/onkologia", "/privacy"])
def test_csp_allows_no_google_font_origin(path):
    from oncofiles.server import _csp_for_path

    csp = _csp_for_path(path)
    for host in GOOGLE_FONT_HOSTS:
        assert host not in csp, f"{host} still allowed by the CSP of {path}"
    assert "font-src 'self' data:;" in csp


@pytest.mark.parametrize(
    "template",
    [SRC / "landing.html", SRC / "dashboard.html"],
    ids=lambda p: p.name,
)
def test_templates_link_the_self_hosted_sheet(template):
    assert '<link rel="stylesheet" href="/fonts/fonts.css">' in template.read_text()


async def test_topic_pages_link_the_self_hosted_sheet():
    from oncofiles.server import eu_page, oncology_page, onkologia_page, sk_page

    pages = (
        (onkologia_page, "/onkologia"),
        (oncology_page, "/oncology"),
        (eu_page, "/eu"),
        (sk_page, "/sk"),
    )
    for handler, path in pages:
        html = (await handler(_req(path))).body.decode()
        assert '<link rel="stylesheet" href="/fonts/fonts.css">' in html
        assert "@import" not in html


# ── The @font-face sheet matches the files on disk ───────────────────────


def _font_faces() -> list[str]:
    return re.findall(r"@font-face\s*\{(.*?)\}", FONTS_CSS.read_text(), re.S)


def test_every_font_face_is_swap_with_a_unicode_range():
    faces = _font_faces()
    assert len(faces) == 8  # 4 faces x (latin, latin-ext)
    for face in faces:
        assert "font-display: swap;" in face
        assert "unicode-range:" in face
        assert "format('woff2')" in face


def test_each_face_ships_latin_and_latin_ext():
    """Slovak letters (č, ľ, ť, ž …) live in latin-ext; without it the
    browser falls back to a system font mid-word."""
    by_face: dict[tuple[str, str], list[str]] = {}
    for face in _font_faces():
        family = re.search(r"font-family: '([^']+)'", face).group(1)
        style = re.search(r"font-style: (\w+)", face).group(1)
        ranges = re.search(r"unicode-range: ([^;]+);", face).group(1)
        by_face.setdefault((family, style), []).append(ranges)
    assert set(by_face) == {
        ("DM Sans", "normal"),
        ("DM Sans", "italic"),
        ("DM Serif Display", "normal"),
        ("IBM Plex Sans", "normal"),
    }
    for key, ranges in by_face.items():
        assert len(ranges) == 2, key
        assert sum(r.startswith(LATIN_EXT_START) for r in ranges) == 1, key
        assert sum(r.startswith("U+0000-00FF") for r in ranges) == 1, key


def test_every_referenced_font_file_exists_and_is_woff2():
    urls = re.findall(r"url\(([^)]+)\)", FONTS_CSS.read_text())
    assert urls
    for url in urls:
        assert url.startswith("/fonts/"), url
        path = FONTS_DIR / url.removeprefix("/fonts/")
        assert path.is_file(), url
        assert path.read_bytes()[:4] == b"wOF2", url
    on_disk = {p.name for p in FONTS_DIR.glob("*.woff2")}
    assert on_disk == {u.removeprefix("/fonts/") for u in urls}, "unreferenced woff2 file"


def test_ofl_licences_ship_with_the_fonts():
    for name in ("OFL-DMSans.txt", "OFL-DMSerifDisplay.txt", "OFL-IBMPlexSans.txt"):
        text = (FONTS_DIR / name).read_text()
        assert "SIL Open Font License, Version 1.1" in text


# ── The /fonts/{name} route ─────────────────────────────────────────────


def test_font_route_is_registered():
    from oncofiles.server import mcp

    paths = {getattr(r, "path", "") for r in mcp._get_additional_http_routes()}
    assert "/fonts/{name}" in paths


async def test_font_route_serves_woff2_immutable():
    from oncofiles.server import font_asset

    name = "dm-sans-v17-latin.woff2"
    resp = await font_asset(_req(f"/fonts/{name}", name=name))
    assert resp.status_code == 200
    assert resp.media_type == "font/woff2"
    assert resp.headers["content-type"] == "font/woff2"
    cache = resp.headers["cache-control"]
    assert "immutable" in cache
    assert "max-age=31536000" in cache
    assert resp.body == (FONTS_DIR / name).read_bytes()


async def test_font_route_serves_the_stylesheet():
    from oncofiles.server import font_asset

    resp = await font_asset(_req("/fonts/fonts.css", name="fonts.css"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert "immutable" not in resp.headers["cache-control"]
    assert b"@font-face" in resp.body


@pytest.mark.parametrize("name", ["../server.py", "..", "missing.woff2", "server.py", ""])
async def test_font_route_rejects_unknown_names(name):
    from oncofiles.server import font_asset

    resp = await font_asset(_req(f"/fonts/{name}", name=name))
    assert resp.status_code == 404
