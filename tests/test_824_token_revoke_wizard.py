"""#824: revoke patient tokens without plaintext, wizard type + birth date,
restored "+ New Patient" button, opt-in bearer token.

All values are invented. The birth date below is an obviously fake one.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oncofiles import patient_context, server
from oncofiles.database import Database
from oncofiles.models import Patient
from oncofiles.persistent_oauth import (
    PersistentOAuthProvider,
    _verified_caller_email,
    _verified_caller_is_admin,
)
from oncofiles.tool_annotations import DESTRUCTIVE_TOOLS, READ_ONLY_TOOLS
from oncofiles.tools import patient as patient_tools
from tests.helpers import ERIKA_UUID

SRC = Path(server.__file__).parent
OTHER_UUID = "33333333-3333-4333-8333-333333333333"
FAKE_DOB = "1901-02-03"


def _ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    return ctx


async def _slug(db: Database, pid: str = ERIKA_UUID) -> str:
    return (await db.get_patient(pid)).slug


async def _other_patient(db: Database) -> str:
    await db.insert_patient(
        Patient(patient_id=OTHER_UUID, slug="other-824", display_name="Other Person")
    )
    return OTHER_UUID


async def _token_rows(db: Database) -> list[dict]:
    async with db.db.execute(
        "SELECT id, patient_id, label, is_active, token_hash FROM patient_tokens ORDER BY id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ── DB helpers ───────────────────────────────────────────────────────────


async def test_revoke_patient_tokens_is_soft_and_scoped_to_one_patient(db: Database):
    other = await _other_patient(db)
    mine_a = await db.create_patient_token(ERIKA_UUID, label="desktop")
    mine_b = await db.create_patient_token(ERIKA_UUID, label="script")
    theirs = await db.create_patient_token(other, label="desktop")

    assert await db.revoke_patient_tokens(patient_id=ERIKA_UUID) == 2

    rows = await _token_rows(db)
    assert len(rows) == 3  # nothing deleted
    assert {r["patient_id"]: r["is_active"] for r in rows if r["patient_id"] == other} == {other: 1}
    assert all(r["is_active"] == 0 for r in rows if r["patient_id"] == ERIKA_UUID)
    assert await db.resolve_patient_from_token(mine_a) is None
    assert await db.resolve_patient_from_token(mine_b) is None
    assert await db.resolve_patient_from_token(theirs) == other
    # A second run finds nothing active.
    assert await db.revoke_patient_tokens(patient_id=ERIKA_UUID) == 0


async def test_revoke_patient_tokens_label_narrows(db: Database):
    keep = await db.create_patient_token(ERIKA_UUID, label="desktop")
    drop = await db.create_patient_token(ERIKA_UUID, label="script")

    assert await db.revoke_patient_tokens(patient_id=ERIKA_UUID, label="script") == 1
    assert await db.resolve_patient_from_token(keep) == ERIKA_UUID
    assert await db.resolve_patient_from_token(drop) is None
    assert await db.revoke_patient_tokens(patient_id=ERIKA_UUID, label="no-such-label") == 0


async def test_token_helpers_require_keyword_patient_id(db: Database):
    with pytest.raises(TypeError):
        await db.revoke_patient_tokens(ERIKA_UUID)  # type: ignore[misc]
    with pytest.raises(TypeError):
        await db.list_patient_tokens(ERIKA_UUID)  # type: ignore[misc]
    with pytest.raises(ValueError):
        await db.revoke_patient_tokens(patient_id="")
    with pytest.raises(ValueError):
        await db.list_patient_tokens(patient_id="")


async def test_list_patient_tokens_has_no_secret(db: Database):
    other = await _other_patient(db)
    token = await db.create_patient_token(ERIKA_UUID, label="desktop")
    await db.create_patient_token(ERIKA_UUID, label="script")
    await db.create_patient_token(other, label="theirs")
    await db.revoke_patient_tokens(patient_id=ERIKA_UUID, label="script")

    listed = await db.list_patient_tokens(patient_id=ERIKA_UUID)
    assert [(t["label"], t["is_active"]) for t in listed] == [("desktop", True), ("script", False)]
    assert all(set(t) == {"id", "label", "created_at", "is_active"} for t in listed)
    blob = json.dumps(listed)
    assert token not in blob and "onco_" not in blob
    for row in await _token_rows(db):
        assert row["token_hash"] not in blob and row["token_hash"][:12] not in blob
    assert [t["label"] for t in await db.list_patient_tokens(patient_id=other)] == ["theirs"]
    active = await db.list_patient_tokens(patient_id=ERIKA_UUID, active_only=True)
    assert [t["label"] for t in active] == ["desktop"]


async def test_revoked_token_no_longer_authenticates(db: Database):
    token = await db.create_patient_token(ERIKA_UUID, label="desktop")
    provider = PersistentOAuthProvider(db=db)
    verified = await provider.verify_token(token)
    assert verified is not None and verified.client_id == f"patient:{ERIKA_UUID}"

    assert await db.revoke_patient_tokens(patient_id=ERIKA_UUID, label="desktop") == 1
    assert await provider.verify_token(token) is None


# ── MCP tools ────────────────────────────────────────────────────────────


async def test_token_tools_reject_non_admin(db: Database):
    token = await db.create_patient_token(ERIKA_UUID, label="desktop")
    slug = await _slug(db)
    _verified_caller_is_admin.set(False)

    listed = json.loads(await patient_tools.list_patient_tokens(_ctx(db), slug))
    revoked = json.loads(await patient_tools.revoke_patient_tokens(_ctx(db), slug, dry_run=False))
    assert "admin scope" in listed["error"] and "admin scope" in revoked["error"]
    assert await db.resolve_patient_from_token(token) == ERIKA_UUID


@pytest.mark.usefixtures("admin_scope")
async def test_token_tools_require_patient_slug(db: Database):
    token = await db.create_patient_token(ERIKA_UUID, label="desktop")
    for slug in ("", "   ", None):
        listed = json.loads(await patient_tools.list_patient_tokens(_ctx(db), slug))
        revoked = json.loads(
            await patient_tools.revoke_patient_tokens(_ctx(db), slug, dry_run=False)
        )
        assert listed == {"error": "patient_slug is required"}
        assert revoked == {"error": "patient_slug is required"}
    assert await db.resolve_patient_from_token(token) == ERIKA_UUID
    for fn in (patient_tools.list_patient_tokens, patient_tools.revoke_patient_tokens):
        param = inspect.signature(fn).parameters["patient_slug"]
        assert param.default is inspect.Parameter.empty  # required in the tool schema


@pytest.mark.usefixtures("admin_scope")
async def test_list_patient_tokens_tool_output_has_no_secret(db: Database):
    token = await db.create_patient_token(ERIKA_UUID, label="desktop")
    raw = await patient_tools.list_patient_tokens(_ctx(db), await _slug(db))
    out = json.loads(raw)
    assert out["total"] == 1 and out["active"] == 1
    assert set(out["tokens"][0]) == {"id", "label", "created_at", "is_active"}
    assert token not in raw and "onco_" not in raw and "hash" not in raw


@pytest.mark.usefixtures("admin_scope")
async def test_revoke_tool_dry_run_is_default_and_writes_nothing(db: Database):
    token = await db.create_patient_token(ERIKA_UUID, label="desktop")
    out = json.loads(await patient_tools.revoke_patient_tokens(_ctx(db), await _slug(db)))
    assert out["dry_run"] is True and out["would_revoke"] == 1 and "revoked" not in out
    assert await db.resolve_patient_from_token(token) == ERIKA_UUID
    async with db.db.execute(
        "SELECT COUNT(*) FROM activity_log WHERE tool_name = 'revoke_patient_tokens'"
    ) as cur:
        assert (await cur.fetchone())[0] == 0


@pytest.mark.usefixtures("admin_scope")
async def test_revoke_tool_real_run_revokes_and_audits_once(db: Database):
    other = await _other_patient(db)
    keep = await db.create_patient_token(ERIKA_UUID, label="desktop")
    drop = await db.create_patient_token(ERIKA_UUID, label="script")
    theirs = await db.create_patient_token(other, label="script")
    slug = await _slug(db)
    email_tok = _verified_caller_email.set("admin.824@example.com")
    try:
        raw = await patient_tools.revoke_patient_tokens(
            _ctx(db), slug, label="script", dry_run=False
        )
    finally:
        _verified_caller_email.reset(email_tok)
    out = json.loads(raw)
    assert out["revoked"] == 1 and out["label"] == "script" and out["dry_run"] is False
    assert await db.resolve_patient_from_token(drop) is None
    assert await db.resolve_patient_from_token(keep) == ERIKA_UUID
    assert await db.resolve_patient_from_token(theirs) == other

    async with db.db.execute(
        "SELECT patient_id, input_summary, output_summary FROM activity_log "
        "WHERE tool_name = 'revoke_patient_tokens'"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    row = rows[0]
    assert row["patient_id"] == ERIKA_UUID
    inp, outp = json.loads(row["input_summary"]), json.loads(row["output_summary"])
    assert inp["label"] == "script" and inp["patient_slug"] == slug and inp["dry_run"] is False
    assert outp["revoked"] == 1 and len(outp["token_ids"]) == 1
    # No token material and no e-mail in the audit row or the tool output.
    audit_blob = row["input_summary"] + row["output_summary"] + raw
    assert "onco_" not in audit_blob and "admin.824@example.com" not in audit_blob
    for secret in (keep, drop, theirs):
        assert secret not in audit_blob
    for trow in await _token_rows(db):
        assert trow["token_hash"] not in audit_blob


def test_token_tools_are_classified():
    assert "list_patient_tokens" in READ_ONLY_TOOLS
    assert "revoke_patient_tokens" in DESTRUCTIVE_TOOLS
    assert "revoke_patient_tokens" not in READ_ONLY_TOOLS


def test_token_revocation_never_deletes():
    src = (SRC / "database" / "_patients.py").read_text(encoding="utf-8")
    assert "DELETE FROM patient_tokens" not in src
    body = inspect.getsource(Database.revoke_patient_tokens)
    assert "SET is_active = 0" in body and "DELETE" not in body


# ── POST /api/patients ───────────────────────────────────────────────────


def _request(db: Database, body: dict) -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.cookies = {}

    async def _json():
        return body

    request.json = _json
    request.app.state.fastmcp_server._lifespan_result = {"db": db}
    return request


@pytest.fixture
def open_api(monkeypatch):
    async def _no_notify(**_kwargs):
        return None

    monkeypatch.setattr(server, "_notify_admin_onboarding_event", _no_notify)
    monkeypatch.setattr(server, "_check_dashboard_auth", lambda req: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda label: None)
    monkeypatch.setattr(server, "_get_dashboard_email", lambda req: "caregiver.824@example.com")


async def _create(db: Database, **extra) -> tuple[int, dict]:
    body = {"patient_id": "wizard-824", "display_name": "Wizard Person", **extra}
    resp = await server.api_create_patient(_request(db, body))
    return resp.status_code, json.loads(resp.body)


async def _stored_context(db: Database, pid: str) -> dict | None:
    async with db.db.execute(
        "SELECT context_json FROM patient_context WHERE patient_id = ?", (pid,)
    ) as cur:
        row = await cur.fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.usefixtures("open_api")
async def test_create_patient_stores_type_and_birth_date_in_context(db: Database):
    status, data = await _create(db, patient_type="general", date_of_birth=FAKE_DOB)
    assert status == 201 and data["context_saved"] is True
    pid = data["patient"]["patient_id"]
    assert data["patient"]["patient_type"] == "general"
    assert data["patient"]["date_of_birth"] == FAKE_DOB

    stored = await _stored_context(db, pid)
    assert stored["patient_type"] == "general" and stored["date_of_birth"] == FAKE_DOB
    cached = patient_context.get_context(pid)
    assert cached["patient_type"] == "general" and cached["date_of_birth"] == FAKE_DOB
    # Another patient's context is untouched.
    assert patient_context.get_context(ERIKA_UUID).get("date_of_birth", "") != FAKE_DOB


@pytest.mark.usefixtures("open_api")
async def test_create_patient_defaults_to_oncology_without_birth_date(db: Database):
    status, data = await _create(db)
    assert status == 201
    stored = await _stored_context(db, data["patient"]["patient_id"])
    assert stored["patient_type"] == "oncology" and stored["date_of_birth"] == ""


@pytest.mark.usefixtures("open_api")
@pytest.mark.parametrize(
    "extra",
    [
        {"patient_type": "pediatric"},
        {"patient_type": "Oncology"},
        {"patient_type": ["general"]},
        {"date_of_birth": "1901-02-30"},  # not a real day
        {"date_of_birth": "03.02.1901"},
        {"date_of_birth": "19010203"},
        {"date_of_birth": 19010203},
        {"date_of_birth": "1899-12-31"},
        {"date_of_birth": (date.today() + timedelta(days=1)).isoformat()},
    ],
)
async def test_create_patient_rejects_bad_type_or_birth_date(db: Database, extra):
    status, data = await _create(db, **extra)
    assert status == 400 and "error" in data
    # Nothing was written: no patient row, no context row.
    assert await db.get_patient_by_slug("wizard-824") is None
    async with db.db.execute("SELECT COUNT(*) FROM patient_context") as cur:
        assert (await cur.fetchone())[0] == 0


@pytest.mark.usefixtures("open_api")
async def test_create_patient_mints_no_token(db: Database):
    status, data = await _create(db, patient_type="general")
    assert status == 201
    assert "bearer_token" not in data and "onco_" not in json.dumps(data)
    assert await _token_rows(db) == []
    assert "create_patient_token" not in inspect.getsource(server.api_create_patient)


@pytest.mark.usefixtures("open_api")
async def test_token_is_minted_only_on_request_and_share_link_needs_none(db, monkeypatch):
    _, data = await _create(db)
    pid = data["patient"]["patient_id"]

    async def _allow(request, db_inst, patient_id):
        return None

    monkeypatch.setattr(server, "_require_body_patient_access", _allow)

    # A share code works for a patient that never had a token.
    resp = await server.api_create_share_link(_request(db, {"patient_id": pid}))
    assert resp.status_code == 200 and json.loads(resp.body)["code"]
    server._share_codes.clear()
    assert [r["label"] for r in await _token_rows(db)] == ["share-link"]

    # The wizard's "Generate token" call.
    resp = await server.api_create_patient_token(
        _request(db, {"patient_id": pid, "label": "wizard"})
    )
    assert resp.status_code == 201
    token = json.loads(resp.body)["bearer_token"]
    assert await db.resolve_patient_from_token(token) == pid


# ── Dashboard assets (static) ────────────────────────────────────────────


def _html() -> str:
    return (SRC / "dashboard.html").read_text(encoding="utf-8")


def _js() -> str:
    return (SRC / "dashboard.js").read_text(encoding="utf-8")


def _wizard_step(html: str, number: int) -> str:
    """The markup of one wizard step (up to the start of the next one)."""
    start = html.index(f'id="wiz-step-{number}"')
    return html[start : html.index(f'id="wiz-step-{number + 1}"')]


def _js_function(name: str) -> str:
    js = _js()
    start = js.index(f"function {name}(")
    nxt = re.search(r"\n    (async )?function \w+\(", js[start + 1 :])
    return js[start : start + 1 + nxt.start()] if nxt else js[start:]


def test_wizard_step1_has_type_and_birth_date_fields():
    html = _html()
    step1 = _wizard_step(html, 1)
    select = step1[step1.index('<select id="wiz-type"') :]
    select = select[: select.index("</select>")]
    assert re.search(
        r'<option value="oncology" selected[^>]*data-sk="[^"]+" data-en="[^"]+"', select
    )
    assert re.search(r'<option value="general"[^>]*data-sk="[^"]+" data-en="[^"]+"', select)
    assert '<input type="date" id="wiz-dob"' in step1
    for field in ("wiz-type", "wiz-dob"):
        assert re.search(rf'<label for="{field}" data-sk="[^"]+" data-en="[^"]+"', step1)


def test_wizard_posts_type_and_birth_date():
    submit = _js_function("wizardStep1Submit")
    assert "patient_type: document.getElementById('wiz-type').value" in submit
    assert "date_of_birth: document.getElementById('wiz-dob').value || null" in submit
    assert "bearer_token" not in submit  # creation returns no token any more
    reset = _js_function("showOnboarding")
    assert "getElementById('wiz-type').value = 'oncology'" in reset
    assert "getElementById('wiz-dob').value = ''" in reset


def test_add_patient_button_is_restored_outside_admin_mode():
    body = _js_function("updateAdminMode")
    assert "addPatientBtn.style.display = isAdminMode ? 'none' : ''" in body
    assert "addPatientBtn && isAdminMode" not in body


def test_wizard_token_is_opt_in():
    html = _html()
    step2 = _wizard_step(html, 2)
    assert re.search(
        r'<button[^>]*id="wiz-token-gen"[^>]*data-action="wizardGenerateToken"'
        r'[^>]*data-sk="[^"]+" data-en="Generate token"',
        step2,
    )
    assert '<div class="wizard-token-box" id="wiz-token-display" style="display:none">' in step2

    js = _js()
    gen = _js_function("wizardGenerateToken")
    assert "'/api/patient-tokens'" in gen and "patient_id: wizardPatientUuid" in gen
    assert ".textContent = wizardBearerToken" in gen and "innerHTML" not in gen
    # Only an explicit click reaches the endpoint: the wizard's generate
    # button and the share modal's regenerate button.
    assert js.count("'/api/patient-tokens'") == 2
    assert "wizardGenerateToken()" not in js.replace("function wizardGenerateToken()", "")


def test_wizard_warns_when_context_was_not_saved():
    step2 = _wizard_step(_html(), 2)
    tag = re.search(r'<p id="wiz-context-warning"[^>]*>', step2).group(0)
    assert "display:none" in tag and 'data-sk="' in tag and 'data-en="' in tag
    assert "could not be saved" in tag and "nepodarilo uložiť" in tag
    assert not re.search(r"\son\w+=", tag)  # no inline handler

    setter = _js_function("setWizardContextWarning")
    assert "innerHTML" not in setter and "el.textContent = el.getAttribute(" in setter
    assert "el.style.display = visible ? 'block' : 'none'" in setter
    assert "setWizardContextWarning(data.context_saved === false)" in _js_function(
        "wizardStep1Submit"
    )
    # A new wizard run and the Drive-setup entry both start with it hidden.
    assert "setWizardContextWarning(false)" in _js_function("showOnboarding")
    assert "setWizardContextWarning(false)" in _js_function("showGdriveSetup")


@pytest.mark.usefixtures("open_api")
async def test_create_patient_reports_context_not_saved(db: Database, monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("synthetic context failure")

    monkeypatch.setattr(patient_context, "save_to_db", _boom)
    status, data = await _create(db, patient_type="general", date_of_birth=FAKE_DOB)
    assert status == 201 and data["context_saved"] is False
    assert await db.get_patient_by_slug("wizard-824") is not None


@pytest.mark.usefixtures("open_api")
@pytest.mark.parametrize("endpoint", ["api_create_share_link", "api_create_patient_token"])
async def test_slug_in_body_yields_token_bound_to_uuid(db: Database, monkeypatch, endpoint):
    _, data = await _create(db)
    pid = data["patient"]["patient_id"]
    seen: list[str] = []

    async def _allow(request, db_inst, patient_id):
        seen.append(patient_id)
        return None

    monkeypatch.setattr(server, "_require_body_patient_access", _allow)
    resp = await getattr(server, endpoint)(_request(db, {"patient_id": "wizard-824"}))
    out = json.loads(resp.body)
    if endpoint == "api_create_share_link":
        assert resp.status_code == 200
        token = server._share_codes[out["code"]]["bearer_token"]
        server._share_codes.clear()
    else:
        assert resp.status_code == 201 and out["patient_id"] == pid
        token = out["bearer_token"]
    assert seen == [pid]  # the ACL gate sees the UUID, not the slug
    assert [r["patient_id"] for r in await _token_rows(db)] == [pid]
    assert await db.resolve_patient_from_token(token) == pid


@pytest.mark.usefixtures("open_api")
@pytest.mark.parametrize("endpoint", ["api_create_share_link", "api_create_patient_token"])
async def test_unknown_patient_in_body_mints_nothing(db: Database, monkeypatch, endpoint):
    async def _allow(request, db_inst, patient_id):
        return None

    monkeypatch.setattr(server, "_require_body_patient_access", _allow)
    resp = await getattr(server, endpoint)(_request(db, {"patient_id": "no-such-patient"}))
    assert resp.status_code == 404
    assert await _token_rows(db) == []
