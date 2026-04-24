"""Google Drive client for downloading medical documents."""

from __future__ import annotations

import base64
import functools
import io
import json
import logging
import ssl
import time

from oncofiles.config import GOOGLE_APPLICATION_CREDENTIALS, GOOGLE_CREDENTIALS_BASE64

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Retry status codes for transient GDrive API failures
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}
_MAX_RETRIES = 3
_INITIAL_BACKOFF = 1.0  # seconds

# SSL/connection errors that indicate a stale connection pool
_CONNECTION_ERRORS = (ssl.SSLError, ConnectionError, OSError, BrokenPipeError, TimeoutError)


def _is_connection_error(exc: Exception) -> bool:
    """Check if an exception is a retryable SSL/connection error."""
    if isinstance(exc, _CONNECTION_ERRORS):
        return True
    # httplib2 wraps SSL errors in ServerNotFoundError or similar
    msg = str(exc).lower()
    return any(
        s in msg for s in ("ssl", "record layer", "broken pipe", "connection reset", "eof occurred")
    )


def _retry_on_transient(func):
    """Retry decorator for transient Google API errors (429/5xx) and SSL errors."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(_MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Check HTTP status code errors (429, 5xx)
                status = getattr(getattr(e, "resp", None), "status", None)
                is_http_transient = status in _RETRYABLE_STATUS_CODES
                is_conn_error = _is_connection_error(e)

                if not is_http_transient and not is_conn_error:
                    raise

                last_exc = e
                backoff = _INITIAL_BACKOFF * (2**attempt)
                error_type = f"HTTP {status}" if is_http_transient else "connection"
                logger.warning(
                    "GDrive API %s failed (%s: %s), retry %d/%d in %.1fs",
                    func.__name__,
                    error_type,
                    str(e)[:120],
                    attempt + 1,
                    _MAX_RETRIES,
                    backoff,
                )

                # Reset connection pool on SSL/connection errors
                if is_conn_error and args and hasattr(args[0], "_rebuild_service"):
                    try:
                        args[0]._rebuild_service()
                        logger.info("GDrive: rebuilt service after %s error", error_type)
                    except Exception:
                        logger.warning("GDrive: failed to rebuild service", exc_info=True)

                time.sleep(backoff)
        raise last_exc  # type: ignore[misc]

    return wrapper


class GDriveClient:
    """Download/upload files from Google Drive using service account or OAuth."""

    def __init__(
        self,
        credentials_base64: str = "",
        credentials_path: str = "",
        owner_email: str | None = None,
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

        self._creds = creds
        self._service = build("drive", "v3", credentials=creds)
        self.owner_email = owner_email

    def _rebuild_service(self) -> None:
        """Rebuild the Drive service with a fresh HTTP connection pool.

        Called automatically by the retry decorator after SSL/connection errors
        to recover from stale httplib2 connections.
        """
        from googleapiclient.discovery import build

        # Close old httplib2 pool before creating a new one to prevent leak
        old = self._service
        if old and hasattr(old, "_http") and old._http:
            old._http.close()
        self._service = build("drive", "v3", credentials=self._creds)

    @classmethod
    def from_oauth(
        cls,
        access_token: str,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        token_expiry: str | None = None,
        owner_email: str | None = None,
    ) -> GDriveClient:
        """Create a GDriveClient from OAuth 2.0 user credentials."""
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        instance = cls.__new__(cls)
        instance._creds = creds
        instance._service = build("drive", "v3", credentials=creds)
        instance.owner_email = owner_email
        return instance

    def get_folder_owner(self, folder_id: str) -> str | None:
        """Get the email of the folder owner. Returns None if not detectable."""
        try:
            result = (
                self._service.files().get(fileId=folder_id, fields="owners(emailAddress)").execute()
            )
            owners = result.get("owners", [])
            if owners:
                return owners[0].get("emailAddress")
        except Exception as e:
            logger.warning("Could not detect folder owner: %s", e)
        return None

    def grant_access(self, file_id: str, email: str, role: str = "writer") -> None:
        """Grant access to a file or folder. Idempotent — skips if already shared."""
        try:
            self._service.permissions().create(
                fileId=file_id,
                body={"type": "user", "role": role, "emailAddress": email},
                sendNotificationEmail=False,
            ).execute()
            logger.debug("Granted %s access to %s on %s", role, email, file_id)
        except Exception as e:
            # 400 = already has access, skip silently
            if "already" in str(e).lower() or "400" in str(e):
                logger.debug("Already shared %s with %s", file_id, email)
            else:
                logger.warning("Failed to share %s with %s: %s", file_id, email, e)

    def _auto_share(self, file_id: str) -> None:
        """Share with owner_email if set (service account created files)."""
        if self.owner_email:
            self.grant_access(file_id, self.owner_email)

    def grant_access_recursive(
        self, folder_id: str, email: str, role: str = "writer", *, _depth: int = 0
    ) -> int:
        """Grant access to all files and subfolders recursively. Returns count."""
        if _depth > 10:
            logger.warning("grant_access_recursive: max depth exceeded at %s", folder_id)
            return 0
        count = 0
        page_token = None
        while True:
            response = (
                self._service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, mimeType)",
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute()
            )
            for item in response.get("files", []):
                self.grant_access(item["id"], email, role)
                count += 1
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    count += self.grant_access_recursive(item["id"], email, role, _depth=_depth + 1)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return count

    @_retry_on_transient
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

    @_retry_on_transient
    def upload(
        self,
        filename: str,
        content_bytes: bytes,
        mime_type: str = "application/octet-stream",
        folder_id: str | None = None,
        app_properties: dict | None = None,
    ) -> dict:
        """Upload a file to Google Drive. Returns file metadata dict with id, modifiedTime."""
        from googleapiclient.http import MediaInMemoryUpload

        file_metadata: dict = {"name": filename}
        if folder_id:
            file_metadata["parents"] = [folder_id]
        if app_properties:
            file_metadata["appProperties"] = app_properties

        media = MediaInMemoryUpload(content_bytes, mimetype=mime_type)
        result = (
            self._service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, name, modifiedTime, appProperties",
            )
            .execute()
        )
        logger.info("Uploaded %s to GDrive: %s", filename, result.get("id"))
        self._auto_share(result["id"])
        return result

    @_retry_on_transient
    def update(self, gdrive_id: str, content_bytes: bytes, mime_type: str) -> dict:
        """Update an existing file's content on Google Drive."""
        from googleapiclient.http import MediaInMemoryUpload

        media = MediaInMemoryUpload(content_bytes, mimetype=mime_type)
        result = (
            self._service.files()
            .update(
                fileId=gdrive_id,
                media_body=media,
                fields="id, name, modifiedTime",
            )
            .execute()
        )
        logger.info("Updated GDrive file %s", gdrive_id)
        return result

    @_retry_on_transient
    def has_changes_since(self, folder_id: str, since_iso: str) -> bool:
        """Quick check: are there any files modified since the given ISO timestamp?

        Only checks the root folder (non-recursive) for speed. Single API call.
        """
        try:
            response = (
                self._service.files()
                .list(
                    q=(
                        f"'{folder_id}' in parents and trashed = false"
                        f" and modifiedTime > '{since_iso}'"
                    ),
                    fields="files(id)",
                    pageSize=1,
                )
                .execute()
            )
            return len(response.get("files", [])) > 0
        except Exception:
            # On error, assume changes exist (fail open)
            return True

    def list_folder(self, folder_id: str, recursive: bool = True) -> list[dict]:
        """List all files in a Google Drive folder.

        Returns list of dicts with keys: id, name, mimeType, modifiedTime, appProperties, parents.
        """
        results: list[dict] = []
        self._list_folder_recursive(folder_id, results, recursive)
        return results

    def _list_folder_recursive(self, folder_id: str, results: list[dict], recursive: bool) -> None:
        page_token = None
        while True:
            response = (
                self._service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields=(
                        "nextPageToken, files(id, name, mimeType,"
                        " modifiedTime, appProperties, parents,"
                        " md5Checksum, size)"
                    ),
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

    @_retry_on_transient
    def list_folder_with_structure(self, folder_id: str) -> tuple[list[dict], dict[str, str]]:
        """List all files and build a folder_id → name map for category detection.

        Returns:
            (files, folder_map) where folder_map is {folder_id: folder_name}.
        """
        files: list[dict] = []
        folder_map: dict[str, str] = {}
        self._list_with_structure(folder_id, files, folder_map)
        return files, folder_map

    def _list_with_structure(
        self, folder_id: str, files: list[dict], folder_map: dict[str, str]
    ) -> None:
        page_token = None
        while True:
            response = (
                self._service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields=(
                        "nextPageToken, files(id, name, mimeType,"
                        " modifiedTime, appProperties, parents,"
                        " md5Checksum, size)"
                    ),
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute()
            )
            for item in response.get("files", []):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    folder_map[item["id"]] = item["name"]
                    self._list_with_structure(item["id"], files, folder_map)
                else:
                    files.append(item)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    @_retry_on_transient
    def create_folder(self, name: str, parent_id: str) -> str:
        """Create a folder on Google Drive. Returns the folder ID."""
        file_metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        result = self._service.files().create(body=file_metadata, fields="id").execute()
        logger.info("Created folder '%s' in %s: %s", name, parent_id, result["id"])
        self._auto_share(result["id"])
        return result["id"]

    @_retry_on_transient
    def list_root_folders(self) -> list[dict]:
        """List folders in My Drive root. Returns [{id, name}]."""
        folders: list[dict] = []
        page_token = None
        while True:
            response = (
                self._service.files()
                .list(
                    q=(
                        "'root' in parents "
                        "and mimeType = 'application/vnd.google-apps.folder' "
                        "and trashed = false"
                    ),
                    fields="nextPageToken, files(id, name)",
                    pageSize=100,
                    pageToken=page_token,
                    orderBy="name",
                )
                .execute()
            )
            folders.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return folders

    @_retry_on_transient
    def find_folder(self, name: str, parent_id: str) -> str | None:
        """Find a folder by name under a parent. Returns folder ID or None."""
        safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
        response = (
            self._service.files()
            .list(
                q=(
                    f"'{parent_id}' in parents and name = '{safe_name}' "
                    f"and mimeType = 'application/vnd.google-apps.folder' "
                    f"and trashed = false"
                ),
                fields="files(id)",
                pageSize=1,
            )
            .execute()
        )
        files = response.get("files", [])
        return files[0]["id"] if files else None

    @_retry_on_transient
    def find_all_folders(self, name: str, parent_id: str) -> list[str]:
        """Find ALL folders with given name under parent. Returns list of folder IDs."""
        safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
        response = (
            self._service.files()
            .list(
                q=(
                    f"'{parent_id}' in parents and name = '{safe_name}' "
                    f"and mimeType = 'application/vnd.google-apps.folder' "
                    f"and trashed = false"
                ),
                fields="files(id, createdTime)",
                orderBy="createdTime",
                pageSize=10,
            )
            .execute()
        )
        return [f["id"] for f in response.get("files", [])]

    @_retry_on_transient
    def set_app_properties(self, file_id: str, properties: dict) -> None:
        """Set appProperties on a file for metadata tracking."""
        self._service.files().update(
            fileId=file_id,
            body={"appProperties": properties},
        ).execute()

    @_retry_on_transient
    def get_file_parents(self, file_id: str) -> list[str]:
        """Get the parent folder IDs of a file."""
        result = self._service.files().get(fileId=file_id, fields="parents").execute()
        return result.get("parents", [])

    @_retry_on_transient
    def get_file_metadata(self, file_id: str, fields: str = "id,name,parents,trashed") -> dict:
        """Fetch metadata for a single file. Used by sync's external-vs-deleted classifier.

        Raises ``FileNotFoundError`` (translated from googleapiclient 404) if the
        file has been permanently deleted. Other HTTP errors bubble up.
        """
        from googleapiclient.errors import HttpError

        try:
            return self._service.files().get(fileId=file_id, fields=fields).execute()
        except HttpError as exc:
            if getattr(exc, "status_code", None) == 404 or getattr(exc.resp, "status", 0) == 404:
                raise FileNotFoundError(f"GDrive file {file_id} not found") from exc
            raise

    @_retry_on_transient
    def create_shortcut(
        self,
        target_file_id: str,
        name: str,
        parent_folder_id: str,
        app_properties: dict | None = None,
    ) -> dict:
        """Create a Google Drive shortcut (application/vnd.google-apps.shortcut)
        pointing at ``target_file_id``, placed inside ``parent_folder_id``.

        Used by #460 multi-event cloning — a single source document (e.g. a
        lifetime vaccination log) can be referenced from every YYYY-MM folder
        where it records an event, without duplicating the file bytes.

        Returns the new shortcut's file metadata. Auto-shared via the same
        fallback used by copy_file.
        """
        body: dict = {
            "name": name,
            "mimeType": "application/vnd.google-apps.shortcut",
            "parents": [parent_folder_id],
            "shortcutDetails": {"targetId": target_file_id},
        }
        if app_properties:
            body["appProperties"] = app_properties
        result = (
            self._service.files()
            .create(
                body=body,
                fields="id, name, shortcutDetails, parents, appProperties",
            )
            .execute()
        )
        logger.info(
            "Created GDrive shortcut %s → '%s' (target=%s, parent=%s)",
            result.get("id"),
            name,
            target_file_id,
            parent_folder_id,
        )
        self._auto_share(result["id"])
        return result

    @_retry_on_transient
    def copy_file(
        self,
        file_id: str,
        new_name: str,
        folder_id: str,
        app_properties: dict | None = None,
    ) -> dict:
        """Copy a file on Google Drive. Returns new file metadata dict."""
        body: dict = {"name": new_name, "parents": [folder_id]}
        if app_properties:
            body["appProperties"] = app_properties
        result = (
            self._service.files()
            .copy(
                fileId=file_id,
                body=body,
                fields="id, name, modifiedTime, md5Checksum, size, appProperties",
            )
            .execute()
        )
        logger.info("Copied GDrive file %s → %s (%s)", file_id, result.get("id"), new_name)
        self._auto_share(result["id"])
        return result

    @_retry_on_transient
    def rename_file(self, file_id: str, new_name: str) -> None:
        """Rename a file or folder on Google Drive."""
        self._service.files().update(
            fileId=file_id,
            body={"name": new_name},
            fields="id, name",
        ).execute()
        logger.info("Renamed GDrive file %s → '%s'", file_id, new_name)

    @_retry_on_transient
    def trash_file(self, file_id: str) -> None:
        """Move a file to trash on Google Drive (soft delete)."""
        self._service.files().update(
            fileId=file_id,
            body={"trashed": True},
            fields="id, name",
        ).execute()
        logger.info("Trashed GDrive file %s", file_id)

    @_retry_on_transient
    def move_file(self, file_id: str, new_parent_id: str) -> None:
        """Move a file to a new parent folder."""
        # Get current parents
        file_info = self._service.files().get(fileId=file_id, fields="parents").execute()
        previous_parents = ",".join(file_info.get("parents", []))
        self._service.files().update(
            fileId=file_id,
            addParents=new_parent_id,
            removeParents=previous_parents,
            fields="id, parents",
        ).execute()

    @_retry_on_transient
    def export_google_doc(self, file_id: str, mime_type: str = "application/pdf") -> bytes:
        """Export a Google Docs/Sheets/Slides file to a downloadable format.

        Args:
            file_id: Google Drive file ID of the Google Docs file.
            mime_type: Target MIME type (default: application/pdf).

        Returns the exported file content as bytes.
        """
        request = self._service.files().export_media(fileId=file_id, mimeType=mime_type)
        buf = io.BytesIO()
        from googleapiclient.http import MediaIoBaseDownload

        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()

    # ── Batch operations ───────────────────────────────────────────────────

    @_retry_on_transient
    def batch_get_parents(self, file_ids: list[str]) -> dict[str, list[str]]:
        """Get parent folder IDs for multiple files in batched requests.

        Returns {file_id: [parent_ids]} mapping.
        Uses batches of 100 (Google API limit).
        """
        results: dict[str, list[str]] = {}
        if not file_ids:
            return results

        for chunk_start in range(0, len(file_ids), 100):
            chunk = file_ids[chunk_start : chunk_start + 100]
            batch = self._service.new_batch_http_request()
            for fid in chunk:

                def _callback(request_id, response, exception, _fid=fid):
                    if exception:
                        logger.warning("batch_get_parents: error for %s: %s", _fid, exception)
                        results[_fid] = []
                    else:
                        results[_fid] = response.get("parents", [])

                batch.add(
                    self._service.files().get(fileId=fid, fields="parents"),
                    callback=_callback,
                )
            batch.execute()

        return results

    @_retry_on_transient
    def batch_rename(self, renames: dict[str, str]) -> dict[str, bool]:
        """Rename multiple files in batched requests.

        Args:
            renames: {file_id: new_name} mapping.

        Returns {file_id: success} mapping.
        """
        results: dict[str, bool] = {}
        if not renames:
            return results

        items = list(renames.items())
        for chunk_start in range(0, len(items), 100):
            chunk = items[chunk_start : chunk_start + 100]
            batch = self._service.new_batch_http_request()
            for fid, new_name in chunk:

                def _callback(request_id, response, exception, _fid=fid):
                    if exception:
                        logger.warning("batch_rename: error for %s: %s", _fid, exception)
                        results[_fid] = False
                    else:
                        results[_fid] = True

                batch.add(
                    self._service.files().update(
                        fileId=fid, body={"name": new_name}, fields="id, name"
                    ),
                    callback=_callback,
                )
            batch.execute()

        return results

    @_retry_on_transient
    def batch_move(self, moves: dict[str, tuple[str, str]]) -> dict[str, bool]:
        """Move multiple files to new parent folders in batched requests.

        Args:
            moves: {file_id: (new_parent_id, old_parents_csv)} mapping.

        Returns {file_id: success} mapping.
        """
        results: dict[str, bool] = {}
        if not moves:
            return results

        items = list(moves.items())
        for chunk_start in range(0, len(items), 100):
            chunk = items[chunk_start : chunk_start + 100]
            batch = self._service.new_batch_http_request()
            for fid, (new_parent, old_parents) in chunk:

                def _callback(request_id, response, exception, _fid=fid):
                    if exception:
                        logger.warning("batch_move: error for %s: %s", _fid, exception)
                        results[_fid] = False
                    else:
                        results[_fid] = True

                batch.add(
                    self._service.files().update(
                        fileId=fid,
                        addParents=new_parent,
                        removeParents=old_parents,
                        fields="id, parents",
                    ),
                    callback=_callback,
                )
            batch.execute()

        return results


def create_gdrive_client(owner_email: str = "") -> GDriveClient | None:
    """Create a GDriveClient if credentials are available, else return None."""
    if GOOGLE_CREDENTIALS_BASE64:
        logger.info("Initializing GDrive client from base64 credentials")
        return GDriveClient(
            credentials_base64=GOOGLE_CREDENTIALS_BASE64,
            owner_email=owner_email or None,
        )
    if GOOGLE_APPLICATION_CREDENTIALS:
        logger.info("Initializing GDrive client from file: %s", GOOGLE_APPLICATION_CREDENTIALS)
        return GDriveClient(
            credentials_path=GOOGLE_APPLICATION_CREDENTIALS,
            owner_email=owner_email or None,
        )
    logger.info("No GDrive credentials found — GDrive fallback disabled")
    return None
