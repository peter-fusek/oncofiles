# Movement-atlas videos (#830)

`render.py` re-renders the showcase videos from the hosted atlas, so the videos
always show what `/movement-atlas` draws. Tools run through `uv run --with`;
none of them is an app dependency.

| Output | Use |
| --- | --- |
| `oncofiles-atlas-{sk,en}.mp4` | full quality (about 27 MB), kept in the maintainer's private Drive, not in the repo |
| `oncofiles-atlas-{sk,en}-small.mp4` | under 3.8 MB; copied to `src/oncofiles/atlas/video-{sk,en}.mp4` |
| `poster-{sk,en}.jpg` | intro card (og:image, JSON-LD thumbnail) |
| `poster-{sk,en}-start.jpg` | the frame at 12 s, where the landing video starts (`VIDEO_START_SECONDS`) |

Cut (82 s, 24 fps, 1080x1080 H.264 yuv420p, no audio): intro card 3 s, then per
area (lower back, right knee, left shoulder) 9 s compare at joint level, 6 s
compare at muscle level, two exercises of 5 s each, outro card 4 s.

## Run

```bash
# 1. a local Oncofiles serving the atlas
DATABASE_PATH=/tmp/atlas-video.db MCP_HOST=127.0.0.1 MCP_PORT=8765 MCP_BEARER_TOKEN=test \
  MCP_TRANSPORT=streamable-http uv run oncofiles-mcp

# 2. render both cuts (about 1.5 minutes each on an Apple-silicon GPU)
uv run --no-project --with playwright --with imageio-ffmpeg \
  python -I scripts/atlas_video/render.py --lang sk --lang en --out /tmp/atlas-video

# 3. ship the small versions and the posters
for l in sk en; do
  cp /tmp/atlas-video/oncofiles-atlas-$l-small.mp4 src/oncofiles/atlas/video-$l.mp4
  cp /tmp/atlas-video/poster-$l.jpg /tmp/atlas-video/poster-$l-start.jpg src/oncofiles/atlas/
done
uv run pytest tests/test_atlas_showcase.py
```

Chrome is the installed Google Chrome (`channel="chrome"`), headless, with the
Metal ANGLE backend; ffmpeg comes from `imageio-ffmpeg` (pass `--ffmpeg` for
another binary).

## How it works

- The page is opened as `/movement-atlas?record=1`: `app.js` hides the page
  chrome, starts no animation loop of its own and exposes
  `window.__atlas.step(dt)`. `render.py` steps it by exactly 1/24 s per frame,
  so every run gives the same frames, and captures each one at 2160 px
  (downscaled by ffmpeg, which supersamples edges and text).
- `overlay.js` is injected into that one recording context (CSP bypass only
  there; the published page and its CSP are unchanged): titles, footer, cards
  in the landing look (navy, coral, turquoise, peach; Outfit and DM Sans from
  `/fonts/`) and the English text of the scene for the EN cut. An untranslated
  scene string stops the EN render.
- The copy may show a realistic look but never claim it: no "photorealistic",
  "diagnostic" or "clinically validated" (`tests/test_atlas_showcase.py`
  checks these files). The footer says the model is schematic.
