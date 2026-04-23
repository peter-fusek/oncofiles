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


# ── Circuit-breaker 503 contract (#412 + #469) ───────────────────────


def test_circuit_breaker_503_matches_breaker_runtimeerror():
    """Helper converts breaker RuntimeError → 503 + Retry-After:30."""
    import json

    from oncofiles.server import _circuit_breaker_503

    exc = RuntimeError("Circuit breaker open — DB unavailable, retry in 28s")
    resp = _circuit_breaker_503(exc, "/status")

    assert resp is not None
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"
    body = json.loads(bytes(resp.body).decode())
    assert "Database briefly unavailable" in body["error"]


def test_circuit_breaker_503_passes_through_other_runtimeerrors():
    """Non-breaker RuntimeError returns None — caller handles as 500."""
    from oncofiles.server import _circuit_breaker_503

    exc = RuntimeError("some other runtime error")
    assert _circuit_breaker_503(exc, "/status") is None


def test_circuit_breaker_503_passes_through_value_errors():
    """Non-RuntimeError exceptions return None."""
    from oncofiles.server import _circuit_breaker_503

    exc = ValueError("bad input")
    assert _circuit_breaker_503(exc, "/status") is None


def test_circuit_breaker_503_matches_real_breaker_message():
    """The helper matches the actual message raised by _CircuitBreaker.check()."""
    from oncofiles.database._base import _CircuitBreaker
    from oncofiles.server import _circuit_breaker_503

    cb = _CircuitBreaker(max_failures=1, window=60.0, cooldown=30.0)
    cb.record_failure()
    try:
        cb.check()
    except RuntimeError as exc:
        resp = _circuit_breaker_503(exc, "/test")
        assert resp is not None
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "30"
    else:
        raise AssertionError("Breaker should have raised RuntimeError")


def test_status_endpoint_uses_breaker_helper():
    """/status catches circuit breaker and returns 503 via the helper."""
    import inspect

    from oncofiles.server import status

    source = inspect.getsource(status)
    assert "_circuit_breaker_503" in source
    assert '"/status"' in source


def test_api_documents_uses_breaker_helper():
    """/api/documents catches circuit breaker and returns 503 via the helper."""
    import inspect

    from oncofiles.server import api_documents

    source = inspect.getsource(api_documents)
    assert "_circuit_breaker_503" in source
    assert '"/api/documents"' in source


def test_api_prompt_log_uses_breaker_helper():
    """/api/prompt-log catches circuit breaker and returns 503 via the helper."""
    import inspect

    from oncofiles.server import api_prompt_log

    source = inspect.getsource(api_prompt_log)
    assert "_circuit_breaker_503" in source
    assert '"/api/prompt-log"' in source


def test_dashboard_apifetch_retries_5xx():
    """apiFetch retries on 500/502/504 and handles 503 separately (#469 Phase 4)."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    # Budget-based retries for transient 5xx
    assert "500" in html and "502" in html and "504" in html
    # Dedicated 503 path that surfaces retryAfterMs upward
    assert "resp.status === 503" in html
    assert "retryAfterMs" in html
    # No more naive "retry once on 500 with 2s" that predates Phase 4
    assert "Retry once on 500 (Turso contention during startup syncs)" not in html


def test_dashboard_apifetch_honors_retry_after():
    """apiFetch parses the Retry-After header (seconds or HTTP-date)."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    assert "_parseRetryAfter" in html
    assert "'Retry-After'" in html
    # seconds-form and HTTP-date form both covered
    assert "parseInt" in html and "Date.parse" in html


def test_dashboard_apifetch_has_abort_controller():
    """apiFetch uses AbortController with a per-request timeout."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    assert "AbortController" in html
    assert "AbortError" in html
    assert "perRequestTimeoutMs" in html


def test_dashboard_breaker_banner_bilingual():
    """Friendly countdown banner renders in SK and EN."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    assert "showBreakerBanner" in html
    # SK text — includes the "Databáza je krátko nedostupná" phrase
    assert "Databáza je krátko nedostupná" in html
    # EN text — "Database briefly unavailable"
    assert "Database briefly unavailable" in html
    # Live countdown via setInterval
    assert "_breakerCountdownTimer" in html
    # Auto-retry fires refresh() when countdown reaches zero
    assert "refresh()" in html


def test_dashboard_swr_cache_helpers_exist():
    """Stale-while-revalidate cache helpers are present (#469 Phase 5)."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    assert "_swrGet" in html
    assert "_swrSet" in html
    assert "_swrCacheClearAll" in html
    assert "_hydrateFromSwrCache" in html


def test_dashboard_swr_per_patient_keying():
    """Cache key includes currentPatientId so cross-patient leak is impossible."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    # The key helper must include the patient id in its key string
    assert "(currentPatientId || 'default')" in html
    assert "dashCache:" in html


def test_dashboard_swr_has_max_age_cap():
    """Cache entries older than _SWR_MAX_AGE_MS are evicted, not deceptively served."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    assert "_SWR_MAX_AGE_MS" in html
    # 10 min default, expressed as 10 * 60 * 1000
    assert "10 * 60 * 1000" in html


def test_dashboard_swr_refresh_hydrates_before_skeletons():
    """refresh() hydrates from cache first, skips skeletons if cache hit."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    # The order matters: hydrate must come before showSkeletons in refresh()
    refresh_start = html.index("async function refresh()")
    refresh_slice = html[refresh_start : refresh_start + 2000]
    assert "_hydrateFromSwrCache" in refresh_slice
    # And skeletons only show when nothing was hydrated
    assert "if (!hydrated) showSkeletons()" in refresh_slice


def test_dashboard_swr_writes_cache_on_success():
    """Successful fetches populate the cache for the 3 critical endpoints."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    # All three critical endpoints write to cache after successful render
    assert "_swrSet('status', status)" in html
    assert "_swrSet('documents', docs)" in html
    assert "_swrSet('prompt-log', prompts)" in html


def test_dashboard_admin_breaker_widget_present():
    """Admin-only breaker widget is wired to /readiness.circuit_breaker (#469 Phase 7)."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    assert 'id="breaker-widget"' in html
    assert "updateBreakerWidget" in html
    # Fetches /readiness (the /readiness endpoint is unauth-safe)
    assert "fetch('/readiness'" in html
    # Visibility gated on admin
    assert "if (!isAdminMode) return" in html
    # Reflects state via CSS classes
    assert "state-open" in html and "state-closed" in html and "state-half_open" in html
    # Trips counter element is populated
    assert 'id="bw-trips"' in html
    # Widget is invoked as part of the refresh() cycle (exact placement may
    # vary but it must live inside refresh() so every tick updates the display).
    refresh_start = html.index("async function refresh()")
    refresh_slice = html[refresh_start : refresh_start + 6000]
    assert "updateBreakerWidget()" in refresh_slice


def test_dashboard_swr_clears_on_logout():
    """logout() wipes the cache so a different user can't see prior data."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    logout_start = html.index("function logout()")
    logout_slice = html[logout_start : logout_start + 500]
    assert "_swrCacheClearAll" in logout_slice


def test_readiness_includes_circuit_breaker_when_turso():
    """/readiness exposes circuit_breaker stats when backed by Turso (#469 Phase 3)."""
    import inspect

    from oncofiles.server import readiness

    source = inspect.getsource(readiness)
    assert "circuit_breaker_stats" in source
    assert '"circuit_breaker"' in source


def test_readiness_uses_circuit_breaker_503_helper():
    """/readiness exception path goes through the 503 contract helper.

    Without the helper, a breaker trip during the reconnect_if_stale probe
    would return the generic 'degraded' 503 (body hides the Retry-After),
    masking the trip signal from clients.
    """
    import inspect

    from oncofiles.server import readiness

    source = inspect.getsource(readiness)
    assert "_circuit_breaker_503" in source
    assert '"/readiness"' in source


def test_database_base_has_circuit_breaker_stats():
    """Database.circuit_breaker_stats() returns None for aiosqlite, dict for Turso."""
    from oncofiles.database._base import DatabaseBase

    assert hasattr(DatabaseBase, "circuit_breaker_stats")
    # Source-level sanity that it falls through for aiosqlite
    import inspect

    source = inspect.getsource(DatabaseBase.circuit_breaker_stats)
    assert "return None" in source
    assert "_TursoConnection" in source


def test_dashboard_refresh_surfaces_503_without_red_cascade():
    """refresh() detects 503 errors and shows the breaker banner instead of
    'Partial load failure: HTTP 503 × N'."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src" / "oncofiles" / "dashboard.html").read_text()
    assert "trackBreaker" in html
    assert "breakerError" in html
    # Fallback generic error path still exists for non-breaker cascades
    assert "Partial load failure" in html


def test_status_reconnect_no_longer_uses_suppress_exception():
    """The silent suppress(Exception) wrap around reconnect_if_stale is gone (#469).

    A bare suppress(Exception) hides a RuntimeError('Circuit breaker open…')
    from the outer handler and prevents the 503 → Retry-After contract from firing.
    """
    import inspect

    from oncofiles.server import status

    source = inspect.getsource(status)
    assert "reconnect_if_stale" in source
    # The whole block immediately preceding reconnect_if_stale must NOT be
    # `with suppress(Exception)` anymore — scope the check to the reconnect call.
    idx = source.index("reconnect_if_stale")
    preceding = source[max(0, idx - 200) : idx]
    assert "suppress(Exception)" not in preceding, (
        "suppress(Exception) around reconnect_if_stale masks the breaker "
        "RuntimeError; use a specific except TimeoutError instead"
    )
    # And there should be an explicit TimeoutError clause nearby.
    following = source[idx : idx + 500]
    assert "TimeoutError" in following


# ── #476: cross-patient leak guard — sentinel on no-access ─────────────


def test_no_patient_access_sentinel_is_truthy_and_non_uuid():
    """Sentinel must be truthy so `if patient_id:` guards apply DB filter,
    AND must not be a valid UUID so `WHERE patient_id = ?` matches 0 rows.
    """
    from oncofiles.server import NO_PATIENT_ACCESS_SENTINEL

    assert NO_PATIENT_ACCESS_SENTINEL, "sentinel must be truthy — empty string leaks"
    assert bool(NO_PATIENT_ACCESS_SENTINEL) is True
    # Not a valid UUID format — 36 chars with hyphens at 8-4-4-4-12
    assert len(NO_PATIENT_ACCESS_SENTINEL) != 36
    assert "-" not in NO_PATIENT_ACCESS_SENTINEL


def test_get_dashboard_patient_id_returns_sentinel_not_empty_string():
    """Regression — #476 P0 leak. Previously returned "" when caller has
    no authorized patient, which bypassed `if patient_id:` filters in DB
    layer and produced cross-patient queries.
    """
    import inspect

    from oncofiles.server import _get_dashboard_patient_id

    source = inspect.getsource(_get_dashboard_patient_id)
    # No `return ""` should remain in the function — it's the leak signature.
    # (Note: strings like raw="" are fine; only bare `return ""` is the bug.)
    assert 'return ""' not in source, (
        'return "" in _get_dashboard_patient_id re-introduces the #476 leak'
    )
    # Sentinel must be returned in the no-access paths
    assert "NO_PATIENT_ACCESS_SENTINEL" in source
