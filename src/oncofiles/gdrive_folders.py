"""Google Drive folder structure management for organized sync."""

from __future__ import annotations

import logging

from oncofiles.models import DocumentCategory

logger = logging.getLogger(__name__)

# Category folders map to DocumentCategory values
CATEGORY_FOLDERS = [cat.value for cat in DocumentCategory]

# Additional metadata folders (not document categories)
METADATA_FOLDERS = ["conversations", "treatment", "research"]

ALL_FOLDERS = CATEGORY_FOLDERS + METADATA_FOLDERS


def ensure_folder_structure(gdrive, root_folder_id: str) -> dict[str, str]:
    """Create category + metadata folders under root. Returns {name: folder_id} map.

    Idempotent — finds existing folders or creates new ones.
    """
    folder_map: dict[str, str] = {}
    for name in ALL_FOLDERS:
        folder_id = find_or_create_folder(gdrive, name, root_folder_id)
        folder_map[name] = folder_id
        logger.debug("Folder '%s' → %s", name, folder_id)
    return folder_map


def ensure_year_month_folder(gdrive, category_folder_id: str, date_str: str) -> str:
    """Create or find a YYYY-MM subfolder under a category folder.

    Args:
        date_str: ISO date string like "2026-03-01" — uses first 7 chars for "2026-03".

    Returns:
        Folder ID of the year-month subfolder.
    """
    year_month = date_str[:7]  # "2026-03"
    return find_or_create_folder(gdrive, year_month, category_folder_id)


def find_or_create_folder(gdrive, name: str, parent_id: str) -> str:
    """Find an existing folder or create a new one. Returns folder ID."""
    existing = gdrive.find_folder(name, parent_id)
    if existing:
        return existing
    return gdrive.create_folder(name, parent_id)


def get_category_folder_path(
    category: str, document_date_iso: str | None
) -> tuple[str, str | None]:
    """Determine the category folder name and year-month subfolder name.

    Returns:
        (category_folder_name, year_month_name_or_none)
    """
    year_month = document_date_iso[:7] if document_date_iso else None
    return category, year_month
