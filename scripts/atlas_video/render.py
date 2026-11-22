# ruff: noqa: E501  (inline HTML/JS snippets for the page are kept on one line each)
"""Render the movement-atlas showcase videos from the hosted atlas (#830).

Drives ``/movement-atlas?record=1`` in headless Chrome, steps the animation with
a fixed timestep (``window.__atlas.step``), captures every frame and pipes it to
ffmpeg. Same cut as the first version: intro card, then per area 9 s compare at
joint level, 6 s compare at muscle level, two exercises of 5 s each, outro card.
82 s at 24 fps, 1080x1080 H.264 yuv420p, no audio; plus a small version under
3.8 MB and the two posters (intro card, and the frame at 12 s where the landing
video starts).

Tools only, never app dependencies (README next to this file)::

    uv run --no-project --with playwright --with imageio-ffmpeg \\
      python -I scripts/atlas_video/render.py --lang en --out <dir>

The page is served by a local Oncofiles (``MCP_TRANSPORT=streamable-http``).
The overlay (titles, cards, English scene text) is injected from overlay.js with
CSP bypass in this recording context only; the page itself is unchanged.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
FPS = 24
INTRO, OUTRO = 3.0, 4.0
JOINT, MUSCLE, EXERCISE = 9.0, 6.0, 5.0
SIDE = 1080
SCALE = 2  # rendered at 2160 px and downscaled: supersampled edges and text
SMALL_BYTES = 3_800_000

COPY = {
    "sk": {
        "intro": (
            "Oncofiles + Claude",
            "Z lekárskych správ interaktívne 3D vysvetlenie",
            "Chrbát · pravé koleno · ľavé rameno",
            "Môj vlastný nález, nie ukážkové dáta",
        ),
        "outro": (
            "MCP backend pre zdravotnú dokumentáciu",
            "Prevádzkované v EÚ · self-deployed na Macu mini",
            "Schematický model zo správ, nie lekárske odporúčanie. Cviky určuje fyzioterapeut.",
        ),
        "kick": ("Nález oproti zdravému stavu", "Svaly a šľachy", "Návrh cviku pre fyzioterapeuta"),
        "areas": {
            "spine": ("Chrbát · L5/S1", "Zaniknutý medzistavcový priestor L5/S1"),
            "knee": ("Pravé koleno", "Preskakovanie vedľa jabĺčka"),
            "shoulder": ("Ľavé rameno", "Poškodený lem kĺbovej jamky"),
        },
        "muscle_sub": "Dynamické stabilizátory kĺbu",
        "foot": "Dáta: Oncofiles (MCP) · schematický 3D model zo správ, nie snímky",
        "ex": {},  # Slovak names and goals come from the page itself
    },
    "en": {
        "intro": (
            "Oncofiles + Claude",
            "From medical reports to an interactive 3D explanation",
            "Lower back · right knee · left shoulder",
            "My own findings, not demo data",
        ),
        "outro": (
            "MCP backend for medical documentation",
            "Runs in the EU · self-deployed on a Mac mini",
            "Schematic model built from reports, not medical advice. "
            "Exercises are for a physiotherapist to decide.",
        ),
        "kick": (
            "Finding vs. healthy state",
            "Muscles and tendons",
            "Exercise suggestion for the physiotherapist",
        ),
        "areas": {
            "spine": ("Lower back · L5/S1", "Collapsed L5/S1 disc space"),
            "knee": ("Right knee", "Clicking next to the kneecap"),
            "shoulder": ("Left shoulder", "Lesion of the socket rim"),
        },
        "muscle_sub": "Dynamic stabilisers of the joint",
        "foot": "Data: Oncofiles (MCP) · schematic 3D model built from the reports, not from scans",
        "ex": {
            "Vták a pes (bird-dog)": ("Bird-dog", "Deep back and core muscles, L5/S1 stability"),
            "Mŕtvy chrobák (dead bug)": ("Dead bug", "Core control while the limbs move"),
            "Mini-drep s kontrolou osi kolena": (
                "Mini squat with knee alignment",
                "Thighs and glutes, knee tracking over the foot",
            ),
            "Dvíhanie vystretej nohy v ľahu": (
                "Straight leg raise",
                "Quadriceps without loading the joint",
            ),
            "Vonkajšia rotácia s gumou": (
                "External rotation with a band",
                "Rotator cuff, centring the head in the socket",
            ),
            "Upažovanie v rovine lopatky": (
                "Scaption raise",
                "Shoulder blade and arm working together, up to shoulder height",
            ),
        },
    },
}
# (region, exercise name) in the order of the first cut
AREAS = [
    ("spine", ("Vták a pes (bird-dog)", "Mŕtvy chrobák (dead bug)")),
    ("knee", ("Mini-drep s kontrolou osi kolena", "Dvíhanie vystretej nohy v ľahu")),
    ("shoulder", ("Vonkajšia rotácia s gumou", "Upažovanie v rovine lopatky")),
]
# (distance factor, target lift as a share of the distance) for joint and muscle level
FRAME = {
    "spine": ((1.08, 0.05), (1.2, 0.06)),
    "knee": ((1.14, 0.03), (1.26, 0.07)),
    "shoulder": ((1.0, 0.03), (1.06, 0.04)),
}
# gentle camera swing per area segment (radians, around the area's home azimuth)
SWING = 0.22


def card_html(lang: str, kind: str) -> str:
    c = COPY[lang]
    circles = (
        '<i class="c" style="background:#33d6d6;width:620px;height:620px;right:-260px;top:-250px"></i>'
        '<i class="c" style="background:#ffd0a8;width:110px;height:110px;right:70px;bottom:250px"></i>'
        '<i class="c" style="background:#ff4646;width:300px;height:300px;left:-190px;bottom:-200px"></i>'
    )
    if kind == "intro":
        k, title, line1, line2 = c["intro"]
        body = f'<div class="rule"></div><p class="k">{k}</p><h1>{title}</h1><p>{line1}<br><b>{line2}</b></p>'
        return circles + f'<div class="body">{body}</div>'
    line1, line2, small = c["outro"]
    body = f'<div class="rule"></div><h1>oncofiles<span>.</span>com</h1><p>{line1}<br>{line2}</p>'
    return circles + f'<div class="body">{body}</div><small>{small}</small>'


def build_timeline() -> list[dict]:
    t = INTRO
    segs = []
    for region, exs in AREAS:
        segs.append({"kind": "joint", "region": region, "t0": t, "dur": JOINT})
        t += JOINT
        segs.append({"kind": "muscle", "region": region, "t0": t, "dur": MUSCLE})
        t += MUSCLE
        for name in exs:
            segs.append({"kind": "ex", "region": region, "name": name, "t0": t, "dur": EXERCISE})
            t += EXERCISE
    return segs


def wait(page, expr: str, timeout: float = 60) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if page.evaluate(f"() => {{ try {{ return !!({expr}) }} catch (e) {{ return false }} }}"):
            return
        time.sleep(0.2)
    raise TimeoutError(expr)


def render(lang: str, out: Path, base: str, ffmpeg: str) -> Path:
    c = COPY[lang]
    segs = build_timeline()
    total = segs[-1]["t0"] + segs[-1]["dur"] + OUTRO
    n_frames = round(total * FPS)
    full = out / f"oncofiles-atlas-{lang}.mp4"
    enc = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
         "-vf", f"scale={SIDE}:{SIDE}:flags=lanczos,format=yuv420p",
         "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-profile:v", "high",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
         "-map_metadata", "-1", "-metadata:s:v", "language=und", str(full)],
        stdin=subprocess.PIPE,
    )  # fmt: skip
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome", headless=True, args=["--use-angle=metal", "--ignore-gpu-blocklist"]
        )
        ctx = browser.new_context(
            viewport={"width": SIDE, "height": SIDE}, device_scale_factor=SCALE, bypass_csp=True
        )
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/movement-atlas?record=1")
        wait(page, "window.__atlas && window.__atlas.ready()")
        page.add_script_tag(content=(HERE / "overlay.js").read_text(encoding="utf-8"))
        page.evaluate("document.fonts.ready.then(() => true)")
        page.evaluate(f"window.__rec.lang({lang!r})")
        page.evaluate(f"window.__rec.foot({c['foot']!r})")
        # load every stage first, then warm the shaders of each state once
        for region, _ in AREAS:
            page.evaluate(f"window.__atlas.set({{region: {region!r}, level: 2, ex: null}})")
        wait(page, "window.__atlas.loaded().length === 4")
        ex_index = page.evaluate(
            "() => { const o = {}; for (const r in window.__atlas.EX) window.__atlas.EX[r].forEach((e, i) => o[e.name] = [r, i, e.goal]); return o; }"
        )
        cdp = ctx.new_cdp_session(page)

        def shot() -> bytes:
            data = cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 94})["data"]
            return base64.b64decode(data)

        def apply(seg: dict) -> dict:
            region = seg["region"]
            title, sub = c["areas"][region]
            if seg["kind"] in ("joint", "muscle"):
                level = 2 if seg["kind"] == "joint" else 3
                page.evaluate(
                    "s => window.__atlas.set(s)",
                    {"region": region, "level": level, "ex": None, "mode": "compare", "cmpT": 0,
                     "m": 1, "mT": 1, "tm": 0, "playing": True, "speed": 1, "autoRot": False},
                )  # fmt: skip
                kick = c["kick"][0 if level == 2 else 1]
                page.evaluate(
                    "a => window.__rec.head(...a)",
                    [kick, title, sub if level == 2 else c["muscle_sub"]],
                )
            else:
                r, i, goal = ex_index[seg["name"]]
                assert r == region, (r, region)
                page.evaluate(
                    "s => window.__atlas.set(s)",
                    {"exRef": [r, i], "tm": 0, "playing": True, "speed": 5.5 / EXERCISE,
                     "mode": "peter", "m": 1, "mT": 1, "autoRot": False},
                )  # fmt: skip
                name, goal_t = c["ex"].get(seg["name"], (seg["name"], goal))
                page.evaluate("a => window.__rec.head(...a)", [c["kick"][2], name, goal_t])
            if seg["kind"] in ("joint", "muscle"):
                # the square frame has the title on top and the badge below: pull back a bit
                f, dy = FRAME[region][0 if seg["kind"] == "joint" else 1]
                page.evaluate(
                    "a => { const g = window.__atlas.goal; g.dist *= a[0]; g.t.y += a[1] * g.dist; }",
                    [f, dy],
                )
            page.evaluate("window.__atlas.snap()")
            return page.evaluate("({az: window.__atlas.goal.az})")

        # warm-up pass: compile every material/state before the clock starts
        for seg in segs:
            apply(seg)
            for _ in range(3):
                page.evaluate(f"window.__atlas.step({1 / FPS})")
            shot()

        intro, outro = card_html(lang, "intro"), card_html(lang, "outro")
        cur = None
        home_az = 0.0
        t0 = time.time()
        for f in range(n_frames):
            t = f / FPS
            seg = next((s for s in segs if s["t0"] <= t < s["t0"] + s["dur"]), None)
            if seg is None:  # under the intro card: the first scene; under the outro: the last
                seg = segs[0] if t < segs[0]["t0"] else segs[-1]
            if seg is not cur:
                prev, cur = cur, seg
                home_az = apply(seg)["az"]
                # a short dip only where the scene changes (never inside one area's
                # joint -> muscle step, so the 12 s poster frame is the plain scene)
                cur["dip"] = prev is not None and not (
                    prev["kind"] == "joint" and seg["kind"] == "muscle"
                )
            local = t - cur["t0"]
            if cur["kind"] in ("joint", "muscle"):
                swing = -SWING + 2 * SWING * min(1.0, local / cur["dur"])
                page.evaluate(f"window.__atlas.goal.az = {home_az + swing}")
                if local < 1 / FPS:
                    page.evaluate("window.__atlas.snap()")
            page.evaluate(f"window.__atlas.step({1 / FPS})")
            # cards and dips
            if t < INTRO:
                op = 1.0 if t < INTRO - 0.4 else max(0.0, (INTRO - t) / 0.4)
                page.evaluate("a => window.__rec.card(a[0], a[1])", [intro if f == 0 else None, op])
            elif t >= total - OUTRO:
                op = min(1.0, (t - (total - OUTRO)) / 0.4)
                page.evaluate("a => window.__rec.card(a[0], a[1])", [outro, op])
            else:
                dip = cur.get("dip") and local < 0.25
                page.evaluate(
                    "a => window.__rec.card(a[0], a[1])", ["", (1 - local / 0.25) if dip else 0]
                )
            enc.stdin.write(shot())
            if f % 240 == 0:
                print(f"{lang} frame {f}/{n_frames} ({time.time() - t0:.0f} s)", flush=True)
        missing = page.evaluate("window.__rec.missing()")
        browser.close()
    enc.stdin.close()
    if enc.wait() != 0:
        raise SystemExit("ffmpeg failed")
    if errors:
        raise SystemExit(f"page errors: {errors}")
    if lang == "en" and missing:
        raise SystemExit(f"untranslated scene text: {missing}")
    return full


def small_and_posters(lang: str, full: Path, out: Path, ffmpeg: str) -> None:
    small = out / f"oncofiles-atlas-{lang}-small.mp4"
    duration = 82.0
    kbps = int(SMALL_BYTES * 8 / duration / 1000 * 0.9)
    common = ["-c:v", "libx264", "-preset", "slow", "-profile:v", "high", "-pix_fmt", "yuv420p",
              "-b:v", f"{kbps}k", "-an", "-map_metadata", "-1"]  # fmt: skip
    passlog = str(out / f"x264-{lang}")
    for n in (1, 2):
        target = ["-f", "mp4", "/dev/null"] if n == 1 else ["-movflags", "+faststart", str(small)]
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(full), *common,
             "-pass", str(n), "-passlogfile", passlog, *target],
            check=True,
        )  # fmt: skip
    for f in out.glob(f"x264-{lang}*"):
        f.unlink()
    if small.stat().st_size >= SMALL_BYTES:
        raise SystemExit(f"{small} is {small.stat().st_size} bytes")
    for name, at in ((f"poster-{lang}.jpg", 1.5), (f"poster-{lang}-start.jpg", 12.0)):
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at), "-i", str(full),
             "-frames:v", "1", "-q:v", "4", "-map_metadata", "-1", str(out / name)],
            check=True,
        )  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=("sk", "en"), action="append", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg binary (default: imageio-ffmpeg's)")
    a = ap.parse_args()
    ffmpeg = a.ffmpeg
    if not ffmpeg:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    a.out.mkdir(parents=True, exist_ok=True)
    for lang in a.lang:
        full = render(lang, a.out, a.base, ffmpeg)
        small_and_posters(lang, full, a.out, ffmpeg)
        print(lang, "done", file=sys.stderr)


if __name__ == "__main__":
    main()
