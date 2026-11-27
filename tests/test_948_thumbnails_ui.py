"""Document previews, the grid view and the quick look in the dashboard (#948).

Static locks on dashboard.html / dashboard.js / dashboard.css (the page is the
same for /dashboard and /demo) and on the demo's synthetic SVG previews.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from starlette.requests import Request

from oncofiles import demo_thumbnails
from oncofiles.server import api_demo_data

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
HTML = (SRC / "dashboard.html").read_text(encoding="utf-8")
JS = (SRC / "dashboard.js").read_text(encoding="utf-8")
CSS = (SRC / "dashboard.css").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = re.search(rf"\n    (?:async )?function {re.escape(name)}\(", JS)
    assert start, name
    end = JS.find("\n    }\n", start.start())
    assert end > 0, name
    return JS[start.start() : end]


async def _demo_dashboard() -> tuple[dict, str]:
    resp = await api_demo_data(
        Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    )
    return json.loads(resp.body)["dashboard"], resp.body.decode()


def _svg(uri: str) -> str:
    prefix = "data:image/svg+xml;charset=utf-8,"
    assert uri.startswith(prefix)
    return urllib.parse.unquote(uri[len(prefix) :])


# ── the list / grid switch ───────────────────────────────────────────────


def test_view_switch_is_a_segmented_control_next_to_the_search():
    toolbar = HTML[HTML.index('<div class="docs-toolbar">') : HTML.index('id="patient-export-btn"')]
    assert 'id="doc-search"' in toolbar
    assert '<div class="view-switch" role="group" aria-labelledby="view-switch-label">' in toolbar
    for view, pressed in (("list", "false"), ("grid", "true")):  # grid is the default
        button = re.search(rf'<button [^>]*data-action="setDocView\|{view}"[^>]*>', toolbar)
        assert button, view
        assert f'aria-pressed="{pressed}"' in button.group(0)
        # labels live in an inner span, so applyDashLang() never wipes the icon
        assert "data-sk=" not in button.group(0)
    assert toolbar.count('<span class="view-btn-label" data-sk=') == 2
    assert toolbar.count('<svg class="view-icon"') == 2  # flat inline icons, no emoji


def test_choice_is_remembered_and_works_without_storage():
    assert "var DOC_VIEW_KEY = 'oncofiles_doc_view';" in JS
    read = _function("readDocView")
    assert "try {" in read and "catch (e) { return 'grid'; }" in read
    # grid is the default; only a remembered "list" shows the table
    assert "=== 'list' ? 'list' : 'grid'" in read
    store = _function("setDocView")
    assert "try { localStorage.setItem(DOC_VIEW_KEY, docView); } catch (e)" in store
    controls = _function("updateDocViewControls")
    assert "btn.setAttribute('aria-pressed', pair[1] ? 'true' : 'false');" in controls
    assert "tableWrap.hidden = grid;" in controls and "gridWrap.hidden = !grid;" in controls


def test_grid_shares_filter_search_and_sort_with_the_table():
    view = _function("docListView")
    assert "docsData.filter(matchesFilter)" in view
    assert "sortCol" in view and "sortAsc" in view
    table = _function("renderTable")
    assert "var view = docListView();" in table
    assert "if (docView === 'grid') { renderGrid(view.sorted); return; }" in table
    # one sort for both: the header click and the grid's "sorted by" line
    assert "setSort(col, sortCol === col ? !sortAsc : defaultSortAsc(col));" in JS
    assert 'data-change="setGridSort|@value"' in HTML
    assert 'data-action="toggleSortDir"' in HTML
    assert "syncGridSort();" in _function("setSort")


def test_a_card_is_a_button_with_preview_name_category_date_and_status():
    card = _function("buildDocCard")
    assert "document.createElement('button')" in card
    assert "card.type = 'button';" in card
    for part in ("buildThumb(d)", "doc-card-name", "categoryBadge(d.category)", "fmtDate(d.date)"):
        assert part in card, part
    assert "stageChip(d, true)" in card  # status = shape + short label
    assert "openQuickLook(d.id, card)" in card
    assert ".doc-card-name { display: -webkit-box;" in CSS and "-webkit-line-clamp: 2;" in CSS
    phone_grid = "grid-template-columns: repeat(2, "  # two columns on a phone
    assert phone_grid + "minmax(0, 1fr));" in CSS


# ── previews: lazy, batched, one request at a time ───────────────────────


def test_previews_are_asked_lazily_in_batches_one_request_at_a_time():
    assert "var THUMB_BATCH = 24;" in JS
    assert "new IntersectionObserver(" in _function("observeThumb")
    pump = _function("pumpThumbs")
    assert "if (thumbBusy || thumbsOff) return;" in pump
    assert "thumbWanted.splice(0, THUMB_BATCH)" in pump
    assert "apiFetch('/api/doc-thumbnails?ids=' + ids.map(encodeURIComponent).join(',')" in pump
    assert "'&size=' + size" in pump
    # the large preview goes first and is asked for one id
    assert "size = 'l'; ids = [thumbLargeWanted];" in pump
    # answers that arrive after a patient switch are dropped
    assert "if (gen === thumbGen) {" in pump


def test_previews_are_kept_for_the_page_and_dropped_on_patient_switch():
    assert "var thumbCache = new Map();" in JS
    reset = _function("resetThumbs")
    assert "thumbGen += 1;" in reset and "thumbCache.clear();" in reset
    assert "resetThumbs();" in _function("resetDocView")
    assert "resetDocView();" in _function("switchPatient")
    assert "resetThumbs();" in _function("logout")
    # an unchanged list draws nothing again: no refetch, no flicker on the 60 s refresh
    assert "if (signature === docsSignature && docsDrawn()) return;" in _function("renderDocs")


def test_only_data_image_answers_become_pictures_else_the_file_type_mark():
    ok = _function("isPreview")
    assert "src.indexOf('data:image/') === 0" in ok
    thumb = _function("buildThumb")
    assert "typeMark(d)" in thumb
    img = _function("thumbImg")
    assert "img.addEventListener('error'" in img and "typeMark(d)" in img  # never a broken image
    mark = _function("typeMark")
    assert "createElementNS" in JS and "textContent = info.label" in mark
    for kind in ("pdf", "image", "doc"):
        assert f".type-{kind} .tm-badge" in CSS, kind


def test_the_preview_in_a_row_opens_the_quick_look_not_the_row_detail():
    row = _function("buildDocRow")
    assert "tdThumb.className = 'col-thumb';" in row
    assert "thumbBtn.type = 'button';" in row
    assert (
        "thumbBtn.addEventListener('click', function(e) { e.stopPropagation(); "
        "openQuickLook(d.id, thumbBtn); });" in row
    )
    # the row keeps its own click and its keyboard handler ignores the button
    assert "tr.addEventListener('click', function() { toggleDocDetail(d, tr); });" in row
    assert "if (e.target !== tr) return;" in row
    assert "td.setAttribute('colspan', '8');" in _function("tableMessage")
    assert "detailTd.setAttribute('colspan', '8');" in _function("openDocDetail")


def test_narrow_screens_hide_the_right_columns_after_the_new_first_one():
    # the new preview column moves category, institution and document date one place right
    assert "#doc-table th:nth-child(5), #doc-table .doc-row td:nth-child(5)," in CSS
    assert "#doc-table th:nth-child(6), #doc-table .doc-row td:nth-child(6)" in CSS
    assert "#doc-table th:nth-child(4), #doc-table .doc-row td:nth-child(4)" in CSS


# ── quick look ───────────────────────────────────────────────────────────


def test_quick_look_is_a_plain_dialog_with_close_previous_and_next():
    block = HTML[HTML.index('<div id="quicklook"') : HTML.index("<!-- Onboarding not open yet")]
    assert '<div id="quicklook" class="quicklook" hidden>' in block
    assert 'role="dialog" aria-modal="true" aria-labelledby="ql-title"' in block
    assert "<dialog" not in HTML
    assert '<div class="ql-backdrop" data-action="closeQuickLook"></div>' in block
    for action in ("quickLookStep|-1", "quickLookStep|1", "closeQuickLook"):
        assert f'data-action="{action}"' in block, action
    # icon buttons carry their names as translated screen-reader text
    assert block.count('<span class="sr-only" data-sk=') >= 3
    for name in ("openQuickLook", "closeQuickLook", "quickLookStep", "renderQuickLook"):
        assert re.search(rf"\n    function {name}\(", JS), name


def test_quick_look_keys_focus_and_scroll():
    escape = JS[JS.index("document.addEventListener('keydown', function(e) {") :]
    assert "if (quickLook.id !== null) { closeQuickLook(); return; }" in escape[:400]
    assert "quickLookStep(e.key === 'ArrowLeft' ? -1 : 1);" in JS
    assert "trapQuickLookFocus(e)" in JS
    close = _function("closeQuickLook")
    assert "target.focus();" in close and "opener.isConnected" in close
    assert "lockPageScroll(false);" in close
    assert "html.ql-lock, html.ql-lock body { overflow: hidden; }" in CSS
    step = _function("quickLookStep")
    assert "docListView().sorted" in step  # the list as it is filtered and sorted now


def test_quick_look_reuses_the_detail_without_frame_or_second_close_button():
    detail = _function("renderDocDetail")
    assert "function renderDocDetail(doc, data, detailTd, opts)" in detail
    assert "if (!overlay) panel.appendChild(closeBtn);" in detail
    assert "if (qlLink && opts.linkHost) opts.linkHost.appendChild(qlLink);" in detail
    look = _function("renderQuickLookDetail")
    assert "renderDocDetail(" in look and "overlay: true" in look
    # the Drive link only where the row has an https one (the demo has none)
    assert "if (!data.gdrive_url || !/^https:\\/\\//.test(data.gdrive_url)) return null;" in detail


def test_quick_look_survives_a_refresh_and_follows_the_language():
    assert "if (quickLook.id !== null) renderQuickLookNav();" in _function("renderTable")
    assert "renderQuickLook();" in _function("setDashLang")


# ── the demo twin and its synthetic pictures ─────────────────────────────


def test_demo_twin_answers_each_requested_id_or_null():
    route = _function("demoRoute")
    block = route[route.index("if (path === '/api/doc-thumbnails') {") :]
    assert "searchParams.get('ids')" in block
    assert "out[id] = all[id] || null;" in block
    assert "return { thumbnails: out };" in block


async def test_demo_previews_are_small_valid_svgs_with_invented_2027_content():
    data, body = await _demo_dashboard()
    thumbs = data["thumbnails"]
    total = 0
    for doc_id, entry in thumbs.items():
        assert set(entry) <= {"src", "src_en"}, doc_id
        for key, uri in entry.items():
            total += len(uri)
            assert len(uri) < 6000, (doc_id, key, len(uri))
            raw = uri.split(",", 1)[1]
            assert "#" not in raw and '"' not in raw and "\n" not in raw
            svg = _svg(uri)
            ET.fromstring(svg)  # well-formed XML (our own generated SVG, not outside input)
            assert "<script" not in svg and "href=" not in svg  # no script, no external file
            text = svg.replace("http://www.w3.org/2000/svg", "")  # the namespace is no date
            years = {int(y) for y in re.findall(r"(?<!\d)(?:19|20)\d\d(?!\d)", text)}
            assert years and min(years) >= 2027, (doc_id, key, years)
        if "src_en" in entry:
            assert entry["src_en"] != entry["src"]
    assert total < 100_000, total
    # only the invented patient
    assert "Jana Testová" in body and "Jane Doe" in body


async def test_demo_previews_draw_the_page_text_the_detail_shows():
    data, _ = await _demo_dashboard()
    lab = _svg(data["thumbnails"]["1"]["src"])
    for cell in ("WBC", "6,1", "Krvný obraz", "Nemocnica Príkladov"):
        assert cell in lab, cell
    lab_en = _svg(data["thumbnails"]["1"]["src_en"])
    assert "Example Hospital" in lab_en and "6.1" in lab_en
    assert "Rp." in _svg(data["thumbnails"]["9"]["src"])  # the prescription form
    note = _svg(data["thumbnails"]["12"]["src"])
    assert "cursive" in note and "ďalšia kontrola" in note  # the handwritten note


def test_flags_and_text_items_read_by_shape():
    assert demo_thumbnails._flag("119", "120–160") == "L"
    assert demo_thumbnails._flag("12,5", "4,0–10,0") == "H"
    assert demo_thumbnails._flag("2,7", "< 5,0") == ""
    items = demo_thumbnails.items_from_text(
        "Nemocnica Príkladov – Oddelenie\nPacientka: Jana Testová (vymyslená)\n\n"
        "Krvný obraz\nWBC    5,8    ×10⁹/L    4,0–10,0\n\nVymyslený výsledok pre ukážku."
    )
    assert [i[0] for i in items] == ["head", "p", "blank", "sub", "row", "blank", "note"]
