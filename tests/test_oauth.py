"""Tests for OAuth 2.0 token management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oncofiles.database import Database
from oncofiles.models import OAuthToken
from oncofiles.oauth import is_token_expired

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
        access_token="access_123",
        refresh_token="refresh_456",
        token_expiry=datetime(2026, 12, 31, tzinfo=UTC),
    )
    saved = await db.upsert_oauth_token(token)
    assert saved.access_token == "access_123"
    assert saved.refresh_token == "refresh_456"
    assert saved.user_id == "default"

    # Retrieve
    fetched = await db.get_oauth_token()
    assert fetched is not None
    assert fetched.access_token == "access_123"


async def test_upsert_updates_existing_token(db: Database):
    """Upserting with same user/provider updates the token."""
    token1 = OAuthToken(access_token="old", refresh_token="refresh")
    await db.upsert_oauth_token(token1)

    token2 = OAuthToken(access_token="new", refresh_token="refresh_new")
    await db.upsert_oauth_token(token2)

    fetched = await db.get_oauth_token()
    assert fetched.access_token == "new"
    assert fetched.refresh_token == "refresh_new"


async def test_get_oauth_token_missing(db: Database):
    """Returns None when no token exists."""
    fetched = await db.get_oauth_token()
    assert fetched is None


async def test_update_oauth_folder(db: Database):
    """Set the GDrive folder ID."""
    token = OAuthToken(access_token="a", refresh_token="r")
    await db.upsert_oauth_token(token)

    await db.update_oauth_folder("default", "google", "folder_abc")

    fetched = await db.get_oauth_token()
    assert fetched.gdrive_folder_id == "folder_abc"
