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
        # Debug: print to stdout (logger.info not visible on Railway)
        import google.auth as _ga
        try:
            import cryptography as _cry
            cry_ver = _cry.__version__
        except ImportError:
            cry_ver = "NOT INSTALLED"
        print(f"[GDRIVE DEBUG] google-auth={_ga.__version__}, cryptography={cry_ver}", flush=True)
        print(f"[GDRIVE DEBUG] base64 len={len(val)}, start={val[:20]}, end={val[-20:]}", flush=True)
        try:
            import base64 as _b64
            import hashlib
            import ssl
            raw = _b64.b64decode(val)
            parsed = json.loads(raw)
            pk = parsed.get("private_key", "")
            pk_hash = hashlib.sha256(pk.encode()).hexdigest()[:16]
            pk_bytes = pk.encode("utf-8")
            print(f"[GDRIVE DEBUG] OpenSSL: {ssl.OPENSSL_VERSION}", flush=True)
            print(f"[GDRIVE DEBUG] private_key len={len(pk)}, sha256={pk_hash}", flush=True)
            print(f"[GDRIVE DEBUG] pk bytes[:50] hex={pk_bytes[:50].hex()}", flush=True)
            print(f"[GDRIVE DEBUG] pk bytes[-50:] hex={pk_bytes[-50:].hex()}", flush=True)
            # Try loading PEM directly
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            try:
                load_pem_private_key(pk_bytes, password=None)
                print("[GDRIVE DEBUG] load_pem_private_key SUCCEEDED", flush=True)
            except Exception as e2:
                print(f"[GDRIVE DEBUG] load_pem_private_key FAILED: {type(e2).__name__}: {e2}", flush=True)
        except Exception as e:
            print(f"[GDRIVE DEBUG] Base64 decode/parse FAILED: {e}", flush=True)
        return GDriveClient(credentials_base64=GOOGLE_CREDENTIALS_BASE64)
    if GOOGLE_APPLICATION_CREDENTIALS:
        logger.info("Initializing GDrive client from file: %s", GOOGLE_APPLICATION_CREDENTIALS)
        return GDriveClient(credentials_path=GOOGLE_APPLICATION_CREDENTIALS)
    logger.info("No GDrive credentials found — GDrive fallback disabled")
    return None
