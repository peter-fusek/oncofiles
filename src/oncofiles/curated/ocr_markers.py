"""Markers the local OCR writes into a transcription, and what readers do with them (#609).

The vision model invents digits on chart axes and on text that is turned
sideways or upside down. The OCR prompt (``extract.OCR_SYSTEM_PROMPT``)
therefore asks for two markers:

* ``[chart: …]`` — everything that belongs to a chart or graph (title, axis
  labels, axis tick labels), never a value read off a plotted curve. A chart
  block is **never data**: ``strip_charts`` replaces each block with
  ``[chart]`` before any fact reader sees the text (the lab rule parsers in
  ``facts_rules.extract_lab_facts`` and every LLM extractor through
  ``llm_input.load_for_llm``, which also feeds the verifiers). An unclosed
  block runs to the end of the page (fails closed).
* ``[rotated region]`` — a part of the page printed sideways or upside down
  relative to the rest; the model does not read it. The page goes to review.

Pure functions, no model and no I/O.
"""

from __future__ import annotations

import re

CHART_OPEN = re.compile(r"\[\s*chart\s*:", re.I)
CHART_PLACEHOLDER = "[chart]"
ROTATED_REGION = "[rotated region]"
_ROTATED_REGION_RE = re.compile(r"\[\s*rotated region\s*\]", re.I)


def strip_charts(text: str | None) -> str:
    """``text`` with every ``[chart: …]`` block replaced by ``[chart]``.

    Brackets inside a block (``[?]``) are nested, so the block ends at its own
    closing bracket. A block without one runs to the end of the text: when in
    doubt, chart content is never read as data."""
    text = text or ""
    out: list[str] = []
    i = 0
    while True:
        m = CHART_OPEN.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i : m.start()])
        out.append(CHART_PLACEHOLDER)
        depth, end = 0, None
        for j in range(m.start(), len(text)):
            if text[j] == "[":
                depth += 1
            elif text[j] == "]":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end is None:  # unclosed: the rest of the page is chart content
            break
        i = end
    return "".join(out)


def strip_charts_pages(pages):
    """``strip_charts`` on every page of a list of texts or of (number, text) pairs."""
    out = []
    for p in pages or []:
        if isinstance(p, tuple):
            out.append((p[0], strip_charts(p[1])))
        else:
            out.append(strip_charts(p))
    return out


def has_chart(text: str | None) -> bool:
    return bool(CHART_OPEN.search(text or ""))


def has_rotated_region(text: str | None) -> bool:
    return bool(_ROTATED_REGION_RE.search(text or ""))
