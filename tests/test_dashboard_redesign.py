"""Dashboard redesign (Nov 2026).

One status per document instead of seven dots, one event log instead of four
panels, admin tools in their own zone, and /demo as the very same page fed by
an invented patient. These are static locks on the three assets plus the two
page variants the server builds from dashboard.html.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from starlette.requests import Request

from oncofiles import server
from oncofiles.server import api_demo_data, demo_dashboard
from tests.helpers import PRIMARY_TEST_UUID, make_doc

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
HTML = (SRC / "dashboard.html").read_text(encoding="utf-8")
JS = (SRC / "dashboard.js").read_text(encoding="utf-8")
CSS = (SRC / "dashboard.css").read_text(encoding="utf-8")


def _req(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


def _function(name: str) -> str:
    """Source of one top-level function of dashboard.js."""
    start = re.search(rf"\n    (?:async )?function {re.escape(name)}\(", JS)
    assert start, name
    end = JS.find("\n    }\n", start.start())
    assert end > 0, name
    return JS[start.start() : end]


async def _demo_page() -> str:
    return (await demo_dashboard(_req("/demo"))).body.decode()


async def _demo_dashboard_data() -> dict:
    return json.loads((await api_demo_data(_req("/api/demo-data"))).body)["dashboard"]


# ── what the patient's page no longer shows ──────────────────────────────


def test_redundant_panels_stay_removed():
    for gone in (
        'id="funnel"',
        'id="remedies-panel"',
        'id="recon-panel"',
        'id="mcp-connection-bar"',
        'id="mcp-setup-section"',
        'id="manifest-section"',
        'id="sync-history-section"',
        'id="next-sync"',
        'id="card-uptime"',
        'id="filters"',
    ):
        assert gone not in HTML, gone
    for gone in ("renderRemedies", "renderReconciliation", "'/api/reconciliation'", "Next sync"):
        assert gone not in JS, gone
    for gone in (".funnel", ".remedy-", ".mcp-bar", "#next-sync"):
        assert gone not in CSS, gone


def test_requests_use_the_auth_header_helper():
    # two requests used an undefined variable, so the germline banner never
    # loaded and archiving a patient always failed
    assert "authToken" not in JS


def test_no_new_inner_html_sinks():
    # dynamic DOM is built with createElement + textContent (XSS guard); the only
    # innerHTML uses are the esc() safety net and the hard-coded i18n strings
    lines = [ln.strip() for ln in JS.splitlines() if "innerHTML" in ln]
    assert lines == [
        "return d.innerHTML;",
        "el.innerHTML = text;  // safe: only hardcoded i18n strings, no user input",
    ]


# ── the document list ────────────────────────────────────────────────────


def test_one_status_column_and_newest_upload_first():
    head = HTML[HTML.index("<thead>") : HTML.index("</thead>")]
    # #948: the first column is the preview tile, no label, no sort; then the chevron
    ths = re.findall(r"<th(?:\s[^>]*)?>", head)
    assert ths[:2] == ['<th class="col-thumb">', '<th class="col-toggle">']
    assert len(ths) == 8
    assert re.findall(r'data-sort="([a-z_]+)"', head) == [
        "filename",
        "category",
        "institution",
        "date",
        "uploaded_at",
        "stage_rank",
    ]
    assert '<th data-sort="uploaded_at" aria-sort="descending">' in head
    assert "let sortCol = 'uploaded_at';" in JS
    assert "let sortAsc = false;" in JS


def test_sort_arrow_survives_a_language_switch():
    # applyDashLang() replaces the text of every [data-sk][data-en] element:
    # a header cell must keep its label in an inner span, next to the arrow
    head = HTML[HTML.index("<thead>") : HTML.index("</thead>")]
    for th in re.findall(r"<th [^>]*>", head):
        assert "data-sk=" not in th, th


def test_whole_list_is_loaded_in_one_call():
    # the old page asked for 200 rows, so a patient with more lost the rest
    assert "apiFetch('/api/documents?filter=all&limit=2000')" in JS
    assert "limit=200'" not in JS


def test_a_row_expands_in_place_and_never_leaves_the_page():
    row = _function("buildDocRow")
    assert "createElement('a')" not in row and "drive.google.com" not in row
    assert "tr.addEventListener('click', function() { toggleDocDetail(d, tr); });" in row
    assert "tr.setAttribute('aria-expanded', 'false');" in row
    assert "row-chevron" in row
    # the Drive link lives inside the opened detail
    assert "gdriveLink.className = 'detail-drive-link';" in _function("renderDocDetail")
    assert ".doc-row.is-open .row-chevron" in CSS


def test_status_is_a_shape_and_a_label():
    for stage in ("ready", "waiting_text", "waiting_name", "waiting_filing", "attention"):
        assert f"{stage}: [" in JS, stage
    for reason in ("review_needed", "missing_in_drive", "outside_folder", "missing_date"):
        assert JS.count(f"{reason}: [") == 2, reason  # the label and the help text
    for shape in (".state-mark.ready", ".state-mark.progress", ".state-mark.attention"):
        assert shape in CSS, shape
    for card in ("all", "ready", "in_progress", "attention"):
        assert f'data-filter="{card}" data-action="setDocFilter|{card}"' in HTML, card


def test_every_action_in_the_page_has_a_function():
    names = set()
    for attr in ("data-action", "data-change", "data-input"):
        for spec in re.findall(rf'{attr}="([^"]+)"', HTML):
            names.update(part.split("|")[0].strip() for part in spec.split(";"))
    for name in sorted(names):
        assert re.search(
            rf"(function {re.escape(name)}\(|window\.{re.escape(name)} = function)", JS
        ), name


# ── administration ───────────────────────────────────────────────────────


def test_refresh_does_not_load_admin_data():
    refresh = _function("refresh")
    for path in (
        "/api/schedules",
        "/api/usage-analytics",
        "/api/prompt-log",
        "/api/cost-leaderboard",
        "/api/admin/",
    ):
        assert path not in refresh, path
    assert "updateAdminArea();" in refresh
    panel = _function("loadAdminPanel")
    assert "if (!isGlobalAdmin) return;" in panel
    for path in (
        "/api/schedules",
        "/api/usage-analytics",
        "/api/prompt-log",
        "/api/cost-leaderboard",
    ):
        assert path in panel, path


def test_admin_area_is_hidden_until_an_admin_signs_in():
    page = server._load_dashboard_html()
    assert '<section class="admin-area" id="admin-area" style="display:none"' in page
    assert "area.style.display = isGlobalAdmin ? 'block' : 'none';" in _function("updateAdminArea")
    for panel in ("curated", "reread", "schedules", "ai"):
        assert f'id="admin-body-{panel}" hidden' in page, panel


# ── two pages from one file ──────────────────────────────────────────────


def test_app_page_carries_nothing_of_the_demo():
    page = server._load_dashboard_html()
    for absent in (
        "data-demo",
        "demo-banner",
        "consent.js",
        "cookie-banner",
        "googletagmanager",
        "-only-->",
        "demo-head",
        "/demo/assistant",
    ):
        assert absent not in page, absent
    for present in ("accounts.google.com/gsi/client", 'id="auth-gate"', 'id="bug-fab"'):
        assert present in page, present
    assert page.count("<title") == 1


async def test_demo_page_is_the_dashboard_without_the_account_parts():
    page = await _demo_page()
    assert '<html lang="sk" data-demo="1">' in page
    for absent in (
        "accounts.google.com",
        'id="auth-gate"',
        'id="admin-area"',
        'id="bug-fab"',
        'data-action="logout"',
        'id="archive-patient-btn"',
        "-only-->",
        "demo-head",
    ):
        assert absent not in page, absent
    for present in (
        'id="demo-banner"',
        'href="/dashboard"',
        '<link rel="stylesheet" href="/dashboard.css">',
        '<script src="/dashboard.js" defer></script>',
        '<script src="/consent.js" defer></script>',
        'id="doc-table"',
        'id="activity-section"',
        'id="share-overlay"',
        'id="doc-grid"',
        'id="quicklook"',
    ):
        assert present in page, present
    assert page.count("<title") == 1
    # same document table, grid and quick look as the app, column for column
    app = server._load_dashboard_html()
    for block in (
        r'<table id="doc-table">.*?</table>',
        r'<div class="view-switch".*?</div>',
        r'<div class="doc-grid-wrap".*?</ul>',
        r'<div id="quicklook".*?<div class="ql-detail" id="ql-detail"></div>',
    ):
        pattern = re.compile(block, re.S)
        assert pattern.search(page).group(0) == pattern.search(app).group(0), block


def test_demo_mode_can_reach_nothing_but_the_demo_data():
    assert "var DEMO_ALLOWED_PATHS = ['/api/demo-data'];" in JS
    # the chokepoint is installed before any request can be made
    assert JS.index("window.fetch = function") < JS.index("fetch('")
    api = _function("apiFetch")
    assert api.index("if (DEMO) {") < api.index("await fetch(url")
    assert "var demoAnswer = demoRoute(url);" in api
    # every fetch the demo performs itself goes to the allowed path
    assert "var resp = await fetch('/api/demo-data');" in _function("ensureDemoData")


def test_demo_blocks_the_actions_that_write():
    block = JS[JS.index("var DEMO_BLOCKED_ACTIONS = {") : JS.index("window._dispatch")]
    blocked = set(re.findall(r"(\w+): 1", block))
    assert blocked >= {
        "showOnboarding",
        "showGdriveSetup",
        "triggerManualSync",
        "generateShareCode",
        "togglePatientArchive",
        "logout",
    }
    for name in blocked:
        assert re.search(rf"(function {name}\(|window\.{name} = function)", JS), name
    assert "if (DEMO && DEMO_BLOCKED_ACTIONS[parts[0]]) { demoNotice(); return; }" in JS


async def test_one_export_button_the_zip_in_app_and_demo():
    """#856: the JSON manifest download is gone; the ZIP export is the only
    export button, in the docs toolbar of both variants, blocked in /demo."""
    button = (
        '<button type="button" class="copy-btn" id="patient-export-btn" '
        'data-action="downloadPatientExport" data-sk="Stiahnuť všetky údaje (ZIP)" '
        'data-en="Download all data (ZIP)">'
    )
    app = server._load_dashboard_html()
    demo = await _demo_page()
    for page in (app, demo):
        for gone in ('id="export-btn"', "downloadManifest", "Download data (JSON)", "(JSON)<"):
            assert gone not in page, gone
        assert page.count(button) == 1
        assert page.count('data-action="downloadPatientExport"') == 1
        toolbar = page[page.index('<div class="docs-toolbar">') : page.index('id="upload-btn"')]
        assert button in toolbar
    # the dashboard no longer calls the JSON route, so it has no demo twin either
    assert "downloadManifest" not in JS and "/api/manifest" not in JS
    assert "downloadPatientExport: 1" in JS  # the demo only explains itself
    export = _function("downloadPatientExport")
    assert "if (DEMO || !currentPatientId) return;" in export
    assert "confirm(" not in export and "alert(" not in export


# ── the demo answers in the shapes of the real API ───────────────────────


async def test_demo_shows_every_state():
    data = await _demo_dashboard_data()
    rows = data["documents"]["documents"]
    assert {r["stage"] for r in rows} >= {"ready", "in_inbox", "waiting_name", "attention"}
    summary = data["documents"]["summary"]
    assert summary["ready"] + summary["in_progress"] + summary["attention"] == summary["total"]
    assert summary["total"] == len(rows)
    assert summary["inbox"] == sum(1 for r in rows if r.get("inbox")) >= 1
    assert all(r["gdrive_id"] is None for r in rows), "no Drive link in the demo"
    # inbox rows have no document behind them, so no detail (#921 follow-up)
    assert set(data["details"]) == {str(r["id"]) for r in rows if not r.get("inbox")}
    assert all(r["id"].startswith("inbox:") for r in rows if r.get("inbox"))
    # #948: a synthetic preview for every document (not for inbox rows), answered by the demo twin
    assert set(data["thumbnails"]) == {str(r["id"]) for r in rows if not r.get("inbox")}
    assert "if (path === '/api/doc-thumbnails') {" in _function("demoRoute")
    assert [p["patient_id"] for p in data["patients"]["own"]] == ["demo-patient"]
    assert data["patients"]["is_admin"] is False


async def test_demo_rows_have_the_keys_of_real_rows(db):
    from oncofiles.tools.hygiene import _build_document_matrix

    await db.insert_document(make_doc(), patient_id=PRIMARY_TEST_UUID)
    real = await _build_document_matrix(db, patient_id=PRIMARY_TEST_UUID, truncate_names=False)
    data = await _demo_dashboard_data()
    demo = data["documents"]

    def plain(keys) -> set[str]:
        return {k for k in keys if not k.endswith("_en")}

    first_doc = next(r for r in demo["documents"] if not r.get("inbox"))
    assert plain(first_doc) == set(real["documents"][0])
    assert set(demo["summary"]) == set(real["summary"])
    assert set(demo) == set(real)


async def test_demo_activity_has_the_keys_of_the_real_log():
    data = await _demo_dashboard_data()
    activity = data["activity"]
    assert set(activity) == {"events", "pending"}
    assert set(activity["pending"]) == {"inbox"}
    from oncofiles import dashboard_activity as da

    counts_by_kind = {
        da.KIND_CURATED_RUN: da.CURATED_COUNT_KEYS,
        da.KIND_TEXT_REREAD: da.REREAD_COUNT_KEYS,
    }
    kinds = set()
    for event in activity["events"]:
        assert set(event) == da.EVENT_KEYS
        assert set(event["counts"]) == counts_by_kind[event["kind"]]
        kinds.add(event["kind"])
    assert kinds == set(counts_by_kind)  # the demo shows both kinds
