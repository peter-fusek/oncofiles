"""The "movement atlas" showcase (#830): Oncofiles is not only for oncology.

Three public things live here, all static (no DB, no model, no Drive):

* the hosted interactive atlas (``/movement-atlas`` + its assets in
  ``src/oncofiles/atlas/``): a three.js page a Claude session built from the
  orthopaedic documents of the Oncofiles author, read through the MCP connector;
* the use-case page in Slovak (``/pouzitie/pohybovy-atlas``) and English
  (``/use-cases/movement-atlas``), path-based like ``/onkologia`` + ``/oncology``;
* the two small videos and posters the landing section ``#atlas`` plays
  (script-driven there: starts when visible, from ``VIDEO_START_SECONDS``;
  the script lives in ``landing.html``). The use-case pages never start the
  video by themselves; ``_START_SCRIPT`` only moves the first play to the
  same start point.

Privacy: this is the author's own health data, published by his decision
(2026-11-15), and every page says so. It is kept apart from the invented demo
patient. Nothing here may name a doctor or an institution, link a document, or
carry a birth number, address or insurer code (``tests/test_atlas_showcase.py``).
The source documents are listed by type and date only.

Claims: the model is schematic and built from report text, the exercises are
common examples for a physiotherapist to review. Never "diagnostic",
"photorealistic" or "clinically validated".
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.responses import FileResponse, Response

ATLAS_DIR = Path(__file__).parent / "atlas"

ATLAS_PATH = "/movement-atlas"
USE_CASE_PATHS = {"sk": "/pouzitie/pohybovy-atlas", "en": "/use-cases/movement-atlas"}
SITE = "https://oncofiles.com"
VIDEO_SECONDS = 82
VIDEO_SIDE_PX = 1080
# Where the video starts on its first play: the rotating model with muscles,
# not the 3-second intro card. The `poster-{lang}-start.jpg` files are this
# frame, so the <video> does not jump when it starts. The intro-card posters
# (`poster-{lang}.jpg`) stay as og:image / JSON-LD thumbnails.
VIDEO_START_SECONDS = 12
PUBLISHED = "2026-11-15"

_DAY = "public, max-age=86400"
_MONTH = "public, max-age=2592000"
_JS = "application/javascript; charset=utf-8"

# Fixed allowlist: only these names are served, so no path reaches the
# filesystem. three.min.js is the vendored r149 build and never changes; the
# media names are not versioned, so they get a month and revalidate by ETag.
ASSETS: dict[str, tuple[str, str]] = {
    "atlas.css": ("text/css; charset=utf-8", _DAY),
    "app.js": (_JS, _DAY),
    "three.min.js": (_JS, "public, max-age=31536000, immutable"),
    "poster-sk.jpg": ("image/jpeg", _MONTH),
    "poster-en.jpg": ("image/jpeg", _MONTH),
    "poster-sk-start.jpg": ("image/jpeg", _MONTH),
    "poster-en-start.jpg": ("image/jpeg", _MONTH),
    "video-sk.mp4": ("video/mp4", _MONTH),
    "video-en.mp4": ("video/mp4", _MONTH),
}

# The atlas page gets its own CSP (per-route, #534): scripts and styles only
# from this origin, no inline script and no inline handler. `style-src-attr`
# stays open because app.js writes legend swatches as `style="background:…"`
# and the labels are positioned through the CSSOM.
ATLAS_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "script-src-attr 'none'; "
    "style-src 'self'; "
    "style-src-attr 'unsafe-inline'; "
    "font-src 'self'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'none'; "
    "upgrade-insecure-requests"
)

_ATLAS_HTML: str | None = None


def atlas_page_html() -> str:
    global _ATLAS_HTML  # noqa: PLW0603
    if _ATLAS_HTML is None:
        _ATLAS_HTML = (ATLAS_DIR / "index.html").read_text(encoding="utf-8")
    return _ATLAS_HTML


def asset_response(name: str) -> Response:
    """One allowlisted file. FileResponse answers ``Range`` with 206 (Safari
    needs that to play a <video>) and adds ETag / Last-Modified."""
    spec = ASSETS.get(name)
    if spec is None:
        return Response("not found", status_code=404)
    media_type, cache_control = spec
    return FileResponse(
        ATLAS_DIR / name,
        media_type=media_type,
        headers={"Cache-Control": cache_control, "X-Content-Type-Options": "nosniff"},
    )


# ── Use-case page ─────────────────────────────────────────────────────────

_CSS = """
:root { color-scheme: light dark;
  --bg:#f6f4ef; --ink:#1c232d; --ink-2:#4a5462; --ink-3:#7b8592; --rule:#dcd5c9; --rule-2:#c9c1b3;
  --accent:#0f6b63; --accent-ink:#ffffff; --warn:#9a5507;
  --serif:'DM Serif Display',Georgia,serif;
  --sans:'DM Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#151918; --ink:#e6eae7; --ink-2:#b3bdb8; --ink-3:#85918c; --rule:#2f3836; --rule-2:#3c4744;
  --accent:#5ccfc0; --accent-ink:#0c1c1a; --warn:#f0b75c; } }
:root[data-theme="dark"] {
  --bg:#151918; --ink:#e6eae7; --ink-2:#b3bdb8; --ink-3:#85918c; --rule:#2f3836; --rule-2:#3c4744;
  --accent:#5ccfc0; --accent-ink:#0c1c1a; --warn:#f0b75c; }
* { margin:0; padding:0; box-sizing:border-box; }
html { overflow-x:hidden; }
body { font:400 1.0625rem/1.65 var(--sans); background:var(--bg); color:var(--ink);
  overflow-x:hidden; -webkit-font-smoothing:antialiased; }
a { color:var(--accent); text-underline-offset:2px; }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.wrap { max-width:760px; margin:0 auto; padding:0 16px; }
@media (min-width:760px) { .wrap { padding:0 32px; } }
header.site { border-bottom:1px solid var(--rule); }
header.site .wrap { display:flex; align-items:baseline; gap:18px; padding-top:18px;
  padding-bottom:14px; }
.logo { font:1.45rem var(--serif); color:var(--ink); text-decoration:none;
  letter-spacing:-0.01em; }
.logo span { color:var(--accent); }
.lang { margin-left:auto; border:1px solid var(--rule-2); border-radius:4px; padding:1px 8px;
  font-size:0.82rem; color:var(--ink-2); text-decoration:none; }
main { padding:40px 0 56px; }
h1, h2 { font-family:var(--serif); font-weight:400; letter-spacing:-0.015em; }
h1 { font-size:clamp(2rem,5.4vw,3rem); line-height:1.08; }
h2 { font-size:clamp(1.4rem,3vw,1.75rem); line-height:1.15; margin:44px 0 12px; }
p { margin-top:12px; color:var(--ink-2); }
.lede { font-size:1.15rem; margin-top:16px; }
ul, ol { margin:12px 0 0 1.25em; color:var(--ink-2); }
li { margin-top:6px; }
li b, p b { color:var(--ink); font-weight:600; }
.real { margin-top:22px; padding:14px 16px; border:1px solid var(--rule-2); border-radius:10px;
  font-size:0.95rem; }
.real p:first-child, .limits p:first-child { margin-top:0; }
figure { margin:28px 0 0; }
video { display:block; width:100%; max-width:560px; height:auto; aspect-ratio:1 / 1;
  border-radius:12px; background:#0c1a1d; }
figcaption { margin-top:10px; font-size:0.92rem; color:var(--ink-3); max-width:60ch; }
.docs { list-style:none; margin-left:0; border-top:1px solid var(--rule); }
.docs li { display:flex; flex-wrap:wrap; justify-content:space-between; gap:2px 16px; margin:0;
  padding:9px 0; border-bottom:1px solid var(--rule); }
.docs span { color:var(--ink-3); font-variant-numeric:tabular-nums; }
.limits { margin-top:14px; padding:16px 18px; border:2px solid var(--warn); border-radius:10px; }
.limits p { color:var(--ink); }
.btn { display:inline-block; background:var(--accent); color:var(--accent-ink); border-radius:6px;
  padding:11px 18px; font:600 0.95rem var(--sans); text-decoration:none; }
.btn:hover { filter:brightness(1.08); }
.try { margin-top:20px; display:flex; flex-wrap:wrap; align-items:center; gap:10px 16px; }
.try small { color:var(--ink-3); font-size:0.88rem; }
footer { border-top:1px solid var(--rule); padding:24px 0 64px; color:var(--ink-3);
  font-size:0.85rem; }
footer p { color:var(--ink-3); max-width:72ch; margin:0 0 10px; }
footer a { color:var(--ink-2); }
"""

_COPY: dict[str, dict] = {
    "sk": {
        "html_lang": "sk",
        "og_locale": "sk_SK",
        "alt": "en",
        "alt_label": "EN",
        "title": "Pohybový atlas: z lekárskych správ interaktívne 3D vysvetlenie | Oncofiles",
        "description": (
            "Oncofiles nie je len pre onkologických pacientov. Skutočný prípad: Claude si cez "
            "Oncofiles prečítal štyri ortopedické dokumenty a jeden popis MR a postavil z nich 3D "
            "atlas chrbta, kolena a ramena s návrhmi cvikov pre fyzioterapeuta."
        ),
        "video_name": "Pohybový atlas: z lekárskych správ interaktívne 3D vysvetlenie",
        "video_label": "Video: ukážka pohybového atlasu",
        "h1": "Z lekárskych správ interaktívne 3D vysvetlenie",
        "lede": (
            "Oncofiles nie je len pre onkologických pacientov. Inak zdravý človek ho môže "
            "použiť ako zdroj faktov pre vysvetlenie bežného zdravotného problému a pre návrhy "
            "cvikov, ktoré si vezme k fyzioterapeutovi."
        ),
        "real": (
            "<p><b>Skutočný prípad, nie ukážkové dáta.</b> Ide o vlastný nález Petra Fuseka, "
            "autora Oncofiles, zverejnený z jeho vlastného rozhodnutia. "
            'Vymyslená pacientka z <a href="/demo">ukážky</a> s tým nemá nič spoločné.</p>'
        ),
        "video_alt": (
            "Video (82 sekúnd, bez zvuku) prejde tri oblasti: driekovú chrbticu, pravé koleno a "
            "ľavé rameno. Pri každej strieda Petrov nález so zdravým stavom, najprv na úrovni "
            "kĺbu a kostí, potom svalov a šliach, a ukáže dva animované cviky."
        ),
        "sections": [
            (
                "Problém",
                "<p>Tri bežné ťažkosti: spodná časť chrbta (prechod L5/S1), preskakovanie v pravom "
                "kolene a bolesť ľavého ramena pri zdvihnutí ruky. K nim štyri ortopedické "
                "dokumenty a jeden popis MR, písané lekárskym jazykom. Z textu si človek ťažko "
                "predstaví, čo "
                "nález znamená pri pohybe a na čo sa opýtať fyzioterapeuta.</p>",
            ),
            (
                "Čo dodal Oncofiles",
                "<p>Profil v Oncofiles je typu „všeobecný“ (nie onkologický) a obsahuje 232 "
                "dokumentov. Claude si cez konektor Oncofiles (MCP) prečítal štyri ortopedické "
                "dokumenty a jeden popis MR a vybral z nich fakty: diagnózy, nálezy a odporúčania, "
                "každý s odkazom na dokument, "
                "z ktorého pochádza.</p>"
                '<ul class="docs">'
                "<li>Ortopedická správa <span>8. 10. 2026</span></li>"
                "<li>Popis MR pravého kolena <span>8. 10. 2026</span></li>"
                "<li>Ortopedická správa <span>11. 11. 2026</span></li>"
                "<li>Ortopedický záznam, ľavé rameno <span>19. 2. 2025</span></li>"
                "<li>Ortopedický záznam, ľavé rameno <span>22. 4. 2025</span></li>"
                "</ul>"
                "<p>Dokumenty uvádzame len typom a dátumom. Odkazy na ne vidí iba ich majiteľ "
                "vo vlastnom rozhovore.</p>"
                "<p><b>Čo si Claude všimol:</b> správa z 11. 11. 2026 cituje MR kolena inak, než "
                "znie samotný popis rádiológa. Keďže každý fakt viedol k svojmu dokumentu, rozdiel "
                "bol viditeľný. Atlas vychádza z popisu rádiológa, upozorňuje na to a otázku "
                "necháva ortopédovi.</p>",
            ),
            (
                "Čo postavil Claude",
                "<p>Claude artefakt: interaktívny 3D atlas troch oblastí.</p>"
                "<ul>"
                "<li><b>Tri úrovne priblíženia:</b> postava, kĺb a kosti, svaly a šľachy.</li>"
                "<li><b>Nález oproti zdravému stavu:</b> prepínač „Petrov nález“ a „Zdravý "
                "stav“ a režim Porovnať, ktorý ich strieda.</li>"
                "<li><b>Ovládanie:</b> prehrať a pozastaviť, posúvať pohyb, otáčať a "
                "približovať.</li>"
                "<li><b>12 animovaných cvikov</b> s postupom krok za krokom v bežnej reči.</li>"
                "</ul>"
                "<p>Vzniklo to v jednom rozhovore, bez ručného písania kódu.</p>",
            ),
        ],
        "try": "Vyskúšať interaktívny atlas",
        "try_note": "",
        "limits_h": "Hranice",
        "limits": (
            "<p>Model je schematický a zostavený z textu lekárskych správ. Nezobrazuje skutočné "
            "snímky.</p>"
            "<p>Cviky sú bežné príklady na posúdenie fyzioterapeutom. Nie sú predpisom ani "
            "lekárskou radou. Ortopéd predpísal rehabilitáciu všeobecne, nie tieto konkrétne "
            "cviky.</p>"
            "<p>Atlas nenahrádza lekára ani fyzioterapeuta.</p>"
        ),
        "how_h": "Ako si spraviť to isté",
        "how": (
            "<ol>"
            "<li><b>Majte správy v Oncofiles.</b> Pripájanie vlastného Google Drive spúšťame "
            'v novembri 2026, <a href="/#waitlist">pridajte sa na zoznam</a>.</li>'
            '<li><b>Pripojte Oncofiles ku Claude</b> ako konektor (<a href="/#connect">'
            "postup</a>).</li>"
            "<li><b>Požiadajte vlastnými slovami:</b> „Prečítaj si moje ortopedické správy a "
            "postav z nich interaktívne vysvetlenie nálezu. Pri každom fakte uveď dokument a "
            "upozorni ma, ak si správy protirečia.“</li>"
            "<li><b>Výsledok vezmite k lekárovi alebo fyzioterapeutovi.</b> Rozhodujú oni.</li>"
            "</ol>"
            "<p>Server Oncofiles beží v EÚ (Slovensko) na našom vlastnom serveri. AI asistent je "
            "samostatná služba, ktorú si pripájate vy: to, na čo sa v nej pýtate, ide tomuto "
            "asistentovi.</p>"
        ),
        "back": "Späť na oncofiles.com",
        "disc": (
            "Oncofiles je informačný nástroj pre pacienta a opatrovateľa. Nenahrádza lekára, "
            "nediagnostikuje a nenavrhuje liečbu. Každý klinický záver si overte u svojho lekára."
        ),
        "legal": (
            "Claude je ochranná známka Anthropic. Oncofiles s touto spoločnosťou nie je spojený; "
            "funguje s jej aplikáciami cez otvorený protokol MCP."
        ),
    },
    "en": {
        "html_lang": "en",
        "og_locale": "en_US",
        "alt": "sk",
        "alt_label": "SK",
        "title": "Movement atlas: from medical reports to an interactive 3D explanation | "
        "Oncofiles",
        "description": (
            "Oncofiles is not only for oncology patients. A real case: Claude read four "
            "orthopaedic documents and one MRI description through Oncofiles and built an "
            "interactive 3D atlas of the "
            "lower back, knee and shoulder, with exercise suggestions for a physiotherapist."
        ),
        "video_name": "Movement atlas: from medical reports to an interactive 3D explanation",
        "video_label": "Video: the movement atlas in use",
        "h1": "From medical reports to an interactive 3D explanation",
        "lede": (
            "Oncofiles is not only for oncology patients. An otherwise healthy person can use it "
            "as the source of facts for an explanation of an everyday health problem, and for "
            "exercise suggestions to take to a physiotherapist."
        ),
        "real": (
            "<p><b>A real case, not demo data.</b> These are the own findings of Peter Fusek, "
            "the author of Oncofiles, published by his own choice. "
            'The invented patient in the <a href="/demo">demo</a> has nothing to do with it.</p>'
        ),
        "video_alt": (
            "The video (82 seconds, no sound) goes through three areas: the lower back, the right "
            "knee and the left shoulder. For each one it alternates Peter's finding with the "
            "healthy state, first at the level of the joint and bones, then muscles and tendons, "
            "and shows two animated exercises."
        ),
        "sections": [
            (
                "The problem",
                "<p>Three everyday complaints: the lower back (the L5/S1 segment), a clicking "
                "right knee and a left shoulder that hurts when the arm is raised. With them, "
                "four orthopaedic documents and one MRI description, written in medical "
                "language. From the text alone "
                "it is hard to picture what a finding means in motion, or what to ask a "
                "physiotherapist.</p>",
            ),
            (
                "What Oncofiles supplied",
                "<p>The Oncofiles profile is of the “general” type (not oncology) and holds 232 "
                "documents. Through the Oncofiles connector (MCP) Claude read four orthopaedic "
                "documents and one MRI description and took the facts from them: "
                "diagnoses, findings and recommendations, each linked to the "
                "document it comes from.</p>"
                '<ul class="docs">'
                "<li>Orthopaedic report <span>8 Oct 2026</span></li>"
                "<li>MRI description, right knee <span>8 Oct 2026</span></li>"
                "<li>Orthopaedic report <span>11 Nov 2026</span></li>"
                "<li>Orthopaedic record, left shoulder <span>19 Feb 2025</span></li>"
                "<li>Orthopaedic record, left shoulder <span>22 Apr 2025</span></li>"
                "</ul>"
                "<p>The documents are listed by type and date only. Links to them are visible "
                "only to their owner, in his own conversation.</p>"
                "<p><b>What Claude noticed:</b> the report of 11 Nov 2026 quotes the knee MRI "
                "differently from the radiologist's own description. Because every fact led back "
                "to its document, the mismatch was visible. The atlas follows the radiologist's "
                "description, says so, and leaves the question to the orthopaedist.</p>",
            ),
            (
                "What Claude built",
                "<p>A Claude artifact: an interactive 3D atlas of the three areas.</p>"
                "<ul>"
                "<li><b>Three zoom levels:</b> the figure, the joint and bones, muscles and "
                "tendons.</li>"
                "<li><b>The finding next to the healthy state:</b> a switch between “Peter's "
                "finding” and “Healthy state”, and a Compare mode that alternates them.</li>"
                "<li><b>Controls:</b> play and pause, scrub through the movement, rotate and "
                "zoom.</li>"
                "<li><b>12 animated exercises</b> with step-by-step text in plain language.</li>"
                "</ul>"
                "<p>It was made in one conversation, without writing code by hand.</p>",
            ),
        ],
        "try": "Try the interactive atlas",
        "try_note": "The atlas interface is in Slovak only.",
        "limits_h": "Limits",
        "limits": (
            "<p>The model is schematic and built from the text of the medical reports. It does "
            "not show real scans.</p>"
            "<p>The exercises are common examples for a physiotherapist to review. They are not a "
            "prescription and not medical advice. The orthopaedist prescribed rehabilitation in "
            "general, not these specific exercises.</p>"
            "<p>The atlas does not replace a doctor or a physiotherapist.</p>"
        ),
        "how_h": "How to do the same",
        "how": (
            "<ol>"
            "<li><b>Have your reports in Oncofiles.</b> Connecting your own Google Drive opens "
            'in November 2026, <a href="/?lang=en#waitlist">join the list</a>.</li>'
            '<li><b>Connect Oncofiles to Claude</b> as a connector (<a href="/?lang=en#connect">'
            "steps</a>).</li>"
            "<li><b>Ask in your own words:</b> “Read my orthopaedic reports and build an "
            "interactive explanation of the findings. Name the document behind every fact and "
            "tell me where the reports contradict each other.”</li>"
            "<li><b>Take the result to your doctor or physiotherapist.</b> They decide.</li>"
            "</ol>"
            "<p>The Oncofiles server runs in the EU (Slovakia) on our own hardware. The AI "
            "assistant is a separate service that you connect: what you ask about in it goes to "
            "that assistant.</p>"
        ),
        "back": "Back to oncofiles.com",
        "disc": (
            "Oncofiles is an information tool for the patient and caregiver. It does not replace "
            "a physician, does not diagnose, and does not propose treatment. Verify every "
            "clinical conclusion with your doctor."
        ),
        "legal": (
            "Claude is a trademark of Anthropic. Oncofiles is not affiliated with this company; "
            "it works with its apps through the open MCP protocol."
        ),
    },
}


def _video_json_ld(lang: str) -> str:
    c = _COPY[lang]
    data = {
        "@context": "https://schema.org",
        "@type": "VideoObject",
        "name": c["video_name"],
        "description": c["video_alt"],
        "thumbnailUrl": f"{SITE}{ATLAS_PATH}/poster-{lang}.jpg",
        "contentUrl": f"{SITE}{ATLAS_PATH}/video-{lang}.mp4",
        "uploadDate": PUBLISHED,
        "duration": f"PT{VIDEO_SECONDS}S",
        "inLanguage": lang,
        "width": VIDEO_SIDE_PX,
        "height": VIDEO_SIDE_PX,
        "isFamilyFriendly": True,
        "publisher": {"@type": "Organization", "name": "Oncofiles", "url": f"{SITE}/"},
    }
    # "</" cannot end the script element early
    return json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")


# The first play starts at data-start (the landing does the same, plus
# visibility-driven playback, in its own script). Nothing starts by itself
# here: people come to this page to read. Inline like the landing's script;
# the public-page CSP allows it (#534).
_START_SCRIPT = """<script>
(function () {
  var v = document.querySelector('video[data-start]');
  if (!v) return;
  var start = parseFloat(v.getAttribute('data-start')) || 0;
  var pending = true;
  function applyStart() {
    if (!pending || v.readyState < 1) return;
    pending = false;
    if (v.currentTime < start && start < v.duration) v.currentTime = start;
  }
  v.addEventListener('play', applyStart);
  v.addEventListener('loadedmetadata', function () { if (!v.paused) applyStart(); });
  // Scrubbing before the first play: the person chose the position.
  v.addEventListener('seeking', function () { if (v.paused) pending = false; });
})();
</script>"""


def use_case_page_html(lang: str) -> str:
    c = _COPY[lang]
    canonical = f"{SITE}{USE_CASE_PATHS[lang]}"
    alt_href = USE_CASE_PATHS[c["alt"]]
    home = "/" if lang == "sk" else "/?lang=en"
    video = f"{ATLAS_PATH}/video-{lang}.mp4"
    poster = f"{ATLAS_PATH}/poster-{lang}.jpg"  # intro card: og:image
    start_poster = f"{ATLAS_PATH}/poster-{lang}-start.jpg"  # the frame the video starts at
    sections = "\n".join(f"<h2>{h}</h2>\n{body}" for h, body in c["sections"])
    try_note = f"<small>{c['try_note']}</small>" if c["try_note"] else ""
    return f"""<!DOCTYPE html>
<html lang="{c["html_lang"]}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{c["title"]}</title>
<meta name="description" content="{c["description"]}">
<link rel="canonical" href="{canonical}">
<link rel="alternate" hreflang="sk" href="{SITE}{USE_CASE_PATHS["sk"]}">
<link rel="alternate" hreflang="en" href="{SITE}{USE_CASE_PATHS["en"]}">
<link rel="alternate" hreflang="x-default" href="{SITE}{USE_CASE_PATHS["en"]}">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta property="og:title" content="{c["title"]}">
<meta property="og:description" content="{c["description"]}">
<meta property="og:type" content="article">
<meta property="og:url" content="{canonical}">
<meta property="og:site_name" content="Oncofiles">
<meta property="og:locale" content="{c["og_locale"]}">
<meta property="og:image" content="{SITE}{poster}">
<meta property="og:image:type" content="image/jpeg">
<meta property="og:image:width" content="{VIDEO_SIDE_PX}">
<meta property="og:image:height" content="{VIDEO_SIDE_PX}">
<meta property="og:video" content="{SITE}{video}">
<meta property="og:video:type" content="video/mp4">
<meta property="og:video:width" content="{VIDEO_SIDE_PX}">
<meta property="og:video:height" content="{VIDEO_SIDE_PX}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{SITE}{poster}">
<meta name="theme-color" content="#f6f4ef" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#151918" media="(prefers-color-scheme: dark)">
<link rel="stylesheet" href="/fonts/fonts.css">
<script type="application/ld+json">
{_video_json_ld(lang)}
</script>
<style>{_CSS}</style>
</head>
<body>
<header class="site"><div class="wrap">
  <a class="logo" href="{home}" aria-label="Oncofiles">onco<span>files</span></a>
  <a class="lang" href="{alt_href}" hreflang="{c["alt"]}" lang="{c["alt"]}">{c["alt_label"]}</a>
</div></header>
<main class="wrap">
<h1>{c["h1"]}</h1>
<p class="lede">{c["lede"]}</p>
<div class="real">{c["real"]}</div>
<figure>
  <video controls muted playsinline preload="metadata" data-start="{VIDEO_START_SECONDS}"
    width="{VIDEO_SIDE_PX}" height="{VIDEO_SIDE_PX}" poster="{start_poster}" src="{video}"
    aria-label="{c["video_label"]}" aria-describedby="video-alt"></video>
  <figcaption id="video-alt">{c["video_alt"]}</figcaption>
</figure>
{sections}
<p class="try"><a class="btn" href="{ATLAS_PATH}">{c["try"]}</a>{try_note}</p>
<h2>{c["limits_h"]}</h2>
<div class="limits">{c["limits"]}</div>
<h2>{c["how_h"]}</h2>
{c["how"]}
</main>
<footer><div class="wrap">
<p>{c["disc"]}</p>
<p>{c["legal"]}</p>
<p><a href="{home}">&larr; {c["back"]}</a> &middot;
<a href="https://github.com/peter-fusek/oncofiles" rel="noreferrer noopener">GitHub</a></p>
</div></footer>
{_START_SCRIPT}
</body>
</html>"""
