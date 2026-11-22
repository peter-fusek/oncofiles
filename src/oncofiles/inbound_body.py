"""The plain text of an inbound mail, for the quarantine card (#976).

A quarantined mail (``inbound_quarantine``) keeps its text so the caregiver
can read what the sender wrote before approving. This module is the ONE
place that turns a mail body into that text; it is stdlib only and never
renders anything:

* the ``text/plain`` part is preferred; when a mail has only HTML, the HTML
  is reduced to text (``html_to_text``: ``<script>`` / ``<style>`` / ``<head>``
  content dropped, every tag stripped, entities unescaped, whitespace
  collapsed). Nothing of the HTML survives: no tag, no attribute, no link
  target (an ``<a href>`` keeps only its visible text);
* control characters (except newline and tab) are removed, line ends are
  normalised and runs of blank lines collapsed;
* the result is capped at ``BODY_MAX_BYTES`` UTF-8 bytes, cut at a character
  boundary with ``TRUNCATED_MARKER`` appended. The input is cut at
  ``INPUT_MAX_CHARS`` before any parsing, so a huge body costs bounded work.

The text is never logged, never sent to a model, never indexed and never
stored in clear: ``inbound_quarantine`` stores it AES-GCM encrypted
(``body_enc``, associated data ``quarantine|<patient>|<item>|body``) and the
dashboard shows it with ``textContent`` only (links are not clickable).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

BODY_MAX_BYTES = 8 * 1024  # after reduction, UTF-8
INPUT_MAX_CHARS = 256 * 1024  # of the raw text / HTML read at all
TRUNCATED_MARKER = "\n[…]"

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏ -‮⁦-⁩﻿]")
_SPACES_RE = re.compile(r"[ \t\xa0]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

# tags whose content is never text
_SKIP_TAGS = frozenset({"script", "style", "head", "title", "template", "noscript", "svg", "math"})
# tags that end a line in the reduced text
_BLOCK_TAGS = frozenset(
    {
        "br",
        "p",
        "div",
        "li",
        "tr",
        "table",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "hr",
        "section",
        "article",
        "header",
        "footer",
    }
)


class _TextOnly(HTMLParser):
    """Collects the visible text of an HTML body; tags and attributes vanish."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):  # noqa: ARG002 - attributes are dropped
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):  # noqa: ARG002
        if tag.lower() in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """The visible text of ``html`` (stdlib parser, never rendered)."""
    parser = _TextOnly()
    try:
        parser.feed(html[:INPUT_MAX_CHARS])
        parser.close()
    except Exception:  # malformed markup: whatever was collected so far
        pass
    # a newline inside running HTML text is just a space; block tags made ours
    return "".join(
        p if p == "\n" else p.replace("\r", " ").replace("\n", " ") for p in parser.parts
    )


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def cap(text: str, max_bytes: int = BODY_MAX_BYTES) -> str:
    """``text`` within ``max_bytes`` UTF-8 bytes, cut at a character boundary
    (the marker included in the budget)."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    budget = max_bytes - len(TRUNCATED_MARKER.encode("utf-8"))
    cut = raw[: max(budget, 0)].decode("utf-8", errors="ignore").rstrip()
    return cut + TRUNCATED_MARKER


def reduce_body(text: str | None, html: str | None) -> str:
    """The mail's text: ``text`` when it has any, else ``html`` reduced to
    text; tidied and capped. ``""`` when there is none."""
    plain = text if isinstance(text, str) else ""
    plain = _tidy(plain[:INPUT_MAX_CHARS])
    if not plain and isinstance(html, str) and html.strip():
        plain = _tidy(html_to_text(html))
    return cap(plain) if plain else ""


def from_mime(msg) -> str:
    """The body text of a parsed ``email.message.EmailMessage`` (top level
    only, never inside an attached ``.eml``); ``""`` when unreadable."""
    text = html = None
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None and part.get_content_disposition() != "attachment":
            text = part.get_content()
        if not (text or "").strip():
            part = msg.get_body(preferencelist=("html",))
            if part is not None and part.get_content_disposition() != "attachment":
                html = part.get_content()
    except Exception:
        return ""
    return reduce_body(text, html)
