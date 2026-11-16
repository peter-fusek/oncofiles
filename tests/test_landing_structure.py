"""Landing page structure locks (#849 redesign).

The one-pager is five chapters (problem, use cases, how it works, setup,
trust). The section ids of the previous layout are shared publicly (LinkedIn
posts, docs, other pages), so every one of them must stay an anchor.
"""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "oncofiles"
LANDING = (SRC / "landing.html").read_text(encoding="utf-8")
STYLE = LANDING[LANDING.index("<style>") : LANDING.index("</style>")]

CHAPTERS = ["problem", "use-cases", "how-it-works", "setup", "trust"]
# Anchors of the layout before #849: all linked from outside the page.
LEGACY = [
    "top",
    "photos",
    "vision",
    "provenance",
    "folder",
    "ask",
    "dashboard",
    "atlas",
    "privacy",
    "connect",
    "roadmap",
    "family",
    "faq",
    "join",
]
# Which chapter each legacy section lives in, in page order.
CHAPTER_OF = {
    "top": "problem",
    "photos": "problem",
    "ask": "use-cases",
    "dashboard": "use-cases",
    "atlas": "use-cases",
    "folder": "how-it-works",
    "provenance": "how-it-works",
    "vision": "how-it-works",
    "connect": "setup",
    "privacy": "trust",
    "family": "trust",
    "roadmap": "trust",
    "faq": "trust",
    "join": "trust",
}


def _at(anchor: str) -> int:
    return LANDING.index(f'id="{anchor}"')


@pytest.mark.parametrize("anchor", [*CHAPTERS, *LEGACY, "waitlist", "no-lock-in"])
def test_every_public_anchor_exists_exactly_once(anchor):
    assert len(re.findall(rf'\sid="{re.escape(anchor)}"', LANDING)) == 1


def test_ids_are_unique():
    ids = re.findall(r'\sid="([^"]+)"', LANDING[LANDING.index("<body>") :])
    assert len(ids) == len(set(ids)), sorted(i for i in set(ids) if ids.count(i) > 1)


def test_chapters_are_sections_in_story_order():
    order = [_at(c) for c in CHAPTERS]
    assert order == sorted(order)
    for chapter in CHAPTERS:
        tag = LANDING[LANDING.rindex("<", 0, _at(chapter)) : LANDING.index(">", _at(chapter))]
        assert tag.startswith('<section class="chapter'), tag


@pytest.mark.parametrize("anchor", LEGACY)
def test_legacy_section_sits_in_its_chapter(anchor):
    chapter = CHAPTER_OF[anchor]
    start = _at(chapter)
    nxt = CHAPTERS.index(chapter) + 1
    end = _at(CHAPTERS[nxt]) if nxt < len(CHAPTERS) else LANDING.index("</main>")
    assert start <= _at(anchor) < end
    assert set(CHAPTER_OF) == set(LEGACY)


def test_sections_follow_the_story_order():
    order = ["top", "photos", "ask", "dashboard", "atlas", "folder", "provenance", "vision"]
    order += ["connect", "privacy", "family", "roadmap", "faq", "join"]
    at = [_at(a) for a in order]
    assert at == sorted(at)


def test_atlas_comes_directly_after_dashboard():
    end = LANDING.index("</section>", _at("dashboard"))
    between = LANDING[end : _at("atlas")]
    assert between.count("<section") == 1


def test_exactly_one_h1_and_it_states_the_problem():
    h1 = re.findall(r"<h1\b[^>]*>", LANDING)
    assert len(h1) == 1
    assert 'data-sk="Stoh lekárskych správ ešte nie je informácia."' in h1[0]
    assert 'data-en="A stack of medical reports is not information."' in h1[0]
    # the previous headline stays visible as the tagline (the page's search phrase)
    hero = LANDING[_at("top") : _at("photos")]
    assert LANDING.index("<h1") > hero.index('class="tagline"') + _at("top") - 1
    assert 'data-en="Medical records where every number has a source"' in hero
    assert 'data-sk="Zdravotné záznamy, v ktorých má každé číslo svoj zdroj"' in hero


def test_heading_levels_chapters_h2_sections_h3():
    for chapter in CHAPTERS:
        nxt = CHAPTERS.index(chapter) + 1
        end = _at(CHAPTERS[nxt]) if nxt < len(CHAPTERS) else LANDING.index("</main>")
        assert len(re.findall(r"<h2\b", LANDING[_at(chapter) : end])) == 1, chapter
    for anchor in LEGACY[1:]:
        if anchor in ("photos", "connect"):  # these carry their chapter's h2
            continue
        block = LANDING[_at(anchor) : LANDING.index("</section>", _at(anchor))]
        assert re.search(r"<h3\b", block), anchor
        assert not re.search(r"<h2\b", block), anchor


def test_every_bilingual_attribute_has_its_twin():
    for tag in re.findall(r"<[a-zA-Z][^>]*\sdata-(?:sk|en)[=-][^>]*>", LANDING):
        for kind in ("", "-label", "-alt", "-title", "-href", "-src", "-poster"):
            sk = re.search(rf'\sdata-sk{kind}="', tag) is not None
            en = re.search(rf'\sdata-en{kind}="', tag) is not None
            assert sk == en, tag[:160]
    for attr in ("placeholder", "label"):
        sk = len(re.findall(rf'\sdata-{attr}-sk="', LANDING))
        assert sk == len(re.findall(rf'\sdata-{attr}-en="', LANDING))


@pytest.mark.parametrize(
    "href,sk,en",
    [
        ("#problem", "Problém", "Problem"),
        ("#use-cases", "Príklady", "Use cases"),
        ("#how-it-works", "Ako to funguje", "How it works"),
        ("#setup", "Nastavenie", "Setup"),
        ("#trust", "Dôvera", "Trust"),
    ],
)
def test_nav_has_the_five_chapters_in_both_languages(href, sk, en):
    nav = LANDING[LANDING.index('<nav class="top"') : LANDING.index("</nav>")]
    assert f'<a class="wide" href="{href}" data-sk="{sk}" data-en="{en}">{sk}</a>' in nav
    assert nav.count('class="wide"') == 5
    assert 'id="lang-toggle"' in nav and 'href="#join"' in nav
    assert nav.count('class="wide"') == 5
    # each chapter repeats its nav word as its label
    assert f'data-sk="{sk}" data-en="{en}">{sk}</span></p>' in LANDING


def test_skip_link_and_focus_styles():
    body = LANDING[LANDING.index("<body>") :]
    assert body.index('class="skip" href="#main"') < body.index("<header")
    assert '<main id="main">' in LANDING
    assert ":focus-visible { outline: 3px solid var(--focus)" in STYLE
    assert "outline: none" not in STYLE and "outline: 0" not in STYLE


# ── Visual identity: Instarea palette, self-hosted fonts, flat colour ─────

BRAND = {
    "--navy": "#000059",
    "--navy-deep": "#000040",
    "--coral": "#ff4646",
    "--turq": "#33d6d6",
    "--peach": "#ffd0a8",
}


def test_brand_tokens_are_the_exact_instarea_values():
    for token, value in BRAND.items():
        assert f"{token}: {value};" in STYLE, token
    for old in ("--accent:", "--accent-ink", "DM Serif Display", "--serif"):
        assert old not in LANDING, old  # the cream/teal/serif look is gone


def test_fonts_are_self_hosted_families_only():
    assert "--display: 'Outfit'," in STYLE and "--sans: 'DM Sans'," in STYLE
    assert "--num: 'IBM Plex Sans'," in STYLE
    for host in ("fonts.googleapis.com", "fonts.gstatic.com", "use.typekit", "cdn."):
        assert host not in LANDING, host
    assert "@import" not in LANDING
    # the only faces declared in the page are metric-matched fallbacks built
    # from local fonts (no download); the web fonts live in /fonts/fonts.css
    faces = re.findall(r"@font-face \{([^}]*)\}", STYLE)
    assert len(faces) == 4
    for face in faces:
        assert "Fallback';" in face and "size-adjust:" in face
        assert "src: local(" in face and "url(" not in face
    assert "'Outfit', 'Outfit Fallback'," in STYLE and "'DM Sans', 'DM Sans Fallback'," in STYLE
    for href in re.findall(r'<link rel="preload" href="([^"]+)"', LANDING):
        assert (SRC / "fonts" / href.removeprefix("/fonts/")).is_file(), href
    # the brand's own typefaces are proprietary: never referenced
    assert "Setup Grotesk" not in LANDING and "Proxima" not in LANDING


def test_no_external_host_is_loaded():
    for attr, url in re.findall(r'\s(src|poster)="([^"]+)"', LANDING):
        assert url.startswith("/"), (attr, url)
    for url in re.findall(r'<link[^>]+href="([^"]+)"', LANDING):
        assert url.startswith(("/", "https://oncofiles.com/")), url
    assert not re.search(r"url\(\s*['\"]?https?:", LANDING)


def test_flat_colour_only():
    for banned in ("gradient", "backdrop-filter", "blur(", "text-shadow"):
        assert banned not in STYLE, banned


def _luminance(colour: str) -> float:
    r, g, b = (channel / 0xFF for channel in bytes.fromhex(colour.removeprefix("#")))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(rule_start: str) -> dict[str, str]:
    block = STYLE[STYLE.index(rule_start) :]
    block = block[: block.index("}")]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b", block))


@pytest.mark.parametrize("surface", [":root {", ':root[data-theme="dark"] {', ".tone-navy {"])
def test_text_tokens_meet_wcag_aa_on_their_surface(surface):
    """Body text, muted text and the three verification states reach 4.5:1 on
    the page, the alternate band and the card of each surface."""
    t = {**_tokens(":root {"), **_tokens(surface)}
    for ink in ("--ink", "--ink-2", "--ink-3", "--ok", "--human", "--warn", "--err", "--link"):
        for bg in ("--bg", "--bg-2", "--card"):
            assert _contrast(t[ink], t[bg]) >= 4.5, (surface, ink, bg, t[ink], t[bg])


def test_call_to_action_and_brand_accents_are_readable():
    # the coral button carries navy text (white on brand coral is only 3.4:1)
    assert "background: var(--coral); color: #000059;" in STYLE
    assert _contrast("#000059", BRAND["--coral"]) >= 4.5
    assert _contrast("#000059", "#ff6b6b") >= 4.5  # hover
    # turquoise and peach only ever sit under navy text
    assert _contrast("#000059", BRAND["--turq"]) >= 4.5
    assert _contrast("#000059", BRAND["--peach"]) >= 4.5
    # the brand coral and turquoise are never a text colour on a light surface
    light = _tokens(":root {")
    assert BRAND["--coral"] not in {light[k] for k in ("--ink", "--ink-2", "--ink-3", "--link")}


def test_verification_states_differ_by_shape_not_only_colour():
    assert ".st-human::before { background: var(--human); border-radius: 0;" in STYLE
    assert ".st-unverified::before { background: transparent; border:" in STYLE
    assert ".cd-h { background: #3538cd; border-radius: 0; transform: rotate(45deg); }" in STYLE
    # and every mark has its words next to it
    for sk in ("overené strojom", "overené človekom", "neoverené"):
        assert LANDING.count(sk) >= 4


def test_dark_mode_and_reduced_motion_are_kept():
    assert "@media (prefers-color-scheme: dark)" in STYLE
    assert ':root:not([data-theme="light"])' in STYLE and ':root[data-theme="dark"]' in STYLE
    assert STYLE.count("prefers-reduced-motion: reduce") >= 3
    assert "html { scroll-behavior: auto; }" in STYLE
    assert "scroll-padding-top:" in STYLE  # anchors land below the sticky bar
    assert "header.site { position: sticky; top: 0;" in STYLE


# ── FAQ: the structured data says what the page shows ─────────────────────


class _Faq(HTMLParser):
    """English question/answer pairs of the visible FAQ. A link contributes
    "label: absolute URL", so the structured answer carries the same target."""

    def __init__(self):
        super().__init__()
        self.items: list[list] = []
        self.where = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "details":
            self.items.append(["", []])
        elif tag == "summary":
            self.where = "q"
        elif tag == "div" and a.get("class") == "a":
            self.where = "a"
        if "data-en" not in a or not self.items:
            return
        if self.where == "q" and tag == "h4":
            self.items[-1][0] = a["data-en"]
        elif self.where == "a":
            text = a["data-en"]
            if tag == "a":
                url = a.get("data-en-href") or a["href"]
                text += ": " + ("https://oncofiles.com" + url if url.startswith("/") else url)
            self.items[-1][1].append(text)


def _visible_faq() -> list[tuple[str, str]]:
    start = LANDING.index('<div class="faq">')
    parser = _Faq()
    parser.feed(LANDING[start : LANDING.index("<!-- /faq -->", start)])
    return [(q, " ".join(parts)) for q, parts in parser.items]


def _faq_json_ld() -> list[tuple[str, str]]:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', LANDING, re.S)
    items = [item for b in blocks for item in json.loads(b)]
    faq = next(i for i in items if i["@type"] == "FAQPage")
    return [(q["name"], q["acceptedAnswer"]["text"]) for q in faq["mainEntity"]]


def test_faq_json_ld_equals_the_visible_faq():
    visible, structured = _visible_faq(), _faq_json_ld()
    assert len(visible) == 14
    assert visible == structured


def test_faq_keeps_every_question_of_both_earlier_lists():
    questions = {q for q, _ in _visible_faq()}
    for q in (
        # shown on the page before #849
        "Is the content of my documents sent to a cloud AI?",
        "Where are my documents and data stored?",
        "What do “machine-verified”, “unverified” and “human-verified” mean?",
        "Will Oncofiles delete anything in my Drive?",
        "When can I connect my own Google Drive?",
        "Is my data safe if I use ChatGPT Free or Plus?",
        "Does it work with Slovak medical reports?",
        "How much does it cost and who pays for it?",
        "Can I self-host Oncofiles?",
        "Is Oncofiles only for oncology patients?",
        "Does Oncofiles give medical advice?",
        # only in the structured data before #849
        "How do I know a lab value in Oncofiles is correct?",
        "What happens to my Google Drive folder?",
        "How do I connect Oncofiles to Claude or ChatGPT?",
    ):
        assert q in questions, q
    start = LANDING.index('<div class="faq">')
    faq = LANDING[start : LANDING.index("<!-- /faq -->", start)]
    assert faq.count("<details") == 14 == faq.count("<summary>")
    assert len(re.findall(r"\sdata-sk=", faq)) == len(re.findall(r"\sdata-en=", faq))
    assert html.unescape(faq).count("oncofiles.com/mcp") == 3  # one answer: SK, EN, shown text


# ── Truth labels that must survive any restyle ────────────────────────────


@pytest.mark.parametrize(
    "anchor,sk,en",
    [
        ("ask", "ukážka na vymyslených údajoch", "shown on invented data"),
        ("dashboard", "ukážka na vymyslených údajoch", "shown on invented data"),
        ("atlas", "Skutočný prípad autora", "Real case of the author"),
        ("atlas", "zverejnený z jeho vlastného rozhodnutia", "published by his own choice"),
    ],
)
def test_each_use_case_says_what_kind_of_proof_it_is(anchor, sk, en):
    block = LANDING[_at(anchor) : LANDING.index("</section>", _at(anchor))]
    label = block[block.index('<p class="proof">') : block.index("</p>")]
    assert f'data-sk="{sk}" data-en="{en}"' in label


@pytest.mark.parametrize(
    "text",
    [
        "Oncofiles does not replace your doctor.",
        "Oncofiles nenahrádza lekára.",
        'data-en="coming soon"',
        "The design is done; no hospital is connected yet.",
        "free up to 500 documents per patient",
        "zadarmo do 500 dokumentov na pacienta",
        "documents per patient free, no credit card",
        "Invented patient.",
        "All data here is invented.",
        "our database",
        "The atlas does not replace a doctor or a physiotherapist.",
    ],
)
def test_disclaimers_and_truth_labels_are_kept(text):
    assert text in LANDING


def test_figures_repeat_claims_made_in_the_text():
    head = LANDING[_at("trust") : _at("privacy")]
    assert re.findall(r"<li><b>([^<]+)</b>", head) == ["500", "MIT", "28"]
    assert "which has shipped 28 products since 2008." in LANDING
    assert "dodala 28 produktov" in LANDING and "products shipped by Instarea since 2008" in LANDING
    assert not re.search(r"\b23 (products|produktov)", LANDING)
    assert "open under the MIT license" in LANDING


# ── Bar: Demo and Sign in, and the menu on narrow screens ─────────────────


def _nav() -> str:
    return LANDING[LANDING.index('<nav class="top"') : LANDING.index("</nav>")]


def test_nav_has_demo_and_sign_in_in_both_languages():
    nav = _nav()
    demo = re.search(r'<a class="nav-demo" href="/demo"[^>]*>([^<]*)</a>', nav)
    assert demo and demo.group(1) == "Demo"
    assert 'data-sk="Demo" data-en="Demo"' in demo.group(0)
    assert 'data-sk-title="Ukážka bez prihlásenia"' in demo.group(0)
    assert 'data-en-title="Demo, no sign-in needed"' in demo.group(0)
    sign = re.search(r'<a class="nav-signin" href="/dashboard"[^>]*>([^<]*)</a>', nav)
    assert sign and sign.group(1) == "Prihlásiť sa"
    assert 'data-sk="Prihlásiť sa" data-en="Sign in"' in sign.group(0)


def test_nav_order_is_chapters_demo_sign_in_language_button():
    """DOM order is the keyboard order: menu button, five chapters, Demo,
    Sign in, language, waitlist."""
    nav = _nav()
    marks = [
        'id="menu-toggle"',
        'href="#problem"',
        'href="#use-cases"',
        'href="#how-it-works"',
        'href="#setup"',
        'href="#trust"',
        'href="/demo"',
        'href="/dashboard"',
        'id="lang-toggle"',
        'href="#join"',
    ]
    at = [nav.index(m) for m in marks]
    assert at == sorted(at)
    assert "tabindex" not in nav


def test_menu_is_a_disclosure_without_inline_handlers():
    nav = _nav()
    button = re.search(r'<button id="menu-toggle"[^>]*>', nav).group(0)
    assert 'aria-expanded="false"' in button and 'aria-controls="nav-links"' in button
    assert 'data-action="toggleMenu"' in button and 'type="button"' in button
    assert '<div class="nav-links" id="nav-links">' in nav
    panel = nav[nav.index('id="nav-links"') : nav.index("</div>")]
    assert panel.count("<a ") == 7  # five chapters, Demo, Sign in
    script = LANDING[LANDING.rindex("<script>") :]
    assert "toggleMenu: toggleMenu" in script
    assert "btn.setAttribute('aria-expanded', open ? 'true' : 'false');" in script
    assert "if (e.key === 'Escape') { closeBug(); closeMenu(true); }" in script
    assert "if (!e.target.closest('#menu-toggle')) closeMenu(false);" in script
    # wide screens show the links in the bar and hide the button
    wide = STYLE[STYLE.index("@media (min-width: 1100px) {\n      .menu-btn { display: none; }") :]
    assert ".nav-links, .nav-links.open { display: flex; position: static;" in wide[:400]
    assert ".nav-links { display: none; position: absolute;" in STYLE
    assert ".nav-links.open { display: flex; }" in STYLE


def test_footer_repeats_demo_and_sign_in():
    footer = LANDING[LANDING.index("<footer") : LANDING.index("</footer>")]
    assert '<a href="/demo" data-sk="Ukážka" data-en="Demo">' in footer
    assert '<a href="/dashboard" data-sk="Prihlásiť sa" data-en="Sign in">' in footer


# ── Deep links: the target is restored after fonts and load ───────────────


def _hash_guard() -> str:
    script = LANDING[LANDING.rindex("<script>") :]
    start = script.index("/* Deep links (#849)")
    return script[start : script.index("/* Showcase video (#830)")]


def test_deep_link_guard_scrolls_again_after_fonts_and_load():
    js = _hash_guard()
    assert "document.fonts.ready.then(settleHash)" in js
    assert "window.addEventListener('load'," in js and "setTimeout(settleHash, 400)" in js
    assert "document.getElementById(decodeURIComponent(location.hash.slice(1)))" in js
    # no animation; the sticky-bar offset comes from scroll-padding-top
    assert "target.scrollIntoView({ behavior: 'instant', block: 'start' });" in js
    assert "scroll-padding-top: 76px;" in STYLE


def test_deep_link_guard_never_runs_after_user_interaction():
    js = _hash_guard()
    assert "if (userActed || location.hash.length < 2) return;" in js
    events = "['wheel', 'touchstart', 'keydown', 'pointerdown', 'mousedown']"
    assert events in js
    assert "window.addEventListener(type, markActed, { capture: true, passive: true });" in js
    assert "function markActed() { userActed = true; }" in js
    assert js.count("userActed = ") == 2  # initial false, set true once: never reset
