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
        ("three.min.js", "application/javascript; charset=utf-8"),
        ("poster-sk.jpg", "image/jpeg"),
        ("poster-en.jpg", "image/jpeg"),
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


@pytest.mark.parametrize("name", ["index.html", "../server.py", "video.mp4", "", "three.js"])
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
    assert sum(p.stat().st_size for p in ATLAS_DIR.iterdir()) < 9_000_000


# ── The hosted atlas page and its CSP ────────────────────────────────────


async def test_atlas_page_is_served_without_inline_code():
    response = await movement_atlas_page(_req("/movement-atlas"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.body.decode()
    assert not re.search(r"\son[a-z]+\s*=", html), "no inline event handlers"
    assert "<style" not in html and "style=" not in html
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S)
    assert [attrs.strip() for attrs, _ in scripts] == [
        'src="/movement-atlas/three.min.js"',
        'src="/movement-atlas/app.js"',
    ]
    assert all(not body.strip() for _, body in scripts)
    # every reference stays on this origin (no CDN, no font host)
    assert not re.search(r"""(?:src|href)=["'](?:https?:)?//(?!oncofiles\.com/)""", html)
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
    assert "@license" in (ATLAS_DIR / "three.min.js").read_text(encoding="utf-8")[:200]


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
    assert "autoplay" not in html and "loop" not in video
    assert 'width="1080"' in video and 'height="1080"' in video
    assert f'poster="/movement-atlas/poster-{lang}.jpg"' in video
    assert f'src="/movement-atlas/video-{lang}.mp4"' in video
    assert 'aria-describedby="video-alt"' in video and 'id="video-alt"' in html

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
    assert "autoplay" not in block and "loop" not in video
    assert 'width="1080"' in video and 'height="1080"' in video
    assert 'poster="/movement-atlas/poster-sk.jpg"' in video
    assert 'src="/movement-atlas/video-sk.mp4"' in video
    for lang in ("sk", "en"):
        assert f'data-{lang}-src="/movement-atlas/video-{lang}.mp4"' in video
        assert f'data-{lang}-poster="/movement-atlas/poster-{lang}.jpg"' in video
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


def test_nav_links_to_the_section_on_wide_screens_only():
    nav = LANDING[LANDING.index('<nav class="top"') : LANDING.index("</nav>")]
    assert '<a class="wide" href="#atlas"' in nav


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
    assert "three\northopaedic reports and one MRI description" in en
    assert "z troch\nortopedických správ a jedného popisu MR" in sk
    assert f"https://oncofiles.com{SK_PATH}" in sk and "nie je to lekárska rada" in sk


# ── Privacy and claims: everything published by this feature ────────────


def _published_texts() -> dict[str, str]:
    texts = {
        name: (ATLAS_DIR / name).read_text(encoding="utf-8")
        for name in ("index.html", "atlas.css", "app.js")
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
            ) or url.startswith("https://github.com/peter-fusek/oncofiles"), (name, url)


@pytest.mark.parametrize("name", ["poster-sk.jpg", "poster-en.jpg", "video-sk.mp4", "video-en.mp4"])
def test_media_metadata_carries_no_personal_data(name):
    """File metadata (not the pixels): no e-mail, home path, GPS or device tag."""
    data = (ATLAS_DIR / name).read_bytes()
    head = data[: data.find(b"mdat")] if name.endswith(".mp4") else data[:20000]
    assert not re.search(rb"[\w.+-]+@[\w-]+\.[a-z]+", head)
    tags = [b"\xa9" + suffix for suffix in (b"xyz", b"mak", b"mod")]  # location, device
    for marker in (b"/Users/", b"/home/", b"Exif", b"GPS", *tags):
        assert marker not in head, marker


# ── Maintainer wording and titles (PR #836 review) ───────────────────────


def test_prose_says_three_reports_and_one_mri_description():
    """The maintainer's wording for the lead. The source list on the use-case
    page stays one entry per document."""
    block = _section("atlas")
    assert "Tri ortopedické správy a jeden popis MR." in block
    assert "Three orthopaedic reports and one MRI description." in block
    sk = atlas_showcase.use_case_page_html("sk")
    en = atlas_showcase.use_case_page_html("en")
    assert sk.count("tri ortopedické správy a jeden popis MR") >= 3
    assert en.count("three orthopaedic reports and one MRI description") >= 3
    for text in (block, sk, en):
        assert not re.search(r"Štyri|štyri|\bFour\b|\bfour\b|\bpäť\b|\bfive\b", text)


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
