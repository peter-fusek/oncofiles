"""Movement-atlas showcase (#830): routes, media, CSP, copy and privacy locks.

The showcase is the Oncofiles author's own health data, published by his
decision. These tests pin what may be served (a fixed file allowlist, Range
for the video, a strict CSP for the three.js page) and what may never appear
in the published files (identifiers, people, institutions, document links,
claims the project cannot back).

Nothing here is a fixture with patient values: the tests read the shipped
files and assert on their shape.
"""

from __future__ import annotations

import json
import re
import struct
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest
from starlette.requests import Request

from oncofiles import atlas_showcase
from oncofiles.server import (
    _csp_for_path,
    _load_landing_html,
    llms_sk_txt,
    llms_txt,
    movement_atlas_asset,
    movement_atlas_page,
    movement_atlas_use_case_en,
    movement_atlas_use_case_sk,
    sitemap_xml,
)

SRC = Path(__file__).parent.parent / "src" / "oncofiles"
ATLAS_DIR = SRC / "atlas"
LANDING = (SRC / "landing.html").read_text(encoding="utf-8")
SK_PATH = "/pouzitie/pohybovy-atlas"
EN_PATH = "/use-cases/movement-atlas"


def _req(path: str, **path_params: str) -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": path, "headers": [], "path_params": path_params}
    )


async def _send(response, headers: list[tuple[bytes, bytes]] | None = None):
    """Run a response through ASGI and return (status, headers, body)."""
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.4"},  # FileResponse then streams without polling receive()
        "method": "GET",
        "path": "/",
        "headers": headers or [],
    }
    messages: list[dict] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await response(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    got = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], got, body


def _section(section_id: str) -> str:
    start = LANDING.index(f'<section class="block" id="{section_id}">')
    return LANDING[start : LANDING.index("</section>", start)]


class _LooseText(HTMLParser):
    """Visible text that no element with data-sk covers."""

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.loose: list[str] = []

    def handle_starttag(self, tag, attrs):
        covered = "data-sk" in dict(attrs) or (self.stack and self.stack[-1][1])
        self.stack.append((tag, bool(covered)))

    def handle_endtag(self, tag):
        while self.stack and self.stack.pop()[0] != tag:
            pass

    def handle_data(self, data):
        if data.strip() and not (self.stack and self.stack[-1][1]):
            self.loose.append(data.strip()[:60])


# ── Static assets ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,media_type",
    [
        ("atlas.css", "text/css; charset=utf-8"),
        ("app.js", "application/javascript; charset=utf-8"),
        ("three.module.js", "application/javascript; charset=utf-8"),
        ("GLTFLoader.js", "application/javascript; charset=utf-8"),
        ("BufferGeometryUtils.js", "application/javascript; charset=utf-8"),
        ("mesh-figure.glb", "model/gltf-binary"),
        ("mesh-spine.glb", "model/gltf-binary"),
        ("mesh-knee.glb", "model/gltf-binary"),
        ("mesh-shoulder.glb", "model/gltf-binary"),
        ("ATTRIBUTION.md", "text/markdown; charset=utf-8"),
        ("poster-sk.jpg", "image/jpeg"),
        ("poster-en.jpg", "image/jpeg"),
        ("poster-sk-start.jpg", "image/jpeg"),
        ("poster-en-start.jpg", "image/jpeg"),
        ("video-sk.mp4", "video/mp4"),
        ("video-en.mp4", "video/mp4"),
    ],
)
async def test_assets_are_served_with_type_and_cache(name, media_type):
    response = await movement_atlas_asset(_req(f"/movement-atlas/{name}", name=name))
    status, headers, body = await _send(response)
    assert status == 200
    assert headers["content-type"] == media_type
    assert headers["cache-control"].startswith("public, max-age=")
    assert int(headers["cache-control"].split("max-age=")[1].split(",")[0]) >= 86400
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["accept-ranges"] == "bytes"
    assert len(body) == (ATLAS_DIR / name).stat().st_size


@pytest.mark.parametrize("lang", ["sk", "en"])
async def test_video_answers_a_range_request_with_206(lang):
    """Safari only plays a <video> from a server that honours Range."""
    name = f"video-{lang}.mp4"
    size = (ATLAS_DIR / name).stat().st_size
    response = await movement_atlas_asset(_req(f"/movement-atlas/{name}", name=name))
    status, headers, body = await _send(response, [(b"range", b"bytes=0-1")])
    assert status == 206
    assert headers["content-range"] == f"bytes 0-1/{size}"
    assert len(body) == 2
    # an open-ended range from the middle, as a seek sends it
    response = await movement_atlas_asset(_req(f"/movement-atlas/{name}", name=name))
    status, headers, body = await _send(response, [(b"range", f"bytes={size - 10}-".encode())])
    assert status == 206 and len(body) == 10
    assert headers["content-type"] == "video/mp4"


@pytest.mark.parametrize(
    "name",
    ["index.html", "../server.py", "video.mp4", "", "three.js", "three.min.js", "mesh-hip.glb"],
)
async def test_only_allowlisted_files_are_served(name):
    response = await movement_atlas_asset(_req(f"/movement-atlas/{name}", name=name))
    assert response.status_code == 404


def test_allowlist_matches_the_shipped_folder():
    shipped = {p.name for p in ATLAS_DIR.iterdir() if p.is_file()}
    assert shipped == set(atlas_showcase.ASSETS) | {"index.html"}


def test_only_the_small_videos_are_shipped():
    """The ~11.5 MB originals stay out of the repo (media decision in #830)."""
    for lang in ("sk", "en"):
        video = (ATLAS_DIR / f"video-{lang}.mp4").read_bytes()
        assert len(video) < 4_000_000
        # moov before mdat, so playback starts without downloading the file
        assert 0 < video.find(b"moov") < video.find(b"mdat")
    # videos ~7.1 MB + meshes ~1.9 MB + three.js modules ~1.3 MB (#841)
    assert sum(p.stat().st_size for p in ATLAS_DIR.iterdir()) < 11_500_000


# ── The hosted atlas page and its CSP ────────────────────────────────────


async def test_atlas_page_is_served_without_inline_code():
    response = await movement_atlas_page(_req("/movement-atlas"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.body.decode()
    assert not re.search(r"\son[a-z]+\s*=", html), "no inline event handlers"
    assert "<style" not in html and "style=" not in html
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S)
    # one ES module; it imports three.js and the loader from this origin (no import map)
    assert [attrs.strip() for attrs, _ in scripts] == ['type="module" src="/movement-atlas/app.js"']
    assert all(not body.strip() for _, body in scripts)
    # nothing is loaded from another host (no CDN, no font host) ...
    assert not re.search(r"""src=["'](?:https?:)?//(?!oncofiles\.com/)""", html)
    assert not re.search(r"""<link[^>]+href=["'](?:https?:)?//(?!oncofiles\.com/)""", html)
    # ... and the only links that leave the site are the mesh credit (#841)
    external = re.findall(r"""href=["']((?:https?:)?//(?!oncofiles\.com/)[^"']+)""", html)
    assert sorted(external) == sorted(MESH_CREDIT_LINKS)
    assert '<link rel="stylesheet" href="/fonts/fonts.css">' in html
    # indexable, with its own Slovak title, description and canonical
    assert "noindex" not in html
    assert "<title>Petrov pohybový atlas: chrbát, koleno a rameno v 3D | Oncofiles</title>" in html
    assert '<link rel="canonical" href="https://oncofiles.com/movement-atlas">' in html
    description = re.search(r'<meta name="description" content="([^"]+)">', html).group(1)
    assert "schematický" in description and "Nie je to lekárska rada." in description
    # real data, said on the page, with the way back to the explanation
    assert "Skutočný nález autora Oncofiles" in html
    assert f'href="{SK_PATH}"' in html and f'href="{EN_PATH}"' in html


def test_atlas_keeps_its_disclaimer_and_knee_warning():
    app_js = (ATLAS_DIR / "app.js").read_text(encoding="utf-8")
    assert "Nezobrazuje skutočné snímky." in app_js
    assert "Cviky sú bežné príklady, nie predpis." in app_js
    assert "Model vychádza z popisu rádiológa." in app_js
    assert "Pred záťažou to treba overiť u ortopéda." in app_js
    html = (ATLAS_DIR / "index.html").read_text(encoding="utf-8")
    assert "Schematický 3D model, nie snímky." in html


@pytest.mark.parametrize("path", ["/movement-atlas", "/movement-atlas/app.js"])
def test_atlas_route_has_its_own_strict_csp(path):
    csp = _csp_for_path(path)
    directives = dict(d.strip().partition(" ")[::2] for d in csp.split(";") if d.strip())
    assert directives["default-src"] == "'self'"
    assert directives["script-src"] == "'self'"
    assert directives["script-src-attr"] == "'none'"
    assert directives["style-src"] == "'self'"
    assert directives["object-src"] == "'none'"
    assert directives["frame-ancestors"] == "'self'"
    for directive in ("default-src", "script-src", "style-src", "connect-src", "font-src"):
        assert "unsafe" not in directives[directive], directive
        assert "http" not in directives[directive], directive
    assert "unsafe-eval" not in csp


@pytest.mark.parametrize(
    "path", ["/", SK_PATH, EN_PATH, "/demo", "/movement-atlas-x", "/dashboard"]
)
def test_atlas_csp_does_not_leak_to_other_routes(path):
    """Per-route CSP (#534): the atlas policy would break every page with an
    inline <style> or <script>."""
    assert _csp_for_path(path) != atlas_showcase.ATLAS_CSP


def test_public_pages_can_play_the_video_under_their_csp():
    """No media-src directive: <video> falls back to default-src 'self'."""
    for path in ("/", SK_PATH, EN_PATH):
        csp = _csp_for_path(path)
        assert "default-src 'self'" in csp and "media-src" not in csp


def test_atlas_scripts_need_nothing_the_csp_forbids():
    app_js = (ATLAS_DIR / "app.js").read_text(encoding="utf-8")
    assert not re.search(
        r"\beval\s*\(|new Function\s*\(|\bfetch\s*\(|XMLHttpRequest|import\s*\(", app_js
    )
    assert not re.search(r"https?://(?!www\.w3\.org/)", app_js)
    assert "WebAssembly" not in app_js and ".wasm" not in app_js
    # three.js r149 as same-origin ES modules: relative imports only, licence header kept
    assert re.findall(r"^import .*? from '([^']+)';$", app_js, re.M) == [
        "./three.module.js",
        "./GLTFLoader.js",
    ]
    three = (ATLAS_DIR / "three.module.js").read_text(encoding="utf-8")
    assert "@license" in three[:200] and "const REVISION = '149';" in three[:400]
    for name in ("GLTFLoader.js", "BufferGeometryUtils.js"):
        text = (ATLAS_DIR / name).read_text(encoding="utf-8")
        assert "three.js r149 (MIT" in text[:200]
        imports = re.findall(r"from '([^']+)';", text)
        assert imports and all(i.startswith("./") and "/" not in i[2:] for i in imports), imports
        assert not re.search(r"\beval\s*\(|new Function\s*\(", text)
    # every file the page asks for is on the allowlist
    for name in ["three.module.js", "GLTFLoader.js", *MESHES]:
        assert name in atlas_showcase.ASSETS
    assert "'/movement-atlas/mesh-' + k + '.glb'" in app_js


def test_atlas_csp_is_unchanged_by_the_meshes():
    """#841 added ES modules and GLB files; both fit the #830 policy as it was.
    No inline import map, no WASM decoder, no other host."""
    assert atlas_showcase.ATLAS_CSP == (
        "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self'; "
        "style-src-attr 'unsafe-inline'; font-src 'self'; img-src 'self' data: blob:; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'self'; base-uri 'self'; "
        "form-action 'none'; upgrade-insecure-requests"
    )
    assert "wasm" not in atlas_showcase.ATLAS_CSP
    assert not [p.name for p in ATLAS_DIR.iterdir() if p.suffix in (".wasm", ".bin")]
    assert "importmap" not in (ATLAS_DIR / "index.html").read_text(encoding="utf-8")


# ── Meshes (#841): budget, file metadata, attribution ────────────────────

MESHES = ("mesh-figure.glb", "mesh-spine.glb", "mesh-knee.glb", "mesh-shoulder.glb")
MESH_CREDIT_LINKS = (
    "https://dbarchive.biosciencedbc.jp/en/bodyparts3d/download.html",
    "https://creativecommons.org/licenses/by/4.0/",
    "https://creativecommons.org/licenses/by-sa/4.0/",
)
_MESH_PARTS = {
    "mesh-figure.glb": {"body"},
    "mesh-knee.glb": {"femur", "tibia", "fibula", "patella", "rectus_femoris", "popliteus"},
    "mesh-spine.glb": {"sacrum", "L5", "iliocostalis_r", "longissimus_l", "psoas_l"},
    "mesh-shoulder.glb": {"scapula", "clavicle", "humerus", "humerus_head", "supraspinatus"},
}


def _glb(name: str) -> tuple[dict, bytes]:
    data = (ATLAS_DIR / name).read_bytes()
    magic, version, total = struct.unpack("<III", data[:12])
    assert (magic, version, total) == (0x46546C67, 2, len(data))
    length, kind = struct.unpack("<II", data[12:20])
    assert kind and data[16:20] == b"JSON"
    return json.loads(data[20 : 20 + length]), data[20 : 20 + length]


@pytest.mark.parametrize("name", MESHES)
def test_mesh_files_are_plain_gltf_with_credit_and_clean_metadata(name):
    doc, raw = _glb(name)
    assert doc["asset"]["version"] == "2.0"
    assert "BodyParts3D" in doc["asset"]["copyright"] and "modified" in doc["asset"]["copyright"]
    # uncompressed and self-contained: no decoder, no external buffer or image
    assert "extensionsRequired" not in doc and "extensionsUsed" not in doc
    assert all("uri" not in b for b in doc["buffers"]) and "images" not in doc
    assert _MESH_PARTS[name] <= {n["name"] for n in doc["nodes"]}
    assert doc["scenes"][0]["extras"], "landmarks the page reads"
    # file metadata: no path, user, e-mail or tool banner beyond the fixed generator string
    assert doc["asset"]["generator"] == "oncofiles atlas_meshes"
    assert not re.search(rb"[\w.+-]+@[\w-]+\.[a-z]+", raw)
    for marker in (b"/Users/", b"/home/", b"/tmp/", b"\\Users"):
        assert marker not in raw, marker


def test_mesh_budget():
    """Issue #841: at most ~3 MB per area and ~8 MB in total, loaded per area."""
    sizes = {name: (ATLAS_DIR / name).stat().st_size for name in MESHES}
    assert all(size < 3_000_000 for size in sizes.values()), sizes
    assert sum(sizes.values()) < 8_000_000, sizes


def test_mesh_credit_and_limits_are_on_the_page():
    html = (ATLAS_DIR / "index.html").read_text(encoding="utf-8")
    credit = html[html.index('<footer class="credit">') : html.index("</footer>")]
    for text in (
        "3D anatómia:",
        "BodyParts3D",
        "© The Database Center for Life Science",
        "CC BY 4.0",
        "upravené",
        "CC BY-SA 4.0",
        "Ilustračná všeobecná anatómia, nie Petrove snímky.",
        'href="/movement-atlas/ATTRIBUTION.md"',
    ):
        assert text in credit, text
    for link in MESH_CREDIT_LINKS:
        assert f'href="{link}"' in credit
    assert "Anatómia je zo všeobecného modelu tela" in html
    app_js = (ATLAS_DIR / "app.js").read_text(encoding="utf-8")
    assert "ilustračná všeobecná anatómia z verejného modelu ľudského tela" in app_js
    assert "nie Petrove vlastné snímky" in app_js
    assert "Načítavam 3D model…" in app_js and 'id="loading" role="status"' in html
    # the use-case pages carry the credit where the atlas is described
    for lang, text in (
        ("sk", "licencia CC BY 4.0, upravené"),
        ("en", "CC BY 4.0 licence, modified"),
    ):
        page = atlas_showcase.use_case_page_html(lang)
        assert "BodyParts3D" in page and "© The Database Center for Life Science" in page
        assert text in page
        assert 'href="/movement-atlas/ATTRIBUTION.md"' in page


def test_attribution_file_names_source_licences_and_every_part():
    text = (ATLAS_DIR / "ATTRIBUTION.md").read_text(encoding="utf-8")
    for needle in (
        "BodyParts3D, © The Database Center for Life Science",
        "isa_BP3D_4.0_obj_99.zip",
        "Creative Commons Attribution\n4.0 International",
        "CC Attribution-Share Alike 2.1 Japan",
        "Creative Commons Attribution-ShareAlike 4.0 International",
        "https://creativecommons.org/licenses/by-sa/4.0/legalcode",
        "It is not the\nanatomy of the person whose findings the atlas shows",
    ):
        assert needle in text, needle
    # every source file the build script reads is listed
    script = Path(__file__).parent.parent / "scripts" / "atlas_meshes" / "build_meshes.py"
    sources = re.search(r"SOURCES = \{(.*?)\n\}", script.read_text(encoding="utf-8"), re.S).group(1)
    ids = re.findall(r'"(FJ\d+M?)":', sources)
    assert len(ids) > 30
    assert not [fid for fid in ids if not re.search(rf"\b{fid}\b", text)]


# ── Use-case pages ───────────────────────────────────────────────────────


async def _page(lang: str) -> str:
    handler = movement_atlas_use_case_sk if lang == "sk" else movement_atlas_use_case_en
    response = await handler(_req(SK_PATH if lang == "sk" else EN_PATH))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return response.body.decode()


@pytest.mark.parametrize("lang", ["sk", "en"])
async def test_use_case_page_head_and_video(lang):
    html = await _page(lang)
    own, other = (SK_PATH, EN_PATH) if lang == "sk" else (EN_PATH, SK_PATH)
    assert f'<html lang="{lang}">' in html
    assert f'<link rel="canonical" href="https://oncofiles.com{own}">' in html
    assert f'hreflang="sk" href="https://oncofiles.com{SK_PATH}"' in html
    assert f'hreflang="en" href="https://oncofiles.com{EN_PATH}"' in html
    assert f'<a class="lang" href="{other}"' in html
    assert len(re.findall(r"<h1[\s>]", html)) == 1
    assert not re.search(r"\son[a-z]+\s*=", html), "no inline event handlers"
    assert '<link rel="stylesheet" href="/fonts/fonts.css">' in html

    video_url = f"https://oncofiles.com/movement-atlas/video-{lang}.mp4"
    assert f'<meta property="og:video" content="{video_url}">' in html
    assert '<meta property="og:video:type" content="video/mp4">' in html

    video = re.search(r"<video\b[^>]*>", html, re.S).group(0)
    for attr in ("controls", "muted", "playsinline", 'preload="metadata"'):
        assert attr in video, attr
    # people come here to read: nothing starts by itself, in markup or script
    assert "autoplay" not in html and "loop" not in video
    assert ".play(" not in html and "IntersectionObserver" not in html
    assert 'width="1080"' in video and 'height="1080"' in video
    # the poster is the frame the first play starts at; the intro card is og:image
    assert 'data-start="12"' in video
    assert f'poster="/movement-atlas/poster-{lang}-start.jpg"' in video
    og_image = f"https://oncofiles.com/movement-atlas/poster-{lang}.jpg"
    assert f'<meta property="og:image" content="{og_image}">' in html
    assert f'src="/movement-atlas/video-{lang}.mp4"' in video
    assert 'aria-describedby="video-alt"' in video and 'id="video-alt"' in html
    start_script = html[html.rindex("<script>") : html.rindex("</script>")]
    assert "video[data-start]" in start_script and "v.currentTime = start" in start_script
    assert "addEventListener('play', applyStart)" in start_script

    block = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1)
    data = json.loads(block)
    assert data["@type"] == "VideoObject"
    assert data["contentUrl"] == video_url
    assert data["thumbnailUrl"] == f"https://oncofiles.com/movement-atlas/poster-{lang}.jpg"
    assert data["duration"] == "PT82S" and data["inLanguage"] == lang
    assert date.fromisoformat(data["uploadDate"]) <= date.today()
    for key in ("name", "description"):
        assert data[key].strip()


@pytest.mark.parametrize(
    "lang,real,limits",
    [
        (
            "sk",
            ("Skutočný prípad, nie ukážkové dáta.", "zverejnený z jeho vlastného rozhodnutia"),
            (
                "Model je schematický a zostavený z textu lekárskych správ.",
                "Nezobrazuje skutočné snímky.",
                "Nie sú predpisom ani lekárskou radou.",
                "nie tieto konkrétne cviky",
            ),
        ),
        (
            "en",
            ("A real case, not demo data.", "published by his own choice"),
            (
                "The model is schematic and built from the text of the medical reports.",
                "It does not show real scans.",
                "not a prescription and not medical advice",
                "not these specific exercises",
            ),
        ),
    ],
)
async def test_use_case_page_says_real_data_limits_and_sources(lang, real, limits):
    html = await _page(lang)
    for text in (*real, *limits):
        assert text in html, text
    # five source documents, by type and date only: no link inside the list
    listing = html[
        html.index('<ul class="docs">') : html.index("</ul>", html.index('class="docs"'))
    ]
    assert listing.count("<li>") == 5 and "<a" not in listing
    # each one carries its date (the dates themselves are not repeated in this test)
    assert listing.count("<span>") == 5 and all(
        re.search(r"\d", item) for item in re.findall(r"<span>(.*?)</span>", listing)
    )
    assert 'href="/movement-atlas"' in html
    # kept apart from the invented demo patient
    assert 'href="/demo"' in html
    assert "Jana Testová" not in html and "Jane Doe" not in html
    # where the data goes: the accurate statement, not "never leaves the EU"
    assert ("separate service" in html) if lang == "en" else ("samostatná služba" in html)


async def test_english_page_says_the_atlas_ui_is_slovak():
    assert "The atlas interface is in Slovak only." in await _page("en")


# ── Landing section ──────────────────────────────────────────────────────


def test_landing_section_sits_right_after_the_dashboard_section():
    dashboard_end = LANDING.index("</section>", LANDING.index('id="dashboard"'))
    between = LANDING[dashboard_end : LANDING.index('id="atlas"')]
    assert "<section" in between and between.count("<section") == 1
    assert LANDING.index('id="atlas"') < LANDING.index('id="privacy"')


def test_landing_section_is_bilingual_with_limits_and_real_data_note():
    block = _section("atlas")
    for sk, en in (
        ("Nielen onkológia: rozumieť vlastnému nálezu", "Not only oncology: understand your own"),
        ("Pozrieť, ako to vzniklo", "See how it was made"),
        ("Vyskúšať interaktívny atlas", "Try the interactive atlas"),
        ("Skutočný prípad autora Oncofiles", "A real case from the author of Oncofiles"),
        ("Nie je to vymyslená pacientka z ukážky.", "This is not the invented patient from"),
        ("Model je schematický, zostavený z textu správ, nie zo snímok.", "The model is schematic"),
        ("nie predpis ani lekárska rada", "not a prescription or medical advice"),
        ("nenahrádza lekára ani fyzioterapeuta", "does not replace a doctor or a physio"),
        ("Video (82 sekúnd, bez zvuku)", "The video (82 seconds, no sound)"),
    ):
        assert sk in block and en in block, (sk, en)
    assert "(the atlas interface is in Slovak only)" in block
    assert len(re.findall(r"\sdata-sk=", block)) == len(re.findall(r"\sdata-en=", block))
    # every visible text node is switched by the page's language mechanism
    parser = _LooseText()
    parser.feed(block)
    assert not parser.loose, parser.loose
    assert len(re.findall(r'class="atlas-points"', block)) == 1
    points = block[block.index('class="atlas-points"') : block.index("</ul>")]
    assert points.count("<li ") == 3
    assert f'data-sk-href="{SK_PATH}"' in block and f'data-en-href="{EN_PATH}"' in block
    assert 'href="/movement-atlas"' in block
    assert "Jana Testová" not in block and "Jane Doe" not in block


def test_landing_video_markup_and_language_switch():
    block = _section("atlas")
    assert block.count("<video") == 1
    video = re.search(r"<video\b[^>]*>", block).group(0)
    for attr in ("controls", "muted", "playsinline", 'preload="metadata"'):
        assert attr in video, attr
    # Playback is script-driven (so reduced motion can be honoured): the
    # markup carries no autoplay and no loop ATTRIBUTE, only the start point.
    assert not re.search(r"\s(autoplay|loop)\b", video)
    assert 'data-start="12"' in video
    assert 'width="1080"' in video and 'height="1080"' in video
    # the posters are the frame at data-start, so the start is not a visual jump
    assert 'poster="/movement-atlas/poster-sk-start.jpg"' in video
    assert 'src="/movement-atlas/video-sk.mp4"' in video
    assert "#t=" not in video  # the language swap compares src verbatim
    for lang in ("sk", "en"):
        assert f'data-{lang}-src="/movement-atlas/video-{lang}.mp4"' in video
        assert f'data-{lang}-poster="/movement-atlas/poster-{lang}-start.jpg"' in video
        assert f"data-{lang}-label=" in video
    assert 'aria-describedby="atlas-video-alt"' in video and 'id="atlas-video-alt"' in block
    # the video element itself must not be a data-sk element: applyLang would
    # replace its content with text
    assert not re.search(r"\sdata-sk=", video)
    script = LANDING[LANDING.rindex("<script>") :]
    apply_lang = script[script.index("function applyLang") : script.index("function toggleLang")]
    assert "video[data-' + l + '-src]" in apply_lang and "v.load()" in apply_lang
    assert "[data-' + l + '-href]" in apply_lang
    assert "<style>" in LANDING and "aspect-ratio: 1 / 1" in LANDING  # no layout shift


def _landing_video_script() -> str:
    script = LANDING[LANDING.rindex("<script>") :]
    start = script.index("/* Showcase video (#830)")
    return script[start : script.index("var urlLang", start)]


def test_landing_video_plays_by_script_when_visible_from_the_start_point():
    """The contract of the script-driven playback (maintainer request, #830)."""
    js = _landing_video_script()
    assert "getElementById('atlas-video')" in js
    assert "getAttribute('data-start')" in js
    # visible = at least half of the video in the viewport; leaving pauses it
    assert "new IntersectionObserver(" in js and "intersectionRatio >= 0.5" in js
    assert "threshold: [0, 0.5]" in js
    assert "scriptPause = true; v.pause();" in js
    # not allowed with reduced motion or save-data: no observer, no play()
    assert "var auto = !reduceMotion && !saveData" in js
    assert "conn.saveData" in js
    assert js.index("if (!auto) return;\n      new IntersectionObserver") > 0
    try_play = js[js.index("function tryPlay") : js.index("v.addEventListener")]
    assert "if (!auto || !visible || userPaused || !v.paused) return;" in try_play
    # a manual pause is respected: only a `pause` we did not cause sets the flag
    pause = js[js.index("addEventListener('pause'") : js.index("addEventListener('seeking'")]
    assert "if (scriptPause) { scriptPause = false; return; }" in pause
    assert "if (!v.ended) userPaused = true;" in pause
    # the start point is set from script once metadata is there, not by #t=
    assert "addEventListener('loadedmetadata'" in js and "v.readyState < 1" in js
    assert "v.currentTime = start > 0 && start < v.duration ? start : 0" in js
    # the end restarts from the start point; the loop attribute would show the intro
    ended = js[js.index("addEventListener('ended'") : js.index("addEventListener('emptied'")]
    assert "seekToStart();" in ended and "v.play()" in ended
    assert "v.loop" not in js and "setAttribute('loop'" not in js
    # a language swap (load() -> emptied) re-arms the same rules
    emptied = js[js.index("addEventListener('emptied'") : js.rindex("if (!auto) return;")]
    assert "pendingStart = true;" in emptied and "tryPlay();" in emptied
    # every play() promise is caught: a blocked start or a swap is not an error
    assert js.count("v.play()") == js.count("p.catch(function ()") == 2
    # the reduced-motion flag is the page's one, defined before this block
    whole = LANDING[LANDING.rindex("<script>") :]
    assert whole.index("var reduceMotion = ") < whole.index("/* Showcase video (#830)")
    assert "prefers-reduced-motion: reduce" in whole
    # set up before the initial applyLang('en'), so the first swap is covered
    assert whole.index("/* Showcase video (#830)") < whole.index("if (initial === 'en')")


def test_start_posters_are_square_jpegs_and_the_intro_posters_are_still_used():
    for lang in ("sk", "en"):
        data = (ATLAS_DIR / f"poster-{lang}-start.jpg").read_bytes()
        assert data[:3] == b"\xff\xd8\xff" and len(data) < 150_000
        sof = re.search(rb"\xff\xc0..\x08(..)(..)", data, re.S)  # baseline SOF0: height, width
        assert [int.from_bytes(g, "big") for g in sof.groups()] == [1080, 1080]
        # the intro-card poster stays for link previews (og:image, JSON-LD)
        page = atlas_showcase.use_case_page_html(lang)
        assert f"https://oncofiles.com/movement-atlas/poster-{lang}.jpg" in page
        assert f"/movement-atlas/poster-{lang}.jpg" not in LANDING
    assert "/movement-atlas/poster-sk.jpg" in (ATLAS_DIR / "index.html").read_text(encoding="utf-8")
    assert atlas_showcase.VIDEO_START_SECONDS == 12


def test_landing_faq_and_json_ld_answer_the_oncology_only_question():
    faq = _section("faq")
    assert 'data-sk="Je Oncofiles len pre onkologických pacientov?"' in faq
    assert 'data-en="Is Oncofiles only for oncology patients?"' in faq
    assert f'data-en-href="{EN_PATH}"' in faq
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', _load_landing_html(), re.S
    )
    items = [item for b in blocks for item in json.loads(b)]
    faq_ld = next(i for i in items if i["@type"] == "FAQPage")
    answer = next(
        q["acceptedAnswer"]["text"]
        for q in faq_ld["mainEntity"]
        if q["name"] == "Is Oncofiles only for oncology patients?"
    )
    assert answer.startswith("No.") and EN_PATH in answer and "schematic" in answer


def test_landing_meta_description_is_not_oncology_only():
    tag = re.search(r'<meta name="description"[^>]*>', LANDING).group(0)
    assert "aj pre bežné nálezy" in tag and "everyday findings" in tag
    assert tag.count("aj pre bežné nálezy") == 2  # data-sk and the default content


def test_nav_links_to_the_chapter_that_holds_the_section():
    """#849: the bar links the five chapters; the atlas is the third use case."""
    nav = LANDING[LANDING.index('<nav class="top"') : LANDING.index("</nav>")]
    assert '<a class="wide" href="#use-cases"' in nav
    chapter = LANDING[LANDING.index('id="use-cases"') : LANDING.index('id="how-it-works"')]
    assert '<section class="block" id="atlas">' in chapter


# ── Sitemap and llms.txt ─────────────────────────────────────────────────


async def test_sitemap_lists_both_use_case_pages_with_hreflang():
    xml = (await sitemap_xml(_req("/sitemap.xml"))).body.decode()
    for path in (SK_PATH, EN_PATH):
        start = xml.index(f"<loc>https://oncofiles.com{path}</loc>")
        entry = xml[start : xml.index("</url>", start)]
        assert f'hreflang="sk" href="https://oncofiles.com{SK_PATH}"' in entry
        assert f'hreflang="en" href="https://oncofiles.com{EN_PATH}"' in entry
    assert "<loc>https://oncofiles.com/movement-atlas</loc>" in xml  # the atlas is indexable


async def test_llms_txt_mentions_the_use_case_in_both_languages():
    en = (await llms_txt(_req("/llms.txt"))).body.decode()
    sk = (await llms_sk_txt(_req("/llms-sk.txt"))).body.decode()
    assert f"https://oncofiles.com{EN_PATH}" in en and "not medical advice" in en
    for text in (en, sk):
        assert "https://oncofiles.com/movement-atlas\n" in text
    assert "four\northopaedic documents and one MRI description" in en
    assert "štyroch ortopedických dokumentov a jedného popisu MR" in sk
    assert f"https://oncofiles.com{SK_PATH}" in sk and "nie je to lekárska rada" in sk


# ── Privacy and claims: everything published by this feature ────────────


def _published_texts() -> dict[str, str]:
    texts = {
        name: (ATLAS_DIR / name).read_text(encoding="utf-8")
        for name in ("index.html", "atlas.css", "app.js", "ATTRIBUTION.md")
    }
    texts["use-case sk"] = atlas_showcase.use_case_page_html("sk")
    texts["use-case en"] = atlas_showcase.use_case_page_html("en")
    texts["landing #atlas"] = _section("atlas")
    return texts


_IDENTIFIERS = {
    "birth number": re.compile(r"\b\d{6}\s?/\s?\d{3,4}\b"),
    "birth date label": re.compile(r"naroden|dátum nar|date of birth|\bDOB\b|rodné", re.I),
    "insurer": re.compile(r"poisťov|poistenc|insurer|insurance", re.I),
    "address": re.compile(r"\bulica\b|\bbytom\b|\bPSČ\b|\d{3} \d{2} [A-ZŠČŽ]", re.I),
    "doctor title": re.compile(r"\bMUDr|\bMDDr|\bdoc\.|\bprof\.|\bPhD\b|\bDr\.", re.I),
    "institution": re.compile(
        r"nemocnic|poliklinik|\bklinik|hospital|\bclinic\b|s\.\s?r\.\s?o", re.I
    ),
    "e-mail": re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I),
    "drive link": re.compile(r"drive\.google|docs\.google|gdrive|/file/d/|[?&]id=", re.I),
}

_FORBIDDEN_CLAIMS = re.compile(
    r"clinically validated|klinicky (overen|validovan)|photorealis|fotorealis|"
    r"never leaves? the EU|neopust\w* EÚ|diagnostick|\bdiagnostic\b",
    re.I,
)


@pytest.mark.parametrize("label", sorted(_IDENTIFIERS))
def test_published_files_carry_no_identifier(label):
    pattern = _IDENTIFIERS[label]
    for name, text in _published_texts().items():
        if name.startswith("use-case") and label == "institution":
            # the JSON-LD publisher is schema.org "Organization" (Oncofiles itself)
            text = text.replace('"@type": "Organization"', "")
        hit = pattern.search(text)
        assert not hit, f"{label} in {name}: {text[max(0, hit.start() - 30) : hit.end() + 30]!r}"


def test_published_files_make_no_claim_we_cannot_back():
    for name, text in _published_texts().items():
        hit = _FORBIDDEN_CLAIMS.search(text)
        assert not hit, f"{name}: {hit.group(0)!r}"


def test_no_external_host_in_the_published_files():
    for name, text in _published_texts().items():
        for url in re.findall(r"https?://[^\s\"'<>)]+", text):
            assert url.startswith(
                ("https://oncofiles.com", "https://schema.org", "http://www.w3.org/2000/svg")
            ) or url.startswith(
                (
                    "https://github.com/peter-fusek/oncofiles",
                    # the mesh credit (#841): source and licences, links only
                    "https://dbarchive.biosciencedbc.jp/",
                    "https://creativecommons.org/licenses/",
                )
            ), (name, url)


@pytest.mark.parametrize(
    "name",
    [
        "poster-sk.jpg",
        "poster-en.jpg",
        "poster-sk-start.jpg",
        "poster-en-start.jpg",
        "video-sk.mp4",
        "video-en.mp4",
    ],
)
def test_media_metadata_carries_no_personal_data(name):
    """File metadata (not the pixels): no e-mail, home path, GPS or device tag."""
    data = (ATLAS_DIR / name).read_bytes()
    head = data[: data.find(b"mdat")] if name.endswith(".mp4") else data[:20000]
    assert not re.search(rb"[\w.+-]+@[\w-]+\.[a-z]+", head)
    tags = [b"\xa9" + suffix for suffix in (b"xyz", b"mak", b"mod")]  # location, device
    for marker in (b"/Users/", b"/home/", b"Exif", b"GPS", *tags):
        assert marker not in head, marker


# ── Maintainer wording and titles (PR #836 review) ───────────────────────


_FOUR = len("four")
_COUNT_WORDS = {
    "sk": {"štyri": _FOUR, "jeden": True},
    "en": {"four": _FOUR, "one": True},
}
_LEAD = {
    "sk": r"(\w+) ortopedické dokumenty a (\w+) popis MR",
    "en": r"(\w+) orthopaedic documents and (\w+) MRI description",
}
_LIST_KINDS = {"sk": ("Ortopedick", "Popis MR"), "en": ("Orthopaedic", "MRI description")}


def test_prose_says_four_documents_and_one_mri_description():
    """The maintainer's wording (PR #836): the lead names what the list shows."""
    block = _section("atlas")
    assert "Štyri ortopedické dokumenty a jeden popis MR." in block
    assert "Four orthopaedic documents and one MRI description." in block
    sk = atlas_showcase.use_case_page_html("sk")
    en = atlas_showcase.use_case_page_html("en")
    assert sk.count("štyri ortopedické dokumenty a jeden popis MR") >= 4
    assert en.count("four orthopaedic documents and one MRI description") >= 4
    for text in (block, sk, en):
        assert not re.search(r"\b(Tri|tri|Three|three) orth?op", text)
        assert not re.search(r"\bpäť\b|\bfive\b", text)


@pytest.mark.parametrize("lang", ["sk", "en"])
def test_lead_count_equals_the_entries_in_the_source_list(lang):
    """The count in the prose is derived from nothing: this ties it to the
    list, so adding or removing a source document fails until both agree."""
    html = atlas_showcase.use_case_page_html(lang)
    start = html.index('<ul class="docs">')
    entries = re.findall(r"<li>(.*?)<span>", html[start : html.index("</ul>", start)])
    ortho_kind, mri_kind = _LIST_KINDS[lang]
    ortho = sum(e.startswith(ortho_kind) for e in entries)
    mri = sum(e.startswith(mri_kind) for e in entries)
    assert ortho + mri == len(entries), entries

    words = _COUNT_WORDS[lang]
    block = _section("atlas")
    landing_lead = re.search(rf'data-{lang}="(\w+) orth?op[^"]*"', block).group(0)
    leads = re.findall(_LEAD[lang], html, re.I) + re.findall(_LEAD[lang], landing_lead, re.I)
    assert len(leads) >= 5  # meta, og, two body paragraphs, landing lead
    for ortho_word, mri_word in leads:
        assert words[ortho_word.lower()] == ortho, (ortho_word, ortho)
        assert words[mri_word.lower()] == mri, (mri_word, mri)


def test_landing_titles_put_cancer_patients_first_and_add_healthy_people():
    title = re.search(r"<title[^>]*>([^<]+)</title>", LANDING).group(1)
    assert title == "Oncofiles – zdravotné záznamy pre onkologických pacientov aj zdravých"
    en = "Oncofiles – medical records for cancer patients and healthy people"
    assert f'data-en="{en}"' in LANDING
    assert f'<meta property="og:title" content="{en}">' in LANDING
    assert f'<meta name="twitter:title" content="{en}">' in LANDING
    assert len(en) <= 70 and len(title) <= 70  # 66 and 68 characters
    assert en.index("cancer patients") < en.index("healthy people")
    assert title.index("onkologických pacientov") < title.index("zdravých")


def test_atlas_heading_font_is_self_hosted_and_atlas_only():
    css = (ATLAS_DIR / "atlas.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*\{(.*?)\}", css, re.S)
    assert len(faces) == 2
    for face in faces:
        assert "font-family: 'Bricolage Grotesque';" in face
        assert "font-weight: 700;" in face and "font-display: swap;" in face
        assert "unicode-range:" in face
        url = re.search(r"url\(([^)]+)\)", face).group(1)
        assert url.startswith("/fonts/bricolage-grotesque-")
        assert (SRC / "fonts" / url.removeprefix("/fonts/")).read_bytes()[:4] == b"wOF2"
    # every heading weight the atlas uses with this family is the shipped one
    for rule in re.findall(r"font-family:var\(--f-display\);([^}]*)", css):
        assert "font-weight:700" in rule, rule
    assert "Bricolage" not in (SRC / "fonts" / "fonts.css").read_text(encoding="utf-8")
    assert "Bricolage" not in LANDING
