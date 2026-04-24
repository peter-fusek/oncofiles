"""Regression lock for #489 Part B — dashboard body-patient-id ACL.

Parallel to Felix Vítek's oncoteam#438 Bug 3. Five dashboard POST endpoints
read `body.get("patient_id")` and previously skipped the caregiver-email
scope check that `_get_dashboard_patient_id` enforces on query-param callers.
`_require_body_patient_access` is the helper that closes that gap.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from oncofiles.server import _require_body_patient_access


def _make_request(auth_header: str | None = None):
    request = MagicMock()
    headers = {}
    if auth_header is not None:
        headers["authorization"] = auth_header
    mock_headers = MagicMock()
    mock_headers.get = lambda key, default="": headers.get(key, default)
    request.headers = mock_headers
    return request


def _make_patient(caregiver_email: str):
    return SimpleNamespace(caregiver_email=caregiver_email)


# ── Admin bypass paths ──────────────────────────────────────────────


async def test_bearer_admin_bypasses_acl():
    """Static bearer callers are implicitly admin per v5.15 contract."""
    db = MagicMock()
    db.get_patient = AsyncMock(return_value=_make_patient("other@example.com"))
    req = _make_request("Bearer admin-secret-token")
    err = await _require_body_patient_access(req, db, "q1b")
    assert err is None
    # Bearer path never even looks up the patient.
    db.get_patient.assert_not_called()


async def test_session_admin_email_bypasses_acl():
    """Dashboard session for a DASHBOARD_ADMIN_EMAILS address bypasses."""
    db = MagicMock()
    db.get_patient = AsyncMock(return_value=_make_patient("someone@other.com"))
    req = _make_request("Bearer session:xyz")
    with (
        patch("oncofiles.server._get_dashboard_email", return_value="peter@instarea.sk"),
        patch("oncofiles.server._is_admin_email", return_value=True),
    ):
        err = await _require_body_patient_access(req, db, "q1b")
    assert err is None


# ── Caregiver match allows ──────────────────────────────────────────


async def test_caregiver_email_match_allows():
    db = MagicMock()
    db.get_patient = AsyncMock(return_value=_make_patient("caregiver@example.com"))
    req = _make_request("Bearer session:xyz")
    with (
        patch("oncofiles.server._get_dashboard_email", return_value="caregiver@example.com"),
        patch("oncofiles.server._is_admin_email", return_value=False),
    ):
        err = await _require_body_patient_access(req, db, "q1b")
    assert err is None


async def test_caregiver_email_match_case_insensitive():
    db = MagicMock()
    db.get_patient = AsyncMock(return_value=_make_patient("Caregiver@Example.com"))
    req = _make_request("Bearer session:xyz")
    with (
        patch("oncofiles.server._get_dashboard_email", return_value="caregiver@example.com"),
        patch("oncofiles.server._is_admin_email", return_value=False),
    ):
        err = await _require_body_patient_access(req, db, "q1b")
    assert err is None


async def test_caregiver_email_list_match():
    """caregiver_email supports comma-separated lists."""
    db = MagicMock()
    db.get_patient = AsyncMock(return_value=_make_patient("primary@example.com,second@example.com"))
    req = _make_request("Bearer session:xyz")
    with (
        patch("oncofiles.server._get_dashboard_email", return_value="second@example.com"),
        patch("oncofiles.server._is_admin_email", return_value=False),
    ):
        err = await _require_body_patient_access(req, db, "q1b")
    assert err is None


# ── Non-match denials — the core vulnerability #489 Part B closes ──


async def test_non_admin_non_caregiver_denied_403():
    """The bug class Felix flagged: session user trying cross-patient write."""
    db = MagicMock()
    db.get_patient = AsyncMock(return_value=_make_patient("owner@example.com"))
    req = _make_request("Bearer session:xyz")
    with (
        patch("oncofiles.server._get_dashboard_email", return_value="attacker@example.com"),
        patch("oncofiles.server._is_admin_email", return_value=False),
    ):
        err = await _require_body_patient_access(req, db, "q1b")
    assert err is not None
    assert err.status_code == 403


async def test_unknown_patient_returns_404():
    db = MagicMock()
    db.get_patient = AsyncMock(return_value=None)
    req = _make_request("Bearer session:xyz")
    with (
        patch("oncofiles.server._get_dashboard_email", return_value="user@example.com"),
        patch("oncofiles.server._is_admin_email", return_value=False),
    ):
        err = await _require_body_patient_access(req, db, "nonexistent")
    assert err is not None
    assert err.status_code == 404


async def test_no_session_email_and_non_admin_denied():
    """Caller has no session email, no bearer match, no admin status → 403."""
    db = MagicMock()
    db.get_patient = AsyncMock(return_value=_make_patient("someone@example.com"))
    req = _make_request("Bearer session:invalid-token")
    with (
        patch("oncofiles.server._get_dashboard_email", return_value=None),
        patch("oncofiles.server._is_admin_email", return_value=False),
    ):
        err = await _require_body_patient_access(req, db, "q1b")
    assert err is not None
    assert err.status_code == 403


# ── Source-level invariant — all 5 endpoints call the helper ───────


def test_all_5_body_patient_id_endpoints_call_helper():
    """Regression lock: every dashboard POST that reads `body.get('patient_id')`
    must go through `_require_body_patient_access` before any DB mutation."""
    import inspect

    from oncofiles.server import (
        api_create_patient_token,
        api_create_share_link,
        api_enhance_trigger,
        api_gdrive_set_folder,
        api_sync_trigger,
    )

    for fn in (
        api_create_patient_token,
        api_sync_trigger,
        api_enhance_trigger,
        api_gdrive_set_folder,
        api_create_share_link,
    ):
        source = inspect.getsource(fn)
        assert "_require_body_patient_access" in source, (
            f"{fn.__name__} must call _require_body_patient_access — "
            f"cross-patient ACL gap (#489 Part B)"
        )
