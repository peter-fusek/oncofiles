"""#857: consent recorded at patient creation, wizard explainer, legal text drift.

* ``POST /api/patients`` refuses a body without ``consent: true`` and the
  current ``CONSENT_VERSION`` (every caller, the operator included) and writes
  nothing;
* the stored consent row holds the version, the time and a keyed hash of the
  caller's e-mail, never the e-mail; patient + consent are one transaction;
* the wizard has a required, bilingual consent checkbox linking /privacy and
  /terms, sends the same version as the server, and explains Google's
  "unverified app" screen before the OAuth button;
* /privacy and /terms keep the claims the pipeline backs (string checks, so a
  later edit that drifts from the code is caught).

All values are synthetic.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oncofiles import patient_consent, server
from oncofiles.database import Database
from oncofiles.models import Patient
from oncofiles.patient_consent import CONSENT_VERSION
from oncofiles.secrets_keys import caller_email_hash

SRC = Path(server.__file__).parent
ROOT = SRC.parent.parent
HTML = (SRC / "dashboard.html").read_text()
JS = (SRC / "dashboard.js").read_text()
CALLER = "synthetic.carer.857@example.test"
SLUG = "synthetic-857"


@pytest.fixture(autouse=True)
def _fresh_legal():
    server._PRIVACY_HTML = None
    server._TERMS_HTML = None
    yield
    server._PRIVACY_HTML = None
    server._TERMS_HTML = None


@pytest.fixture
def api(monkeypatch):
    """Dashboard session caller (non-admin, onboarding open), no side effects."""

    async def _no_notify(**_kwargs):
        return None

    monkeypatch.delenv("AUDIT_EMAIL_HASH_SECRET", raising=False)
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "synthetic-bearer-857")
    monkeypatch.setattr("oncofiles.config.ONBOARDING_MODE", "open")
    monkeypatch.setattr(server, "_notify_admin_onboarding_event", _no_notify)
    monkeypatch.setattr(server, "_check_dashboard_auth", lambda req: None)
    # an HTTP session caller: no static bearer (tests run with MCP_TRANSPORT=stdio,
    # which _check_bearer would trust as the operator)
    monkeypatch.setattr(
        server, "_check_bearer", lambda req: server.JSONResponse({}, status_code=401)
    )
    monkeypatch.setattr(server, "_check_rate_limit", lambda label: None)
    monkeypatch.setattr(server, "_get_dashboard_email", lambda req: CALLER)


def _request(db: Database, body) -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.cookies = {}

    async def _json():
        return body

    request.json = _json
    request.app.state.fastmcp_server._lifespan_result = {"db": db}
    return request


def _body(**extra) -> dict:
    return {
        "patient_id": SLUG,
        "display_name": "Synthetic Person",
        "date_of_birth": "1901-02-03",
        "consent": True,
        "consent_version": CONSENT_VERSION,
        **extra,
    }


async def _post(db: Database, body) -> tuple[int, dict]:
    resp = await server.api_create_patient(_request(db, body))
    return resp.status_code, json.loads(resp.body)


async def _consent_rows(db: Database) -> list[dict]:
    async with db.db.execute("SELECT * FROM patient_consents") as cur:
        return [dict(r) for r in await cur.fetchall()]


# ── server refusal ───────────────────────────────────────────────────────


def test_consent_version_is_the_approved_one():
    assert CONSENT_VERSION == "2026-11-17"


@pytest.mark.usefixtures("api")
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"consent": None}, patient_consent.CONSENT_REQUIRED),
        ({"consent": False}, patient_consent.CONSENT_REQUIRED),
        ({"consent": "true"}, patient_consent.CONSENT_REQUIRED),
        ({"consent": 1}, patient_consent.CONSENT_REQUIRED),
        ({"consent_version": None}, patient_consent.CONSENT_VERSION_MISMATCH),
        ({"consent_version": "2026-01-01"}, patient_consent.CONSENT_VERSION_MISMATCH),
        ({"consent_version": CONSENT_VERSION + " "}, patient_consent.CONSENT_VERSION_MISMATCH),
    ],
)
async def test_create_patient_refused_without_valid_consent(db: Database, change, code):
    body = _body()
    for key, value in change.items():
        if value is None:
            body.pop(key)
        else:
            body[key] = value
    status, data = await _post(db, body)
    assert status == 400
    assert data["code"] == code and data["consent_version"] == CONSENT_VERSION
    assert data["error_sk"] and data["error_en"]
    # Nothing was written.
    assert await db.get_patient_by_slug(SLUG) is None
    assert await _consent_rows(db) == []


async def test_operator_must_pass_consent_too(db: Database, monkeypatch):
    """The static bearer (operator/admin) gets no exemption."""
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "synthetic-bearer-857")
    monkeypatch.setattr(server, "_check_dashboard_auth", lambda req: None)
    monkeypatch.setattr(server, "_check_bearer", lambda req: None)  # operator
    monkeypatch.setattr(server, "_check_rate_limit", lambda label: None)

    async def _no_notify(**_kwargs):
        return None

    monkeypatch.setattr(server, "_notify_admin_onboarding_event", _no_notify)
    body = _body()
    del body["consent"]
    status, data = await _post(db, body)
    assert status == 400 and data["code"] == patient_consent.CONSENT_REQUIRED
    assert await db.get_patient_by_slug(SLUG) is None

    status, data = await _post(db, _body())
    assert status == 201
    pid = data["patient"]["patient_id"]
    [row] = await db.get_patient_consents(patient_id=pid)
    assert row["is_admin"] == 1
    assert row["caller_email_hash"] is None  # the operator has no e-mail
    assert row["consent_version"] == CONSENT_VERSION


# ── stored record ────────────────────────────────────────────────────────


@pytest.mark.usefixtures("api")
async def test_consent_row_holds_version_time_and_keyed_hash_never_the_email(db: Database):
    status, data = await _post(db, _body(caregiver_email="other.carer.857@example.test"))
    assert status == 201
    pid = data["patient"]["patient_id"]

    rows = await _consent_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["patient_id"] == pid
    assert row["consent_version"] == CONSENT_VERSION
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", row["consented_at"])
    assert row["is_admin"] == 0
    # The hash of the signed-in CALLER (who gave consent), keyed, not a bare digest.
    assert row["caller_email_hash"] == caller_email_hash(CALLER)
    assert re.fullmatch(r"[0-9a-f]{64}", row["caller_email_hash"])
    import hashlib

    assert row["caller_email_hash"] != hashlib.sha256(CALLER.encode()).hexdigest()
    # No e-mail anywhere in the row.
    flat = json.dumps(row)
    assert "@" not in flat and "example.test" not in flat
    assert await db.get_patient_consents(patient_id=pid) == rows


async def test_patient_and_consent_are_one_transaction(db: Database):
    """A failing consent write leaves no patient behind."""
    await db.db.execute("DROP TABLE patient_consents")
    await db.db.commit()
    with pytest.raises(Exception):  # noqa: B017 - any DB error
        await db.insert_patient(
            Patient(patient_id=SLUG, display_name="Synthetic Person"),
            consent_version=CONSENT_VERSION,
            consent_caller_hash="0" * 64,
        )
    assert await db.get_patient_by_slug(SLUG) is None


async def test_consent_rows_are_patient_scoped(db: Database):
    a = await db.insert_patient(
        Patient(patient_id="synthetic-a", display_name="A"), consent_version=CONSENT_VERSION
    )
    b = await db.insert_patient(Patient(patient_id="synthetic-b", display_name="B"))
    assert len(await db.get_patient_consents(patient_id=a.patient_id)) == 1
    assert await db.get_patient_consents(patient_id=b.patient_id) == []  # no row without consent


def test_migration_is_additive_without_backfill():
    sql = (ROOT / "migrations" / "091_patient_consents.sql").read_text()
    code = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    assert "CREATE TABLE IF NOT EXISTS patient_consents" in code
    assert "patient_id TEXT NOT NULL REFERENCES patients(patient_id)" in code
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"):
        assert verb not in code.upper(), verb
    assert "email TEXT" not in code.replace("caller_email_hash TEXT", "")


# ── wizard ───────────────────────────────────────────────────────────────


def _step(n: int) -> str:
    start = HTML.index(f'<div id="wiz-step-{n}"')
    return HTML[start : HTML.index(f'<div id="wiz-step-{n + 1}"', start)]


def test_wizard_has_a_required_bilingual_consent_checkbox():
    step1 = _step(1)
    tag = re.search(r'<input type="checkbox" id="wiz-consent"[^>]*>', step1)
    assert tag and "required" in tag.group(0)
    label = step1[step1.index('<label class="wizard-consent" for="wiz-consent">') :]
    label = label[: label.index("</label>")]
    sk = re.search(r'data-sk="([^"]+)"', label).group(1)
    en = re.search(r'data-en="([^"]+)"', label).group(1)
    assert "súhlas" in sk and "zdravotné údaje" in sk and "zákonný zástupca" in sk
    assert "consent" in en and "health data" in en and "legal guardian" in en
    assert 'href="/privacy"' in step1 and 'href="/terms"' in step1
    # The checkbox comes before the create button.
    assert step1.index('id="wiz-consent"') < step1.index('id="wiz-create-btn"')


def test_wizard_sends_the_server_consent_version():
    m = re.search(r"var CONSENT_VERSION = '([^']+)';", JS)
    assert m and m.group(1) == CONSENT_VERSION
    submit = JS[JS.index("async function wizardStep1Submit()") :]
    submit = submit[: submit.index("\n    }\n")]
    assert "getElementById('wiz-consent').checked" in submit
    assert "consent: true" in submit and "consent_version: CONSENT_VERSION" in submit
    assert "innerHTML" not in submit


def test_wizard_explains_the_google_screen_before_the_oauth_button():
    step2 = _step(2)
    explainer = step2.index('id="wiz-google-explainer"')
    assert explainer < step2.index('data-action="wizardStep2Auth"')
    box = step2[explainer : step2.index("</div>", explainer)]
    assert "hasn’t verified this app" in box  # both languages quote Google's wording
    assert box.count("hasn’t verified this app") >= 3  # data-sk, data-en, text
    assert "100" in box and "myaccount.google.com/permissions" in box
    assert "celému vášmu Google Drive" in box and "whole Google Drive" in box
    assert "read-only" not in step2  # the scope is full drive (read and write)


def test_new_wizard_markup_has_no_inline_handlers_or_html_injection():
    for block in (_step(1), _step(2)):
        assert not re.search(r"\son[a-z]+\s*=", block)
        assert "data-html" not in block


# ── legal pages ──────────────────────────────────────────────────────────


async def _text(route) -> str:
    return (await route(None)).body.decode()


async def test_privacy_matches_the_pipeline():
    html = await _text(server.privacy_policy)
    # local processing, Drive as the store, no cloud in the background
    assert "local AI model on our own server in Slovakia (EU)" in html
    assert "does not send your documents to any cloud AI service" in html
    assert "<strong>Google Drive</strong>" in html and "your original files" in html
    assert "whole Google" in html and "only the folder you choose" in html
    assert "hasn't verified this app" in html and "at most 100" in html
    # consent record, access log, rights
    assert "Consent version 2026-11-17" in html
    assert "keyed hash of your\ne-mail address" in html or "keyed hash of your e-mail" in html
    assert "GDPR Art. 9(2)(a)" in html
    assert "within 30 days" in html
    assert "cannot connect their own Google Drive yet" in html  # ONBOARDING_MODE=closed
    assert "by invitation first" in html
    assert "general health records" in html
    assert "verification state" in html
    # backups without retention numbers the repo does not document
    assert "kept for a limited time" in html
    assert "7 daily" not in html and "6 monthly" not in html and "14 days" not in html
    assert "only the 5 newest" in html  # pre_store.KEEP


async def test_privacy_numbers_match_the_code():
    from oncofiles import app_log, pre_store

    html = await _text(server.privacy_policy)
    assert pre_store.KEEP == 5 and "only the 5 newest" in html
    assert "kept for 7 days" in html
    assert app_log.DEFAULT_RETENTION_DAYS == 7


async def test_terms_record_consent_and_manual_rights():
    html = await _text(server.terms_of_service)
    assert "Consent version 2026-11-17" in html
    assert "legal guardian" in html
    assert "within 30 days" in html
    assert "cannot connect their own Google Drive yet" in html  # ONBOARDING_MODE=closed
    assert "by invitation first" in html
    assert "500 documents per patient" in html
