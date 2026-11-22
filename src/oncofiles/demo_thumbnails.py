"""Synthetic first-page previews for the dashboard demo (#948).

``/demo`` shows the document list with a small preview of every document and a
large one in the quick look. The real route (``/api/doc-thumbnails``) returns
JPEG data URIs of the patient's own Drive files; the demo cannot have those, so
this module DRAWS the invented documents of the invented patient as small SVG
pictures: a lab sheet with its result table, a CT report with a key image, a
prescription form, hospital letters with a stamp and a signature, a scanned
page, a phone photo of a lab printout and a handwritten note photographed on a
desk.

Everything is made up: the patient, the institution, every value and every
date (2027 or later), per the synthetic-fixtures rule. The text on a page is
the text the demo's document detail shows for it, so the picture and the
"text read" next to it agree. Pure functions of constants: no files, no fonts
(system font families only, an SVG inside ``<img>`` cannot load web fonts), no
network.

Each picture is returned as ``data:image/svg+xml`` (UTF-8, only ``%`` and
``#`` escaped, attributes in single quotes so JSON adds no escapes), a few kB
per page; one picture serves both sizes, since an SVG scales.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

# A4 portrait, two units per millimetre; photos are 3:4 like a phone camera.
PAGE = (420, 594)
PHOTO = (420, 560)

_INK = "#1f2937"
_NAVY = "#000059"
_STYLE = (
    "<style>"
    f"text{{font-family:Helvetica,Arial,sans-serif;fill:{_INK}}}"
    f".h{{font-size:15px;font-weight:700;fill:{_NAVY}}}"
    ".d{font-size:9px;fill:#5b6b82}"
    f".t{{font-size:12.5px;font-weight:700;letter-spacing:.8px;fill:{_NAVY}}}"
    f".s{{font-size:10.5px;font-weight:700;fill:{_NAVY}}}"
    ".b{font-size:9.5px}.p{font-size:8.5px}.k{font-weight:700}"
    ".m{font-size:7.5px;fill:#64748b}.i{font-size:8.5px;font-style:italic;fill:#64748b}"
    ".f{font-weight:700;fill:#c81e1e}"
    ".w{font-family:'Bradley Hand','Segoe Print','Noteworthy','Comic Sans MS',cursive;"
    "font-size:20px;fill:#1d2a8a}"
    ".r{fill:#e2e8f0}.n{fill:#000059}.c{fill:#ff4646}"
    "</style>"
)

_CELL_SPLIT = re.compile(r"\s{2,}")
_DIGIT = re.compile(r"\d")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def data_uri(svg: str) -> str:
    """``data:`` URI of an SVG; only the two characters a URL reads specially are escaped."""
    return "data:image/svg+xml;charset=utf-8," + svg.replace("%", "%25").replace("#", "%23")


def _t(x: float, y: float, text: str, cls: str = "b", extra: str = "") -> str:
    return f"<text x='{x:g}' y='{y:g}' class='{cls}'{extra}>{escape(text)}</text>"


def _svg(size: tuple[int, int], body: str, defs: str = "") -> str:
    w, h = size
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {w} {h}' width='{w}' height='{h}'>"
        f"{_STYLE}{defs}{body}</svg>"
    )


# ── reading the demo's page text into drawable items ───────────────────────


def items_from_lines(lines: list[dict], en: bool) -> list[tuple]:
    """Items of a showcase page: its lines carry ``kind`` (head, sub, th, note) and ``cells``."""
    out: list[tuple] = []
    for line in lines:
        cells = line.get("cells_en" if en else "cells") or line.get("cells")
        text = (line.get("text_en") if en else None) or line.get("text") or ""
        kind = line.get("kind", "")
        if cells:
            out.append(("th" if kind == "th" else "row", [str(c) for c in cells]))
        elif kind == "head":
            out.append(("head", text))
        elif kind == "sub":
            out.append(("sub", text))
        elif kind == "note":
            out.append(("note", text))
        elif not text:
            out.append(("blank",))
        else:
            out.append(("p", text))
    return out


def items_from_text(text: str) -> list[tuple]:
    """Items of a page given as plain text (the demo-only documents): read by shape."""
    raw = text.split("\n")
    out: list[tuple] = []
    for n, line in enumerate(raw):
        stripped = line.strip()
        cells = _CELL_SPLIT.split(stripped)
        nxt = raw[n + 1].strip() if n + 1 < len(raw) else ""
        if n == 0:
            out.append(("head", stripped))
        elif not stripped:
            out.append(("blank",))
        elif len(cells) >= 3:
            out.append(("row" if _DIGIT.search(cells[1]) else "th", cells))
        elif stripped.lower().startswith(("vymyslen", "invented")):
            out.append(("note", stripped))
        elif (
            ":" not in stripped
            and not stripped.endswith(".")
            and len(stripped) < 40
            and len(_CELL_SPLIT.split(nxt)) >= 3
        ):
            out.append(("sub", stripped))
        else:
            out.append(("p", stripped))
    return out


def _split_head(text: str) -> tuple[str, str]:
    hospital, _, dept = text.partition(" – ")
    return hospital.strip(), dept.strip()


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def _flag(value: str, ref: str) -> str:
    """``L`` / ``H`` when the printed value lies outside the printed range, else empty."""
    try:
        v = float(value.replace(",", "."))
    except ValueError:
        return ""
    bounds = [float(x.replace(",", ".")) for x in _NUM.findall(ref)]
    if ref.strip().startswith("<") and bounds:
        return "H" if v >= bounds[0] else ""
    if len(bounds) >= 2:
        return "L" if v < bounds[0] else "H" if v > bounds[1] else ""
    return ""


# ── the printed page ───────────────────────────────────────────────────────


def _logo(x: float, y: float, s: float = 1.0) -> str:
    """The invented hospital's mark: a navy tile with a white cross and a coral dot."""
    return (
        f"<g transform='translate({x:g} {y:g}) scale({s:g})'>"
        "<rect width='30' height='30' rx='7' class='n'/>"
        "<rect x='12' y='6' width='6' height='18' fill='white'/>"
        "<rect x='6' y='12' width='18' height='6' fill='white'/>"
        "<circle cx='25' cy='5' r='3.2' class='c'/></g>"
    )


def _signature(x: float, y: float, color: str = _NAVY) -> str:
    return (
        f"<path d='M{x:g} {y:g}c8-14 14-16 12-4s-6 10 3-2 13-14 12-1 4 6 14-6 9-3 8 4 "
        f"10-1 20-5' fill='none' stroke='{color}' stroke-width='1.4' stroke-linecap='round'/>"
    )


def _stamp(x: float, y: float, hospital: str) -> str:
    """A round ink stamp, slightly turned, with the hospital's name in two lines."""
    words = hospital.upper().split()
    top, bottom = (" ".join(words[:1]), " ".join(words[1:])) if len(words) > 1 else (hospital, "")
    ink = "#3538cd"
    return (
        f"<g transform='rotate(-12 {x:g} {y:g})' opacity='.62' fill='none' stroke='{ink}'>"
        f"<circle cx='{x:g}' cy='{y:g}' r='31' stroke-width='2'/>"
        f"<circle cx='{x:g}' cy='{y:g}' r='25' stroke-width='.8'/>"
        f"<text x='{x:g}' y='{y - 2:g}' text-anchor='middle' class='p k' stroke='none' "
        f"style='fill:{ink};font-size:7px'>{escape(top)}</text>"
        f"<text x='{x:g}' y='{y + 8:g}' text-anchor='middle' class='p k' stroke='none' "
        f"style='fill:{ink};font-size:7px'>{escape(bottom)}</text></g>"
    )


def _ct_slice(x: float, y: float) -> str:
    """A stylised axial CT slice (key image): body outline, spine, organs in greys."""
    return (
        f"<g transform='translate({x:g} {y:g})'>"
        "<rect width='96' height='96' rx='4' fill='#0f172a'/>"
        "<ellipse cx='48' cy='50' rx='40' ry='31' fill='#475569'/>"
        "<ellipse cx='48' cy='50' rx='36' ry='27' fill='#94a3b8'/>"
        "<ellipse cx='33' cy='44' rx='15' ry='12' fill='#cbd5e1'/>"
        "<ellipse cx='64' cy='46' rx='10' ry='8' fill='#64748b'/>"
        "<circle cx='48' cy='68' r='6' fill='#f8fafc'/>"
        "<circle cx='48' cy='57' r='3.5' fill='#e2e8f0'/></g>"
    )


def _patient_box(lines: list[str], y: float) -> tuple[str, float]:
    """Patient and dates in a grey box, label in bold, two columns."""
    if not lines:
        return "", y
    half = (len(lines) + 1) // 2
    rows = max(half, len(lines) - half)
    h = 14 + rows * 13
    parts = [f"<rect x='28' y='{y:g}' width='364' height='{h:g}' rx='4' fill='#f1f5f9'/>"]
    for i, line in enumerate(lines):
        col, row = divmod(i, half)
        label, sep, value = line.partition(":")
        tx, ty = 38 + col * 182, y + 17 + row * 13
        if sep:
            parts.append(
                f"<text x='{tx}' y='{ty:g}' class='p'><tspan class='k'>{escape(label)}:</tspan>"
                f"{escape(value)}</text>"
            )
        else:
            parts.append(_t(tx, ty, line, "p"))
    return "".join(parts), y + h + 18


_COLS = (34, 205, 222, 300)  # label, value (right edge), unit, reference range


def _body(items: list[tuple], y: float, width: int = 66) -> tuple[str, float]:
    parts: list[str] = []
    for item in items:
        kind = item[0]
        if kind == "blank":
            y += 7
        elif kind == "sub":
            parts.append(_t(28, y + 4, item[1], "s"))
            y += 18
        elif kind == "th":
            cells = item[1]
            parts.append(f"<rect x='28' y='{y - 11:g}' width='364' height='16' class='r'/>")
            for (x, anchor), cell in zip(
                ((_COLS[0], ""), (_COLS[1], " text-anchor='end'"), (_COLS[2], ""), (_COLS[3], "")),
                cells,
                strict=False,
            ):
                parts.append(_t(x, y, cell, "p k", anchor))
            y += 17
        elif kind == "row":
            cells = item[1] + [""] * (4 - len(item[1]))
            flag = _flag(cells[1], cells[3])
            parts.append(_t(_COLS[0], y, cells[0], "b k"))
            parts.append(_t(_COLS[1], y, cells[1], "b k" if flag else "b", " text-anchor='end'"))
            if flag:
                parts.append(_t(_COLS[1] + 4, y, flag, "p f"))
            parts.append(_t(_COLS[2], y, cells[2]))
            parts.append(_t(_COLS[3], y, cells[3], "p"))
            parts.append(f"<rect x='28' y='{y + 5:g}' width='364' height='.7' class='r'/>")
            y += 17
        elif kind == "note":
            parts.append(_t(28, y, item[1], "i"))
            y += 14
        else:  # paragraph
            for line in _wrap(item[1], width):
                parts.append(_t(28, y, line))
                y += 13.5
    return "".join(parts), y


def _split_items(items: list[tuple], title: str) -> tuple[str, str, str, list[str], list[tuple]]:
    """(hospital, department, title, patient lines, body items) of a page."""
    hospital, dept = _split_head(items[0][1]) if items and items[0][0] == "head" else ("", "")
    rest = items[1:]
    if rest and rest[0][0] == "p" and ":" not in rest[0][1]:
        title, rest = rest[0][1], rest[1:]
    patient: list[str] = []
    while rest and rest[0][0] == "p" and ":" in rest[0][1]:
        patient.append(rest[0][1])
        rest = rest[1:]
    while rest and rest[0][0] == "blank":
        rest = rest[1:]
    return hospital, dept, title, patient, rest


def _footer(hospital: str, en: bool) -> str:
    note = (
        f"{hospital} · Example Street 1 · invented document"
        if en
        else f"{hospital} · Ukážková 1 · vymyslený dokument"
    )
    return (
        "<rect x='28' y='566' width='364' height='.8' class='r'/>"
        + _t(28, 578, note, "m")
        + _t(392, 578, "1 / 1", "m", " text-anchor='end'")
    )


def printed_page(items: list[tuple], title: str, category: str, en: bool) -> str:
    """One printed hospital page: header with logo, title, patient box, body, closing."""
    hospital, dept, title, patient, body_items = _split_items(items, title)
    parts = [
        "<rect width='420' height='594' fill='white'/>",
        _logo(28, 22),
        _t(68, 35, hospital, "h"),
        _t(68, 48, dept, "d"),
        "<rect x='28' y='62' width='364' height='1.5' class='n'/>",
        _t(28, 86, title.upper(), "t"),
    ]
    box, y = _patient_box(patient, 98)
    parts.append(box)
    width = 66
    mid = " text-anchor='middle'"
    if category == "imaging":
        parts.append(_ct_slice(296, y - 6))
        parts.append(_t(344, y + 102, "Key image" if en else "Kľúčový obraz", "m", mid))
        width = 46
    if category == "prescription":
        parts.append(
            f"<rect x='28' y='{y - 6:g}' width='364' height='150' rx='3' fill='none' "
            "stroke='#94a3b8'/>"
            f"<text x='40' y='{y + 26:g}' style='font:italic 700 26px Georgia,serif;"
            f"fill:{_NAVY}'>Rp.</text>"
        )
        y += 50
    body, y = _body(body_items, y + 4, width)
    parts.append(body)
    if category == "prescription":
        parts.append("<rect x='28' y='510' width='150' height='30' fill='url(#bc)'/>")
    if category == "labs":
        parts.append(_signature(300, max(y + 10, 470)))
    else:
        sy = max(y + 40, 470)
        parts.append(_stamp(318, sy, hospital))
        parts.append(_signature(258, sy + 6))
        parts.append(f"<rect x='250' y='{sy + 20:g}' width='120' height='.8' fill='#94a3b8'/>")
        parts.append(_t(310, sy + 31, "signature, stamp" if en else "podpis, pečiatka", "m", mid))
    parts.append(_footer(hospital, en))
    defs = ""
    if category == "prescription":
        defs = (
            "<defs><pattern id='bc' width='9' height='30' patternUnits='userSpaceOnUse'>"
            "<rect width='2' height='30' fill='#111'/>"
            "<rect x='3.5' width='1' height='30' fill='#111'/>"
            "<rect x='6' width='1.6' height='30' fill='#111'/></pattern></defs>"
        )
    return _svg(PAGE, "".join(parts), defs)


def scanned_page(items: list[tuple], title: str, category: str, en: bool) -> str:
    """The printed page as a flatbed scan: grey bed, page slightly turned, warm paper."""
    inner = printed_page(items, title, category, en)
    inner = inner[inner.index("</style>") + len("</style>") : -len("</svg>")]
    paper = "<rect width='420' height='594' fill='%s'/>"
    inner = inner.replace(paper % "white", paper % "#fbfaf6", 1)
    body = (
        "<rect width='420' height='594' fill='#d7dbe1'/>"
        "<g transform='translate(12 10) rotate(.9) scale(.95)'>"
        "<rect x='3' y='4' width='420' height='594' fill='#b8bec8'/>"
        f"{inner}</g>"
        "<rect width='420' height='14' fill='#3d4452' opacity='.28'/>"
    )
    return _svg(PAGE, body)


# ── photographed documents ─────────────────────────────────────────────────


def _desk(top: str, bottom: str) -> str:
    return (
        "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        f"<stop offset='0' stop-color='{top}'/>"
        f"<stop offset='1' stop-color='{bottom}'/></linearGradient>"
        "<radialGradient id='v' cx='.42' cy='.35' r='.8'>"
        "<stop offset='.55' stop-color='#000' stop-opacity='0'/>"
        "<stop offset='1' stop-color='#000' stop-opacity='.35'/></radialGradient>"
        "<filter id='s' x='-20%' y='-20%' width='140%' height='140%'>"
        "<feGaussianBlur stdDeviation='7'/></filter>"
        "<pattern id='l' width='260' height='30' patternUnits='userSpaceOnUse'>"
        "<rect y='29' width='260' height='1' fill='#c3d3ec'/></pattern></defs>"
    )


def photo_of_printout(
    rows: list[tuple[str, str, str, str]], header: dict[str, str], en: bool
) -> str:
    """A phone photo of a lab printout on a dark desk: turned, shadowed, lit from the top left."""
    hospital, dept = _split_head(header["head"])
    lines = [
        _logo(26, 18, 0.8),
        _t(56, 30, hospital, "h", " style='font-size:13px'"),
        _t(56, 41, dept, "d"),
        "<rect x='26' y='52' width='268' height='1.3' class='n'/>",
        _t(26, 72, header["title"].upper(), "t", " style='font-size:11px'"),
        _t(26, 90, header["patient"], "p"),
        _t(26, 103, header["sampled"], "p"),
    ]
    y = 128
    lines.append(f"<rect x='22' y='{y - 11}' width='276' height='16' class='r'/>")
    for x, cell, anchor in zip(
        (26, 170, 182, 234), header["th"], ("", " text-anchor='end'", "", ""), strict=False
    ):
        lines.append(_t(x, y, cell, "p k", anchor))
    y += 18
    for code, value, unit, ref in rows:
        flag = _flag(value, ref)
        lines += [
            _t(26, y, code, "b k"),
            _t(170, y, value, "b k" if flag else "b", " text-anchor='end'"),
            _t(182, y, unit, "p"),
            _t(234, y, ref, "p"),
            f"<rect x='22' y='{y + 5}' width='276' height='.7' class='r'/>",
        ]
        y += 18
    lines.append(_t(26, y + 8, header["note"], "i"))
    lines.append(_signature(200, y + 40))
    body = (
        "<rect width='420' height='560' fill='url(#g)'/>"
        "<g transform='translate(62 58) rotate(-4.5) skewX(-2.5)'>"
        "<rect x='10' y='16' width='320' height='430' fill='#000' opacity='.55' filter='url(#s)'/>"
        "<rect width='320' height='430' fill='#fdfdfb'/>" + "".join(lines) + "</g>"
        "<ellipse cx='140' cy='110' rx='190' ry='130' fill='#fff' opacity='.10'/>"
        "<rect width='420' height='560' fill='url(#v)'/>"
    )
    return _svg(PHOTO, body, _desk("#5b6474", "#262b36"))


def photo_of_note(head: str, written: list[str], en: bool) -> str:
    """A handwritten note on a hospital notepad, photographed on a grey desk, a pen beside it."""
    hospital, dept = _split_head(head)
    ink: list[str] = []
    y, x, drift = 142, 44, (0, 5, -2, 3, 1, 4)
    for line in written:
        if line == "~":  # an illegible word: what the reading marked with [?]
            ink.append(
                f"<path d='M44 {y - 7}c5-12 9 4 13-6s7 8 11-2 5 6 10-3 6 5 9 0' fill='none' "
                "stroke='#1d2a8a' stroke-width='2' stroke-linecap='round'/>"
            )
            x = 98  # the rest of the line follows the scribble
            continue
        ink.append(_t(x + drift[len(ink) % len(drift)], y, line, "w"))
        x, y = 44, y + 30
    ink.append(_signature(140, y + 4, "#1d2a8a"))
    paper = (
        "<g transform='translate(78 52) rotate(4)'>"
        "<rect x='8' y='14' width='264' height='392' fill='#000' opacity='.45' filter='url(#s)'/>"
        "<rect width='264' height='392' fill='#fffdf5'/>"
        "<rect y='86' width='264' height='300' fill='url(#l)'/>"
        "<rect x='34' y='70' width='1' height='322' fill='#f2b8b8'/>"
        + _logo(18, 16, 0.7)
        + _t(44, 27, hospital, "p k", f" style='fill:{_NAVY}'")
        + _t(44, 38, dept, "m")
        + "<rect x='18' y='50' width='228' height='1' class='n'/>"
        + "".join(ink)
        + "</g>"
    )
    pen = (
        "<g transform='rotate(-28 330 470)'><rect x='250' y='462' width='150' height='13' rx='6.5' "
        "fill='#000059'/><rect x='372' y='462' width='28' height='13' rx='6.5' fill='#ff4646'/>"
        "<path d='M250 462l-16 6.5 16 6.5z' fill='#cbd5e1'/></g>"
    )
    body = (
        "<rect width='420' height='560' fill='url(#g)'/>"
        + paper
        + pen
        + "<ellipse cx='170' cy='120' rx='170' ry='110' fill='#fff' opacity='.12'/>"
        "<rect width='420' height='560' fill='url(#v)'/>"
    )
    return _svg(PHOTO, body, _desk("#b3bac6", "#7c8595"))


# ── the demo patient's pages ───────────────────────────────────────────────

# The phone photo of a lab printout (demo document 11): the printout is drawn
# from these constants, which match the page text the demo detail shows
# (invented values, a 2027 sample date).
_PRINTOUT = {
    False: {
        "head": "Nemocnica Príkladov – Oddelenie klinickej biochémie",
        "title": "Výsledky vyšetrenia",
        "patient": "Pacientka: Jana Testová (vymyslená)",
        "sampled": "Odber: 13.04.2027 07:20",
        "th": ["Vyšetrenie", "Výsledok", "Jednotky", "Ref. rozsah"],
        "note": "Vymyslený výsledok pre ukážku.",
    },
    True: {
        "head": "Example Hospital – Clinical Biochemistry",
        "title": "Test results",
        "patient": "Patient: Jane Doe (invented)",
        "sampled": "Sampled: 13 Apr 2027 07:20",
        "th": ["Test", "Result", "Units", "Ref. range"],
        "note": "Invented result for the demo.",
    },
}
_PRINTOUT_ROWS = {
    False: [
        ("WBC", "6,4", "×10⁹/L", "4,0–10,0"),
        ("HGB", "127", "g/L", "120–160"),
        ("PLT", "241", "×10⁹/L", "150–400"),
        ("CRP", "3,8", "mg/L", "< 5,0"),
    ],
}
_PRINTOUT_ROWS[True] = [
    (c, v.replace(",", "."), u, r.replace(",", ".")) for c, v, u, r in _PRINTOUT_ROWS[False]
]

# poznamka_z_kontroly.jpg: the handwriting of the note whose reading is in the demo
# ("~" is the word the reading could not make out, shown there as [?]).
_NOTE = {
    False: (
        "Nemocnica Príkladov – Onkologická ambulancia",
        ["9. 4. 2027", "Kontrola", "~", "ďalšia kontrola", "o 3 týždne"],
    ),
    True: (
        "Example Hospital – Oncology Clinic",
        ["9 Apr 2027", "Visit", "~", "next visit", "in 3 weeks"],
    ),
}


def picture(doc: dict, en: bool) -> str:
    """The SVG of one demo document in one language.

    ``doc``: ``style`` (printed | scan | printout_photo | note_photo),
    ``category``, ``title`` / ``title_en`` and either ``lines`` (showcase
    documents, with kinds and cells) or ``text`` / ``text_en`` (plain page text).
    """
    style = doc.get("style", "printed")
    if style == "printout_photo":
        return photo_of_printout(_PRINTOUT_ROWS[en], _PRINTOUT[en], en)
    if style == "note_photo":
        head, written = _NOTE[en]
        return photo_of_note(head, written, en)
    if doc.get("lines"):
        items = items_from_lines(doc["lines"], en)
    else:
        items = items_from_text(doc.get("text_en" if en else "text") or "")
    title = doc.get("title_en" if en else "title") or ""
    draw = scanned_page if style == "scan" else printed_page
    return draw(items, title, doc.get("category", "other"), en)


def build(docs: list[dict]) -> dict[str, dict]:
    """``{id: {"src": ..., "src_en": ...}}`` for the demo twin of ``/api/doc-thumbnails``.

    ``src_en`` (the English page, read through the dashboard's ``loc()``) is
    left out when it is the same picture.
    """
    out: dict[str, dict] = {}
    for doc in docs:
        sk = data_uri(picture(doc, en=False))
        en = data_uri(picture(doc, en=True))
        out[str(doc["id"])] = {"src": sk} if en == sk else {"src": sk, "src_en": en}
    return out
