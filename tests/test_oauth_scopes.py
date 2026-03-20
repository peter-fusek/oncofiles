"""Tests for OAuth scope management."""

from oncofiles.oauth import (
    ALL_SCOPES,
    SCOPE_CALENDAR,
    SCOPE_DRIVE,
    SCOPE_GMAIL,
    get_auth_url_for_scopes,
    parse_granted_scopes,
)


def test_scope_constants():
    assert "drive" in SCOPE_DRIVE
    assert "gmail" in SCOPE_GMAIL
    assert "calendar" in SCOPE_CALENDAR


def test_all_scopes_contains_all():
    assert SCOPE_DRIVE in ALL_SCOPES
    assert SCOPE_GMAIL in ALL_SCOPES
    assert SCOPE_CALENDAR in ALL_SCOPES


def test_parse_granted_scopes():
    response = {
        "scope": "https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/gmail.readonly"
    }
    scopes = parse_granted_scopes(response)
    assert len(scopes) == 2
    assert SCOPE_DRIVE in scopes
    assert SCOPE_GMAIL in scopes


def test_parse_granted_scopes_empty():
    assert parse_granted_scopes({}) == []
    assert parse_granted_scopes({"scope": ""}) == []


def test_auth_url_includes_scopes(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "test-token")
    # Force config reload by patching the module-level constants
    import oncofiles.config as cfg
    import oncofiles.oauth as oauth

    monkeypatch.setattr(cfg, "GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(cfg, "MCP_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(oauth, "GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(oauth, "MCP_BEARER_TOKEN", "test-token")

    url = get_auth_url_for_scopes([SCOPE_DRIVE, SCOPE_GMAIL])
    assert "gmail.readonly" in url
    assert "drive" in url
    assert "include_granted_scopes=true" in url
