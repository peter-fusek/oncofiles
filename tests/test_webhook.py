"""Tests for oncoteam webhook notifications."""

from unittest.mock import patch


async def test_notify_disabled_when_no_url(monkeypatch):
    """Webhook is a no-op when ONCOTEAM_WEBHOOK_URL is not set."""
    monkeypatch.setattr("oncofiles.webhook.ONCOTEAM_WEBHOOK_URL", "")
    from oncofiles.webhook import _notify_oncoteam_async

    # Should return immediately without making any HTTP call
    await _notify_oncoteam_async(document_id=1, filename="test.pdf")


async def test_notify_sends_correct_payload(monkeypatch):
    """Webhook sends correct JSON payload when URL is configured."""
    monkeypatch.setattr(
        "oncofiles.webhook.ONCOTEAM_WEBHOOK_URL",
        "https://test.example.com/webhook",
    )
    monkeypatch.setattr("oncofiles.webhook.ONCOTEAM_WEBHOOK_TOKEN", "test-token")

    import httpx

    from oncofiles.webhook import _notify_oncoteam_async

    captured = {}

    async def mock_post(self, url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")

        class MockResponse:
            status_code = 200
            text = '{"status": "pipeline_started"}'

        return MockResponse()

    with patch.object(httpx.AsyncClient, "post", mock_post):
        await _notify_oncoteam_async(document_id=42, filename="labs.pdf", category="labs")

    assert captured["url"] == "https://test.example.com/webhook"
    assert captured["json"]["document_id"] == 42
    assert captured["json"]["filename"] == "labs.pdf"
    assert captured["json"]["category"] == "labs"
    assert "uploaded_at" in captured["json"]
    assert captured["headers"]["Authorization"] == "Bearer test-token"


async def test_notify_failure_does_not_raise(monkeypatch):
    """Webhook failure is caught and logged, never raises."""
    monkeypatch.setattr(
        "oncofiles.webhook.ONCOTEAM_WEBHOOK_URL",
        "https://test.example.com/webhook",
    )
    monkeypatch.setattr("oncofiles.webhook.ONCOTEAM_WEBHOOK_TOKEN", "test-token")

    import httpx

    from oncofiles.webhook import _notify_oncoteam_async

    async def mock_post(self, url, **kwargs):
        raise httpx.ConnectError("Connection refused")

    with patch.object(httpx.AsyncClient, "post", mock_post):
        # Should not raise
        await _notify_oncoteam_async(document_id=99)


async def test_notify_sync_wrapper_disabled(monkeypatch):
    """Sync wrapper is a no-op when URL is not set."""
    monkeypatch.setattr("oncofiles.webhook.ONCOTEAM_WEBHOOK_URL", "")
    from oncofiles.webhook import notify_oncoteam

    # Should not create any task or raise
    notify_oncoteam(document_id=1)
