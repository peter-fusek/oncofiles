"""Regression tests for FilesClient filename sanitization (#477, #508)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from oncofiles.files_api import FilesClient, _sanitize_filename, sanitize_filename


def test_sanitize_strips_forbidden_chars():
    """Forbidden Anthropic Files API chars are replaced with underscore."""
    assert sanitize_filename("_ POURICOVANIE FOL FAX?.png") == "_ POURICOVANIE FOL FAX_.png"
    assert sanitize_filename("Scanned 30 Mar 2026 at 11:27:28.pdf") == (
        "Scanned 30 Mar 2026 at 11_27_28.pdf"
    )
    # #508: an embedded / triggers basename extraction, so we keep just the
    # part after the last separator and then sub the remaining forbidden chars.
    assert sanitize_filename('weird<>:"/\\|?*name.txt') == "___name.txt"


def test_sanitize_passthrough_for_clean_names():
    assert sanitize_filename("20260418_ErikaFusekova_NOU_Labs_PreCycle1.pdf") == (
        "20260418_ErikaFusekova_NOU_Labs_PreCycle1.pdf"
    )


def test_sanitize_empty_after_strip_falls_back():
    assert sanitize_filename("") == "file"
    assert sanitize_filename("   ") == "file"


# ── #508: path-traversal hardening ────────────────────────────────────────


def test_sanitize_strips_posix_path_traversal():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("../foo.pdf") == "foo.pdf"
    assert sanitize_filename("/absolute/path/foo.pdf") == "foo.pdf"


def test_sanitize_strips_windows_path_traversal():
    assert sanitize_filename("..\\..\\windows\\system32\\foo.pdf") == "foo.pdf"
    assert sanitize_filename("C:\\Users\\victim\\foo.pdf") == "foo.pdf"


def test_sanitize_strips_leading_dots():
    """Leading dots can produce hidden files in any future POSIX exporter."""
    assert sanitize_filename(".hidden.pdf") == "hidden.pdf"
    assert sanitize_filename("....pdf") == "pdf"
    assert sanitize_filename("..") == "file"
    assert sanitize_filename(".") == "file"


def test_sanitize_strips_control_chars():
    assert sanitize_filename("foo\x00bar.pdf") == "foo_bar.pdf"
    assert sanitize_filename("foo\x1fbar.pdf") == "foo_bar.pdf"
    assert sanitize_filename("foo\x7fbar.pdf") == "foo_bar.pdf"


def test_sanitize_underscore_alias_still_works():
    """Existing imports of `_sanitize_filename` continue to function."""
    assert _sanitize_filename("../foo.pdf") == "foo.pdf"


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
