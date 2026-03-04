"""Google Drive client for downloading medical documents."""

from __future__ import annotations

import base64
import io
import json
import logging

from erika_files_mcp.config import GOOGLE_APPLICATION_CREDENTIALS, GOOGLE_CREDENTIALS_BASE64

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


class GDriveClient:
    """Download files from Google Drive using a service account."""

    def __init__(
        self,
        credentials_base64: str = "",
        credentials_path: str = "",
    ) -> None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        if credentials_base64:
            info = json.loads(base64.b64decode(credentials_base64))
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        elif credentials_path:
            creds = service_account.Credentials.from_service_account_file(
                credentials_path, scopes=SCOPES
            )
        else:
            raise ValueError("Either credentials_base64 or credentials_path must be provided")

        self._service = build("drive", "v3", credentials=creds)

    def download(self, gdrive_id: str) -> bytes:
        """Download a file's content by its Google Drive file ID."""
        from googleapiclient.http import MediaIoBaseDownload

        request = self._service.files().get_media(fileId=gdrive_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)

        done = False
        while not done:
            _, done = downloader.next_chunk()

        return buf.getvalue()

    def list_folder(self, folder_id: str, recursive: bool = True) -> list[dict]:
        """List all files in a Google Drive folder.

        Returns list of dicts with keys: id, name, mimeType, modifiedTime.
        """
        results: list[dict] = []
        self._list_folder_recursive(folder_id, results, recursive)
        return results

    def _list_folder_recursive(
        self, folder_id: str, results: list[dict], recursive: bool
    ) -> None:
        page_token = None
        while True:
            response = (
                self._service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute()
            )
            for item in response.get("files", []):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    if recursive:
                        self._list_folder_recursive(item["id"], results, recursive)
                else:
                    results.append(item)

            page_token = response.get("nextPageToken")
            if not page_token:
                break


def create_gdrive_client() -> GDriveClient | None:
    """Create a GDriveClient if credentials are available, else return None."""
    if GOOGLE_CREDENTIALS_BASE64:
        val = GOOGLE_CREDENTIALS_BASE64
        logger.info(
            "Initializing GDrive client from base64 credentials "
            "(len=%d, start=%s, end=%s)",
            len(val), val[:20], val[-20:],
        )
        # Debug: verify base64 decodes to valid JSON with private_key
        try:
            import base64 as _b64
            raw = _b64.b64decode(val)
            parsed = json.loads(raw)
            pk = parsed.get("private_key", "")
            logger.info(
                "Decoded JSON OK: private_key len=%d, starts=%s",
                len(pk), repr(pk[:40]),
            )
        except Exception as e:
            logger.error("Base64 decode/parse failed: %s", e)
        return GDriveClient(credentials_base64=GOOGLE_CREDENTIALS_BASE64)
    if GOOGLE_APPLICATION_CREDENTIALS:
        logger.info("Initializing GDrive client from file: %s", GOOGLE_APPLICATION_CREDENTIALS)
        return GDriveClient(credentials_path=GOOGLE_APPLICATION_CREDENTIALS)
    logger.info("No GDrive credentials found — GDrive fallback disabled")
    return None
