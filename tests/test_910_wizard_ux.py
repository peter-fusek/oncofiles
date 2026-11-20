"""#910: small wizard / landing UX defects stay fixed (static checks of the shipped assets)."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"


def test_every_dashboard_placeholder_has_both_languages():
    html = (SRC / "dashboard.html").read_text(encoding="utf-8")
    missing = []
    for tag in re.findall(r"<(?:input|textarea)\b[^>]*>", html):
        ph = re.search(r'\splaceholder="([^"]*)"', tag)
        if not ph or ph.group(1).startswith("https://"):
            continue  # a URL example is the same in both languages
        if "data-sk-placeholder=" not in tag or "data-en-placeholder=" not in tag:
            missing.append(tag[:80])
    assert not missing, missing


def test_wizard_step_change_scrolls_the_panel_to_the_top():
    js = (SRC / "dashboard.js").read_text(encoding="utf-8")
    _, _, rest = js.partition("function wizardGoToStep(step) {")
    body, _, _ = rest.partition("\n    }\n")
    assert "wizardScrollTop();" in body
    assert "panel.scrollTop = 0" in js


def test_narrow_desktop_bar_gives_patient_controls_their_own_row():
    css = (SRC / "dashboard.css").read_text(encoding="utf-8")
    _, _, rest = css.partition("@media (min-width: 769px) and (max-width: 1100px) {")
    block, _, _ = rest.partition("\n    }\n")
    assert "flex-wrap: wrap" in block and "flex: 1 1 100%" in block


def test_landing_decorative_bands_cannot_be_scrolled_sideways():
    html = (SRC / "landing.html").read_text(encoding="utf-8")
    for selector in (".join-band", ".hero", "#photos"):
        rule = re.search(re.escape(selector) + r" \{[^}]*\}", html).group(0)
        assert "overflow: clip" in rule, selector
