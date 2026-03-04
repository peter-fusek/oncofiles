"""Tests for GDriveClient."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from erika_files_mcp.gdrive_client import create_gdrive_client

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

        from erika_files_mcp.gdrive_client import GDriveClient

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


def test_create_gdrive_client_no_credentials():
    """Returns None when no credentials are set."""
    with (
        patch("erika_files_mcp.gdrive_client.GOOGLE_CREDENTIALS_BASE64", ""),
        patch(
            "erika_files_mcp.gdrive_client.GOOGLE_APPLICATION_CREDENTIALS",
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
        from erika_files_mcp.gdrive_client import GDriveClient

        with pytest.raises(
            ValueError,
            match="Either credentials_base64 or credentials_path",
        ):
            GDriveClient()
