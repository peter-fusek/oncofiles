"""Tests for the dashboard page and /api/documents endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from oncofiles.server import _check_bearer
from tests.helpers import ERIKA_UUID, make_doc

# ── _check_bearer helper ─────────────────────────────────────────────


def _make_request(auth_header: str | None = None):
    """Create a minimal mock request with optional authorization header."""
    request = MagicMock()
    headers = {}
    if auth_header is not None:
        headers["authorization"] = auth_header
    # Use MagicMock for headers so .get is settable
    mock_headers = MagicMock()
    mock_headers.get = lambda key, default="": headers.get(key, default)
    request.headers = mock_headers
    return request


def test_check_bearer_no_token_configured():
    """When MCP_BEARER_TOKEN is empty, _check_bearer returns None (allow)."""
    with patch("oncofiles.server.MCP_BEARER_TOKEN", ""):
        result = _check_bearer(_make_request())
        assert result is None


def test_check_bearer_missing_header():
    """Missing Authorization header returns 401."""
    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-token"):
        result = _check_bearer(_make_request())
        assert result is not None
        assert result.status_code == 401


def test_check_bearer_wrong_token():
    """Wrong bearer token returns 401."""
    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-token"):
        result = _check_bearer(_make_request("Bearer wrong-token"))
        assert result is not None
        assert result.status_code == 401


def test_check_bearer_correct_token():
    """Correct bearer token returns None (allow)."""
    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-token"):
        result = _check_bearer(_make_request("Bearer test-token"))
        assert result is None


def test_check_bearer_no_bearer_prefix():
    """Authorization header without 'Bearer ' prefix returns 401."""
    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-token"):
        result = _check_bearer(_make_request("Basic dXNlcjpwYXNz"))
        assert result is not None
        assert result.status_code == 401


def test_check_bearer_constant_time():
    """_check_bearer uses hmac.compare_digest (constant-time comparison)."""
    import inspect

    source = inspect.getsource(_check_bearer)
    assert "hmac.compare_digest" in source


# ── _build_document_matrix ────────────────────────────────────────────


async def test_build_document_matrix_empty_db(db):
    """Matrix returns empty results for empty database."""
    from oncofiles.tools.hygiene import _build_document_matrix

    result = await _build_document_matrix(db, patient_id=ERIKA_UUID)
    assert result["filter"] == "all"
    assert result["matched"] == 0
    assert result["summary"]["total"] == 0
    assert result["summary"]["fully_complete"] == 0
    assert result["documents"] == []


async def test_build_document_matrix_with_docs(db):
    """Matrix returns correct structure for documents."""
    from oncofiles.tools.hygiene import _build_document_matrix

    doc = make_doc(filename="20260101_Test_Labs.pdf")
    inserted = await db.insert_document(doc, patient_id=ERIKA_UUID)

    # Update with some fields to make it partially complete
    await db.update_document_ai_metadata(inserted.id, "Test summary", "tag1,tag2")

    result = await _build_document_matrix(db, patient_id=ERIKA_UUID)
    assert result["summary"]["total"] == 1
    assert result["summary"]["with_ai"] == 1
    assert result["matched"] == 1

    row = result["documents"][0]
    assert row["id"] == inserted.id
    assert row["has_ai"] is True
    assert row["is_synced"] is False
    assert row["fully_complete"] is False
    assert "gdrive_id" in row


async def test_build_document_matrix_filter_incomplete(db):
    """Filter 'incomplete' returns only docs with gaps."""
    from oncofiles.tools.hygiene import _build_document_matrix

    doc = make_doc(filename="test.pdf", gdrive_id=None)
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    result = await _build_document_matrix(db, filter_param="incomplete", patient_id=ERIKA_UUID)
    # Doc is incomplete (no AI, no sync, etc.) so it should be returned
    assert result["matched"] == 1

    result_all = await _build_document_matrix(db, filter_param="all", patient_id=ERIKA_UUID)
    assert result_all["matched"] == 1


async def test_build_document_matrix_filter_missing_ai(db):
    """Filter 'missing_ai' returns docs without AI summary."""
    from oncofiles.tools.hygiene import _build_document_matrix

    doc = make_doc(filename="test.pdf")
    inserted = await db.insert_document(doc, patient_id=ERIKA_UUID)

    # No AI summary → should be returned
    result = await _build_document_matrix(db, filter_param="missing_ai", patient_id=ERIKA_UUID)
    assert result["matched"] == 1

    # Add AI summary → should be excluded
    await db.update_document_ai_metadata(inserted.id, "Summary", "tags")
    result = await _build_document_matrix(db, filter_param="missing_ai", patient_id=ERIKA_UUID)
    assert result["matched"] == 0


async def test_build_document_matrix_summary_has_fully_complete(db):
    """Summary includes fully_complete count."""
    from oncofiles.tools.hygiene import _build_document_matrix

    result = await _build_document_matrix(db, patient_id=ERIKA_UUID)
    assert "fully_complete" in result["summary"]
    assert isinstance(result["summary"]["fully_complete"], int)


async def test_build_document_matrix_limit(db):
    """Limit parameter caps the returned page size. 'matched' reports total hits,
    'returned' reports the slice, 'summary.total' is the unfiltered patient total."""
    from oncofiles.tools.hygiene import _build_document_matrix

    for i in range(5):
        await db.insert_document(
            make_doc(filename=f"doc_{i}.pdf", file_id=f"file_{i}"), patient_id=ERIKA_UUID
        )

    result = await _build_document_matrix(db, limit=3, patient_id=ERIKA_UUID)
    assert result["matched"] == 5  # total filter hits (all 5 match filter='all')
    assert result["returned"] == 3  # page size
    assert result["limit"] == 3
    assert result["offset"] == 0
    assert len(result["documents"]) == 3
    assert result["summary"]["total"] == 5


async def test_build_document_matrix_pagination(db):
    """Offset + limit work together for true pagination (#419 fix)."""
    from oncofiles.tools.hygiene import _build_document_matrix

    for i in range(10):
        await db.insert_document(
            make_doc(filename=f"doc_{i:02d}.pdf", file_id=f"file_{i}"),
            patient_id=ERIKA_UUID,
        )

    page1 = await _build_document_matrix(db, limit=4, offset=0, patient_id=ERIKA_UUID)
    page2 = await _build_document_matrix(db, limit=4, offset=4, patient_id=ERIKA_UUID)
    page3 = await _build_document_matrix(db, limit=4, offset=8, patient_id=ERIKA_UUID)

    # All three pages report the same 'matched' total
    assert page1["matched"] == page2["matched"] == page3["matched"] == 10

    # Page sizes: 4, 4, 2 (10 docs)
    assert page1["returned"] == 4
    assert page2["returned"] == 4
    assert page3["returned"] == 2

    # Pages don't overlap
    ids_p1 = {d["id"] for d in page1["documents"]}
    ids_p2 = {d["id"] for d in page2["documents"]}
    ids_p3 = {d["id"] for d in page3["documents"]}
    assert not (ids_p1 & ids_p2)
    assert not (ids_p2 & ids_p3)
    assert len(ids_p1 | ids_p2 | ids_p3) == 10  # all unique


async def test_build_document_matrix_cap_and_clamp(db):
    """Verify the documented bounds (#419): default 500, ceiling 2000, clamp
    behavior on over/under-size requests.

    We can't exceed the MAX_DOCUMENTS_PER_PATIENT FUP limit in the test DB so
    we verify the *limit echoed back in the response* reflects the clamp,
    rather than inserting >2000 rows.
    """
    from oncofiles.tools.hygiene import _build_document_matrix

    for i in range(5):
        await db.insert_document(
            make_doc(filename=f"doc_{i}.pdf", file_id=f"file_{i}"),
            patient_id=ERIKA_UUID,
        )

    # Limits above ceiling are clamped to 2000
    clamped = await _build_document_matrix(db, limit=99_999, patient_id=ERIKA_UUID)
    assert clamped["limit"] == 2000

    # Negative / zero limits are clamped UP to 1 (min(max(1, limit), 2000))
    raised = await _build_document_matrix(db, limit=0, patient_id=ERIKA_UUID)
    assert raised["limit"] == 1
    assert raised["returned"] == 1  # we seeded 5 but asked for 1

    # Negative offset is clamped up to 0
    result = await _build_document_matrix(db, limit=10, offset=-5, patient_id=ERIKA_UUID)
    assert result["offset"] == 0


# ── Dashboard route (source-level checks) ────────────────────────────


def test_dashboard_route_exists():
    """Dashboard route is registered on the MCP server."""
    import inspect

    from oncofiles.server import dashboard

    assert callable(dashboard)
    source = inspect.getsource(dashboard)
    assert "HTMLResponse" in source


def test_api_documents_route_exists():
    """API documents route is registered on the MCP server."""
    import inspect

    from oncofiles.server import api_documents

    assert callable(api_documents)
    source = inspect.getsource(api_documents)
    assert "_check_dashboard_auth" in source
    assert "_build_document_matrix" in source


def test_dashboard_verify_route_exists():
    """Dashboard verify route is registered on the MCP server."""
    import inspect

    from oncofiles.server import dashboard_verify

    assert callable(dashboard_verify)
    source = inspect.getsource(dashboard_verify)
    assert "tokeninfo" in source
    assert "session_token" in source


def test_dashboard_html_exists():
    """Dashboard HTML file exists and contains expected content."""
    from pathlib import Path

    html_path = Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html"
    assert html_path.exists()
    content = html_path.read_text()
    assert "Oncofiles Dashboard" in content
    assert "pipeline" in content.lower() or "funnel" in content.lower()
    assert "google" in content.lower()
    assert "sessionStorage" in content


# ── Session tokens ───────────────────────────────────────────────────


def test_make_and_verify_session_token():
    """Session token roundtrip works."""
    from oncofiles.server import _make_session_token, _verify_session_token

    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-secret"):
        token = _make_session_token("user@example.com")
        email = _verify_session_token(token)
        assert email == "user@example.com"


def test_session_token_rejects_tampered():
    """Tampered session token is rejected."""
    from oncofiles.server import _make_session_token, _verify_session_token

    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-secret"):
        token = _make_session_token("user@example.com")
        parts = token.rsplit(".", 1)
        tampered = parts[0] + "." + "a" * 32
        assert _verify_session_token(tampered) is None


def test_session_token_rejects_expired():
    """Expired session token is rejected."""
    from oncofiles.server import _verify_session_token

    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-secret"):
        # Craft a token with expiry in the past
        import hashlib
        import hmac as _hmac

        email = "user@example.com"
        expiry = "1000000000"  # year 2001 — definitely expired
        key = b"test-secret"
        payload = f"{email}.{expiry}"
        sig = _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]
        token = f"{payload}.{sig}"
        assert _verify_session_token(token) is None


def test_check_dashboard_auth_accepts_session():
    """_check_dashboard_auth accepts valid session tokens."""
    from oncofiles.server import _check_dashboard_auth, _make_session_token

    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-secret"):
        session = _make_session_token("user@example.com")
        request = _make_request("Bearer session:" + session)
        result = _check_dashboard_auth(request)
        assert result is None  # No error = allowed


def test_check_dashboard_auth_rejects_invalid():
    """_check_dashboard_auth rejects invalid tokens."""
    from oncofiles.server import _check_dashboard_auth

    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-secret"):
        request = _make_request("Bearer session:garbage")
        result = _check_dashboard_auth(request)
        assert result is not None
        assert result.status_code == 401


def test_dashboard_admin_emails_config():
    """DASHBOARD_ADMIN_EMAILS defaults to a list (set via env var in prod)."""
    from oncofiles.config import DASHBOARD_ADMIN_EMAILS

    assert isinstance(DASHBOARD_ADMIN_EMAILS, list)


# ── Open signup (#341) ──────────────────────────────────────────────


def test_get_dashboard_email_returns_email_for_session():
    """_get_dashboard_email extracts email from valid session token."""
    from oncofiles.server import _get_dashboard_email, _make_session_token

    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-secret"):
        session = _make_session_token("user@example.com")
        request = _make_request("Bearer session:" + session)
        assert _get_dashboard_email(request) == "user@example.com"


def test_get_dashboard_email_returns_none_for_bearer():
    """_get_dashboard_email returns None for standard bearer token auth."""
    from oncofiles.server import _get_dashboard_email

    request = _make_request("Bearer some-token")
    assert _get_dashboard_email(request) is None


def test_is_admin_email_true_for_configured():
    """_is_admin_email returns True for configured admin emails."""
    from oncofiles.server import _is_admin_email

    with patch("oncofiles.server.DASHBOARD_ADMIN_EMAILS", ["admin@test.com"]):
        assert _is_admin_email("admin@test.com") is True
        assert _is_admin_email("Admin@Test.com") is True


def test_is_admin_email_false_for_regular_user():
    """_is_admin_email returns False for non-admin emails."""
    from oncofiles.server import _is_admin_email

    with patch("oncofiles.server.DASHBOARD_ADMIN_EMAILS", ["admin@test.com"]):
        assert _is_admin_email("user@test.com") is False
        assert _is_admin_email(None) is False


def test_dashboard_verify_no_allowlist_check():
    """dashboard_verify no longer references DASHBOARD_ALLOWED_EMAILS for gating."""
    import inspect

    from oncofiles.server import dashboard_verify

    source = inspect.getsource(dashboard_verify)
    # The allowlist gate has been removed — open signup
    assert "access denied for this email" not in source
    assert "DASHBOARD_ALLOWED_EMAILS" not in source


# ── Patient ID guard (#342) ─────────────────────────────────────────


def test_get_patient_id_raises_when_empty():
    """_get_patient_id raises ValueError with helpful message when no patient selected."""
    import pytest

    from oncofiles.tools._helpers import _get_patient_id

    with patch("oncofiles.patient_middleware._current_patient_id") as mock_cv:
        mock_cv.get.return_value = ""
        with pytest.raises(ValueError, match="No patient selected"):
            _get_patient_id()


def test_get_patient_id_allows_empty_when_not_required():
    """_get_patient_id(required=False) returns empty string without raising."""
    from oncofiles.tools._helpers import _get_patient_id

    with patch("oncofiles.patient_middleware._current_patient_id") as mock_cv:
        mock_cv.get.return_value = ""
        result = _get_patient_id(required=False)
        assert result == ""


def test_get_patient_id_returns_value_when_set():
    """_get_patient_id returns the patient_id when set."""
    from oncofiles.tools._helpers import _get_patient_id

    with patch("oncofiles.patient_middleware._current_patient_id") as mock_cv:
        mock_cv.get.return_value = "test-patient-123"
        assert _get_patient_id() == "test-patient-123"


# ── OAuth callback patient validation (#345) ────────────────────────


def test_oauth_callback_validates_patient_exists():
    """oauth_callback checks that patient exists before exchanging code."""
    import inspect

    from oncofiles.server import oauth_callback

    source = inspect.getsource(oauth_callback)
    assert "get_patient" in source
    assert "no longer exists" in source.lower() or "Patient no longer exists" in source
