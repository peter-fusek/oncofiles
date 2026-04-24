"""Regression tests for FilesClient filename sanitization (#477)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from oncofiles.files_api import FilesClient, _sanitize_filename


def test_sanitize_strips_forbidden_chars():
    """Forbidden Anthropic Files API chars are replaced with underscore."""
    assert _sanitize_filename("_ POURICOVANIE FOL FAX?.png") == "_ POURICOVANIE FOL FAX_.png"
    assert _sanitize_filename("Scanned 30 Mar 2026 at 11:27:28.pdf") == (
        "Scanned 30 Mar 2026 at 11_27_28.pdf"
    )
    assert _sanitize_filename('weird<>:"/\\|?*name.txt') == "weird_________name.txt"


def test_sanitize_passthrough_for_clean_names():
    assert _sanitize_filename("20260418_ErikaFusekova_NOU_Labs_PreCycle1.pdf") == (
        "20260418_ErikaFusekova_NOU_Labs_PreCycle1.pdf"
    )


def test_sanitize_empty_after_strip_falls_back():
    assert _sanitize_filename("") == "file"
    assert _sanitize_filename("   ") == "file"


def test_upload_passes_sanitized_filename_to_anthropic():
    """The Anthropic SDK call must receive the sanitized name, not the raw input."""
    with patch("oncofiles.files_api.anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        fc = FilesClient(api_key="test")

        fc.upload(io.BytesIO(b"x"), "_ POURICOVANIE FOL FAX?.png", "image/png")

        upload_kwargs = mock_client.beta.files.upload.call_args.kwargs
        sent_filename, _, sent_mime = upload_kwargs["file"]
        assert "?" not in sent_filename
        assert sent_filename == "_ POURICOVANIE FOL FAX_.png"
        assert sent_mime == "image/png"
