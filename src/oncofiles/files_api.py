"""Anthropic Files API client for persistent document storage."""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import BinaryIO

import anthropic

from oncofiles.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

# Anthropic Files API rejects filenames with characters like ?, :, *, <, >, |, ".
# iOS Notes ("Scanned 30 Mar 2026 at 11:27:28.pdf") and SK fax scanner outputs
# ("_ POURICOVANIE FOL FAX?.png") routinely produce such names. See #477.
_FORBIDDEN_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def sanitize_filename(filename: str) -> str:
    """Centralized filename sanitizer for the upload pipeline (#477, #508).

    Strips path components (so ``../../foo.pdf`` becomes ``foo.pdf``),
    forbidden characters (Anthropic Files API restrictions + control chars),
    and leading dots (so ``.hidden`` / ``....pdf`` cannot be persisted as
    hidden files by any downstream exporter that treats the value as a
    POSIX filename). Falls back to ``"file"`` when everything is stripped.

    Apply at every external-facing upload boundary BEFORE the value reaches
    the Files API, the database, GDrive, or the parser — never trust the
    caller-supplied filename downstream.
    """
    # Take basename only — both POSIX (/) and Windows (\) separators.
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    # Replace forbidden characters with underscore, strip surrounding ws.
    name = _FORBIDDEN_FILENAME_CHARS.sub("_", name).strip()
    # Strip leading dots so `..`, `....pdf`, `.hidden` collapse safely.
    name = name.lstrip(".")
    return name or "file"


# Backward-compat alias — older imports used the underscore-prefixed name.
_sanitize_filename = sanitize_filename


# ── Drive-only documents (#777) ────────────────────────────────────────────
# A document whose bytes live only in Google Drive carries this file_id
# prefix instead of an Anthropic Files API id. Curated documents (#603) were
# the first; since #777 the Drive sync import and ``upload_document`` store
# Drive-only too whenever the Files API may not be used. Every read path
# (``_try_download``, view_document, OCR) fetches such a document from Drive.
# Must equal ``curated.indexer.CURATED_FILE_PREFIX`` (locked by a test).
DRIVE_ONLY_PREFIX = "gdrive:"

# structured_metadata marker of a Drive-only row written by the legacy sync
# or upload path, so the curated indexer never mistakes it for its own row.
DRIVE_SYNC_SOURCE = "drive_sync"


def drive_only_file_id(gdrive_id: str) -> str:
    return DRIVE_ONLY_PREFIX + gdrive_id


def is_drive_only_file_id(file_id: str | None) -> bool:
    return bool(file_id) and file_id.startswith(DRIVE_ONLY_PREFIX)


def files_api_refusal_reason() -> str | None:
    """Why the Files API may not be used right now, or None when it may.

    The same predicate as ``enhance._get_client`` / ``FilesClient._guarded``:
    refused inside a ``local_only()`` job and whenever cloud AI is off
    (``CLOUD_AI_ENABLED`` off or no key, #618). Callers that would upload
    document bytes check this first and store the document Drive-only instead
    of calling the API and counting the refusal as a failure (#777).
    """
    from oncofiles.cloud_guard import cloud_ai_disabled_reason, local_only_job

    if (job := local_only_job()) is not None:
        return f"'{job}' runs on the local model only"
    return cloud_ai_disabled_reason()


class FilesClient:
    """Wrapper around the Anthropic Files API (beta)."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key  # explicit key; else config is read at call time (#618)
        self._client = anthropic.Anthropic(api_key=api_key or ANTHROPIC_API_KEY)

    def _guarded(self):
        """The Anthropic client, refused inside local-only jobs (policy 2026-11-09)
        and when the server has no ANTHROPIC_API_KEY (#618)."""
        from oncofiles.cloud_guard import check_cloud_allowed, require_cloud_ai

        check_cloud_allowed("Files API")
        require_cloud_ai(self._api_key)
        return self._client

    def close(self) -> None:
        """Close the underlying httpx client to release connection pool."""
        if self._client:
            self._client.close()

    def upload(
        self,
        file: BinaryIO,
        filename: str,
        mime_type: str | None = None,
    ) -> anthropic.types.beta.FileMetadata:
        """Upload a file and return its metadata."""
        sanitized = _sanitize_filename(filename)
        if sanitized != filename:
            logger.info("Sanitized filename for upload: %r -> %r", filename, sanitized)
        filename = sanitized
        if len(filename) > 255:
            _, _, ext = filename.rpartition(".")
            filename = filename[: 255 - len(ext) - 1] + "." + ext if ext else filename[:255]
            logger.warning("Truncated filename to 255 chars: %s...", filename[:50])
        if mime_type is None:
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return self._guarded().beta.files.upload(file=(filename, file, mime_type))

    def upload_path(self, path: Path | str) -> anthropic.types.beta.FileMetadata:
        """Upload a file from a local path."""
        path = Path(path)
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            return self.upload(f, path.name, mime_type)

    def list(self, limit: int = 100) -> list[anthropic.types.beta.FileMetadata]:
        """List all uploaded files."""
        page = self._guarded().beta.files.list(limit=limit)
        return list(page.data)

    def get(self, file_id: str) -> anthropic.types.beta.FileMetadata:
        """Get metadata for a specific file."""
        return self._guarded().beta.files.retrieve_metadata(file_id)

    def download(self, file_id: str) -> bytes:
        """Download file content as raw bytes."""
        response = self._guarded().beta.files.download(file_id)
        return response.read()

    def delete(self, file_id: str) -> bool:
        """Delete a file. Returns True if successfully deleted."""
        result = self._guarded().beta.files.delete(file_id)
        return result.type == "file_deleted"
