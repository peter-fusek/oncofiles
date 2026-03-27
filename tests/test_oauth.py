"""Tests for OAuth 2.0 token management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oncofiles.database import Database
from oncofiles.models import OAuthToken
from oncofiles.oauth import is_token_expired
from tests.helpers import ERIKA_UUID

# ── Token expiry checks ──────────────────────────────────────────────────


def test_token_expired_when_none():
    assert is_token_expired(None) is True


def test_token_expired_when_past():
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert is_token_expired(past) is True


def test_token_not_expired_when_future():
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert is_token_expired(future) is False


def test_token_expired_invalid_string():
    assert is_token_expired("not-a-date") is True


# ── Database CRUD ────────────────────────────────────────────────────────


async def test_upsert_and_get_oauth_token(db: Database):
    """Insert and retrieve an OAuth token."""
    token = OAuthToken(
        patient_id=ERIKA_UUID,
        access_token="access_123",
        refresh_token="refresh_456",
        token_expiry=datetime(2026, 12, 31, tzinfo=UTC),
    )
    saved = await db.upsert_oauth_token(token)
    assert saved.access_token == "access_123"
    assert saved.refresh_token == "refresh_456"
    assert saved.patient_id == ERIKA_UUID

    # Retrieve
    fetched = await db.get_oauth_token(patient_id=ERIKA_UUID)
    assert fetched is not None
    assert fetched.access_token == "access_123"


async def test_upsert_updates_existing_token(db: Database):
    """Upserting with same user/provider updates the token."""
    token1 = OAuthToken(patient_id=ERIKA_UUID, access_token="old", refresh_token="refresh")
    await db.upsert_oauth_token(token1)

    token2 = OAuthToken(patient_id=ERIKA_UUID, access_token="new", refresh_token="refresh_new")
    await db.upsert_oauth_token(token2)

    fetched = await db.get_oauth_token(patient_id=ERIKA_UUID)
    assert fetched.access_token == "new"
    assert fetched.refresh_token == "refresh_new"


async def test_get_oauth_token_missing(db: Database):
    """Returns None when no token exists."""
    fetched = await db.get_oauth_token(patient_id=ERIKA_UUID)
    assert fetched is None


async def test_update_oauth_folder(db: Database):
    """Set the GDrive folder ID."""
    token = OAuthToken(patient_id=ERIKA_UUID, access_token="a", refresh_token="r")
    await db.upsert_oauth_token(token)

    await db.update_oauth_folder(ERIKA_UUID, "google", "folder_abc")

    fetched = await db.get_oauth_token(patient_id=ERIKA_UUID)
    assert fetched.gdrive_folder_id == "folder_abc"


# ── Scope parsing ───────────────────────────────────────────────────────


def test_parse_granted_scopes():
    from oncofiles.oauth import parse_granted_scopes

    result = parse_granted_scopes(
        {
            "scope": "https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/gmail.readonly"
        }
    )
    assert set(result) == {
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/gmail.readonly",
    }


def test_parse_granted_scopes_empty():
    from oncofiles.oauth import parse_granted_scopes

    assert parse_granted_scopes({}) == []
    assert parse_granted_scopes({"scope": ""}) == []


async def test_scope_merge_on_reauth(db: Database):
    """Re-authorization should merge scopes, not replace them.

    Simulates the bug where Calendar scope was dropped when Gmail re-auth
    only returned drive + gmail scopes.
    """
    import json

    # Existing token has all 3 scopes
    existing = OAuthToken(
        patient_id=ERIKA_UUID,
        access_token="old_access",
        refresh_token="old_refresh",
        token_expiry=datetime(2026, 12, 31, tzinfo=UTC),
        granted_scopes=json.dumps(
            [
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.readonly",
            ]
        ),
    )
    await db.upsert_oauth_token(existing)

    # Simulate re-auth that only returns drive + gmail
    new_scopes = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]

    # Merge logic (mirrors server.py oauth callback)
    existing_token = await db.get_oauth_token(patient_id=ERIKA_UUID)
    existing_scopes = json.loads(existing_token.granted_scopes)
    merged = sorted(set(existing_scopes) | set(new_scopes))

    updated = OAuthToken(
        patient_id=ERIKA_UUID,
        access_token="new_access",
        refresh_token="new_refresh",
        token_expiry=datetime(2026, 12, 31, tzinfo=UTC),
        granted_scopes=json.dumps(merged),
    )
    await db.upsert_oauth_token(updated)

    fetched = await db.get_oauth_token(patient_id=ERIKA_UUID)
    scopes = json.loads(fetched.granted_scopes)
    assert "https://www.googleapis.com/auth/calendar.readonly" in scopes
    assert "https://www.googleapis.com/auth/gmail.readonly" in scopes
    assert "https://www.googleapis.com/auth/drive" in scopes
    assert fetched.access_token == "new_access"
