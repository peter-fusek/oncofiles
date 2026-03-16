"""Tests for GDriveClient."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from oncofiles.gdrive_client import create_gdrive_client

MT_PDF = "application/pdf"
MT_FOLDER = "application/vnd.google-apps.folder"


def _make_client():
    """Create a GDriveClient with mocked Google dependencies."""
    with (
        patch("google.oauth2.service_account.Credentials") as mock_creds_cls,
        patch("googleapiclient.discovery.build") as mock_build,
    ):
        mock_creds_cls.from_service_account_info.return_value = MagicMock()
        service = MagicMock()
        mock_build.return_value = service

        from oncofiles.gdrive_client import GDriveClient

        # {"type": "service_account"} base64-encoded
        client = GDriveClient(credentials_base64="eyJ0eXBlIjogInNlcnZpY2VfYWNjb3VudCJ9")
        return client, service


def test_download():
    """Test downloading a file by GDrive ID."""
    client, service = _make_client()

    with patch("googleapiclient.http.MediaIoBaseDownload") as mock_dl_cls:
        mock_dl = MagicMock()
        mock_dl.next_chunk.return_value = (None, True)
        mock_dl_cls.return_value = mock_dl

        service.files.return_value.get_media.return_value = MagicMock()

        result = client.download("file123")
        assert isinstance(result, bytes)
        service.files.return_value.get_media.assert_called_once_with(fileId="file123")


def test_list_folder():
    """Test listing files in a GDrive folder."""
    client, service = _make_client()

    service.files.return_value.list.return_value.execute.return_value = {
        "files": [
            {
                "id": "f1",
                "name": "doc1.pdf",
                "mimeType": MT_PDF,
                "modifiedTime": "2024-01-01T00:00:00Z",
            },
            {
                "id": "f2",
                "name": "doc2.pdf",
                "mimeType": MT_PDF,
                "modifiedTime": "2024-02-01T00:00:00Z",
            },
        ],
    }

    result = client.list_folder("folder123")
    assert len(result) == 2
    assert result[0]["id"] == "f1"
    assert result[1]["name"] == "doc2.pdf"


def test_list_folder_recursive():
    """Test that subfolders are recursed into."""
    client, service = _make_client()

    call_count = 0

    def mock_execute():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "files": [
                    {"id": "sub1", "name": "Subfolder", "mimeType": MT_FOLDER},
                    {
                        "id": "f1",
                        "name": "root.pdf",
                        "mimeType": MT_PDF,
                        "modifiedTime": "2024-01-01T00:00:00Z",
                    },
                ],
            }
        return {
            "files": [
                {
                    "id": "f2",
                    "name": "sub.pdf",
                    "mimeType": MT_PDF,
                    "modifiedTime": "2024-02-01T00:00:00Z",
                },
            ],
        }

    service.files.return_value.list.return_value.execute = mock_execute

    result = client.list_folder("root_folder")
    assert len(result) == 2
    names = {f["name"] for f in result}
    assert names == {"root.pdf", "sub.pdf"}


def test_upload():
    """Test uploading a file to GDrive."""
    client, service = _make_client()

    service.files.return_value.create.return_value.execute.return_value = {
        "id": "new_file_id",
        "name": "test.pdf",
        "modifiedTime": "2026-03-01T00:00:00Z",
    }

    result = client.upload("test.pdf", b"pdf-content", "application/pdf", "folder123")
    assert result["id"] == "new_file_id"
    service.files.return_value.create.assert_called_once()


def test_update():
    """Test updating an existing file on GDrive."""
    client, service = _make_client()

    service.files.return_value.update.return_value.execute.return_value = {
        "id": "existing_id",
        "name": "test.pdf",
        "modifiedTime": "2026-03-01T00:00:00Z",
    }

    result = client.update("existing_id", b"new-content", "application/pdf")
    assert result["id"] == "existing_id"
    service.files.return_value.update.assert_called_once()


def test_create_gdrive_client_no_credentials():
    """Returns None when no credentials are set."""
    with (
        patch("oncofiles.gdrive_client.GOOGLE_CREDENTIALS_BASE64", ""),
        patch(
            "oncofiles.gdrive_client.GOOGLE_APPLICATION_CREDENTIALS",
            "",
        ),
    ):
        assert create_gdrive_client() is None


def test_gdrive_client_requires_credentials():
    """Raises ValueError when no credential source is provided."""
    with (
        patch("google.oauth2.service_account.Credentials"),
        patch("googleapiclient.discovery.build"),
    ):
        from oncofiles.gdrive_client import GDriveClient

        with pytest.raises(
            ValueError,
            match="Either credentials_base64 or credentials_path",
        ):
            GDriveClient()


# ── Retry decorator ─────────────────────────────────────────────────────


def test_retry_on_transient_retries_429(monkeypatch):
    """Retry decorator retries on 429 status code."""
    from oncofiles.gdrive_client import _retry_on_transient

    monkeypatch.setattr("oncofiles.gdrive_client._INITIAL_BACKOFF", 0.01)

    call_count = 0

    @_retry_on_transient
    def flaky_func():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            exc = Exception("rate limited")
            exc.resp = MagicMock(status=429)
            raise exc
        return "success"

    result = flaky_func()
    assert result == "success"
    assert call_count == 3


def test_retry_on_transient_no_retry_on_404():
    """Retry decorator does not retry on non-transient errors."""
    from oncofiles.gdrive_client import _retry_on_transient

    @_retry_on_transient
    def not_found():
        exc = Exception("not found")
        exc.resp = MagicMock(status=404)
        raise exc

    with pytest.raises(Exception, match="not found"):
        not_found()


def test_retry_on_transient_exhausts_retries(monkeypatch):
    """Retry decorator raises after max retries."""
    from oncofiles.gdrive_client import _retry_on_transient

    monkeypatch.setattr("oncofiles.gdrive_client._INITIAL_BACKOFF", 0.01)

    @_retry_on_transient
    def always_fails():
        exc = Exception("server error")
        exc.resp = MagicMock(status=500)
        raise exc

    with pytest.raises(Exception, match="server error"):
        always_fails()


# ── SSL / connection error retry ───────────────────────────────────────


def test_retry_on_ssl_error(monkeypatch):
    """Retry decorator retries on SSL record layer failure."""
    import ssl

    from oncofiles.gdrive_client import _retry_on_transient

    monkeypatch.setattr("oncofiles.gdrive_client._INITIAL_BACKOFF", 0.01)

    call_count = 0

    @_retry_on_transient
    def ssl_flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ssl.SSLError(1, "[SSL] record layer failure (_ssl.c:2580)")
        return "recovered"

    result = ssl_flaky()
    assert result == "recovered"
    assert call_count == 2


def test_retry_on_connection_reset(monkeypatch):
    """Retry decorator retries on ConnectionResetError."""
    from oncofiles.gdrive_client import _retry_on_transient

    monkeypatch.setattr("oncofiles.gdrive_client._INITIAL_BACKOFF", 0.01)

    call_count = 0

    @_retry_on_transient
    def conn_flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionResetError("Connection reset by peer")
        return "recovered"

    result = conn_flaky()
    assert result == "recovered"
    assert call_count == 2


def test_retry_on_broken_pipe(monkeypatch):
    """Retry decorator retries on BrokenPipeError."""
    from oncofiles.gdrive_client import _retry_on_transient

    monkeypatch.setattr("oncofiles.gdrive_client._INITIAL_BACKOFF", 0.01)

    call_count = 0

    @_retry_on_transient
    def pipe_flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise BrokenPipeError("Broken pipe")
        return "recovered"

    result = pipe_flaky()
    assert result == "recovered"
    assert call_count == 2


def test_retry_does_not_catch_value_error():
    """Non-transient, non-SSL errors are not retried."""
    from oncofiles.gdrive_client import _retry_on_transient

    @_retry_on_transient
    def bad_input():
        raise ValueError("bad argument")

    with pytest.raises(ValueError, match="bad argument"):
        bad_input()


def test_retry_rebuilds_service_on_ssl(monkeypatch):
    """SSL error triggers _rebuild_service on the GDriveClient instance."""
    import ssl

    from oncofiles.gdrive_client import _retry_on_transient

    monkeypatch.setattr("oncofiles.gdrive_client._INITIAL_BACKOFF", 0.01)

    rebuild_called = False

    class FakeClient:
        def _rebuild_service(self):
            nonlocal rebuild_called
            rebuild_called = True

    client = FakeClient()
    call_count = 0

    @_retry_on_transient
    def method(self):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ssl.SSLError(1, "[SSL] record layer failure")
        return "ok"

    result = method(client)
    assert result == "ok"
    assert rebuild_called


def test_is_connection_error_detects_ssl_in_message():
    """_is_connection_error catches SSL-related error messages."""
    from oncofiles.gdrive_client import _is_connection_error

    assert _is_connection_error(Exception("[SSL] record layer failure (_ssl.c:2580)"))
    assert _is_connection_error(Exception("EOF occurred in violation of protocol"))
    assert _is_connection_error(ConnectionResetError("reset"))
    assert not _is_connection_error(ValueError("bad value"))
    assert not _is_connection_error(Exception("file not found"))
