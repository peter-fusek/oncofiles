"""Anthropic Files API client for persistent document storage."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import BinaryIO

import anthropic

from erika_files_mcp.config import ANTHROPIC_API_KEY


class FilesClient:
    """Wrapper around the Anthropic Files API (beta)."""

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.Anthropic(api_key=api_key or ANTHROPIC_API_KEY)

    def upload(
        self,
        file: BinaryIO,
        filename: str,
        mime_type: str | None = None,
    ) -> anthropic.types.beta.FileMetadata:
        """Upload a file and return its metadata."""
        if mime_type is None:
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return self._client.beta.files.upload(file=(filename, file, mime_type))

    def upload_path(self, path: Path | str) -> anthropic.types.beta.FileMetadata:
        """Upload a file from a local path."""
        path = Path(path)
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            return self.upload(f, path.name, mime_type)

    def list(self, limit: int = 100) -> list[anthropic.types.beta.FileMetadata]:
        """List all uploaded files."""
        page = self._client.beta.files.list(limit=limit)
        return list(page.data)

    def get(self, file_id: str) -> anthropic.types.beta.FileMetadata:
        """Get metadata for a specific file."""
        return self._client.beta.files.retrieve_metadata(file_id)

    def delete(self, file_id: str) -> bool:
        """Delete a file. Returns True if successfully deleted."""
        result = self._client.beta.files.delete(file_id)
        return result.type == "file_deleted"
