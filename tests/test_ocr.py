"""Tests for the OCR module."""

from unittest.mock import MagicMock, patch

from fastmcp.utilities.types import Image

from oncofiles.ocr import OCR_MODEL, OCR_SYSTEM_PROMPT, extract_text_from_image


def test_extract_text_from_image():
    """Test that extract_text_from_image calls Claude API correctly."""
    fake_image = Image(data=b"fake-jpeg-bytes", format="jpeg")

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="Hemoglobín: 135 g/L\nLeukocyty: 5.2")]

    with patch("oncofiles.ocr.anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_response

        result = extract_text_from_image(fake_image)

    assert result == "Hemoglobín: 135 g/L\nLeukocyty: 5.2"

    # Verify API call
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == OCR_MODEL
    assert call_kwargs["system"] == OCR_SYSTEM_PROMPT
    assert call_kwargs["max_tokens"] == 4096

    # Verify image was sent as base64
    content = call_kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["type"] == "base64"
    assert content[0]["source"]["media_type"] == "image/jpeg"


def test_extract_text_empty_response():
    """Test handling of empty response from Claude."""
    fake_image = Image(data=b"empty-page", format="png")

    mock_response = MagicMock()
    mock_response.content = []

    with patch("oncofiles.ocr.anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_response

        result = extract_text_from_image(fake_image)

    assert result == ""


def test_extract_text_png_format():
    """Test that PNG images use correct media type."""
    fake_image = Image(data=b"fake-png-bytes", format="png")

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="Test text")]

    with patch("oncofiles.ocr.anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_response

        extract_text_from_image(fake_image)

    content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    # Image._mime_type is set by fastmcp Image class from the format parameter
    assert content[0]["source"]["media_type"] == "image/png"
