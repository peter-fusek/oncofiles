"""Shared test helpers."""

from datetime import date

from erika_files_mcp.models import Document, DocumentCategory


def make_doc(**overrides) -> Document:
    defaults = {
        "file_id": "file_test123",
        "filename": "20240115_NOUonko_labs_krvnyObraz.pdf",
        "original_filename": "20240115_NOUonko_labs_krvnyObraz.pdf",
        "document_date": date(2024, 1, 15),
        "institution": "NOUonko",
        "category": DocumentCategory.LABS,
        "description": "krvnyObraz",
        "mime_type": "application/pdf",
        "size_bytes": 1024,
    }
    defaults.update(overrides)
    return Document(**defaults)
