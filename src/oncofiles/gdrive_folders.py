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

# Bilingual display names: EN key → SK translation (for GDrive folder display)
FOLDER_SK: dict[str, str] = {
    "labs": "laboratórne výsledky",
    "report": "lekárske správy",
    "pathology": "patológia",
    "imaging": "zobrazovanie",
    "surgery": "operácie",
    "surgical_report": "operačné protokoly",
    "prescription": "recepty",
    "referral": "odporúčania",
    "discharge": "prepúšťacie správy",
    "discharge_summary": "epikrízy",
    "chemo_sheet": "chemoterapeutické protokoly",
    "genetics": "genetické vyšetrenia",
    "reference": "referenčné materiály",
    "advocate": "záznamy advokáta pacienta",
    "other": "ostatné",
    "conversations": "záznamy rozhovorov",
    "treatment": "priebeh liečby",
    "research": "výskum",
}


def bilingual_name(en_key: str) -> str:
    """Return bilingual folder display name: 'en_key — SK translation'."""
    sk = FOLDER_SK.get(en_key)
    if sk:
        return f"{en_key} — {sk}"
    return en_key


def en_key_from_folder_name(folder_name: str) -> str | None:
    """Extract the EN category key from a folder name (bilingual or legacy).

    Handles both 'labs — laboratórne výsledky' and plain 'labs'.
    Returns None if not a known category/metadata folder.
    """
    # Bilingual format: take everything before ' — '
    candidate = folder_name.split(" — ", 1)[0] if " — " in folder_name else folder_name
    if candidate in ALL_FOLDERS:
        return candidate
    return None


def ensure_folder_structure(gdrive, root_folder_id: str) -> dict[str, str]:
    """Create category + metadata folders under root. Returns {en_key: folder_id} map.

    Uses bilingual display names. Finds existing folders by either old (EN-only)
    or new (bilingual) name, renames old folders to bilingual format.
    Idempotent.
    """
    folder_map: dict[str, str] = {}
    for name in ALL_FOLDERS:
        display = bilingual_name(name)
        folder_id = _find_or_create_bilingual(gdrive, name, display, root_folder_id)
        folder_map[name] = folder_id
        logger.debug("Folder '%s' → %s", display, folder_id)
    return folder_map


def _find_or_create_bilingual(gdrive, en_key: str, bilingual: str, parent_id: str) -> str:
    """Find by bilingual name, fall back to old EN name (and rename), or create new."""
    # Try bilingual name first
    existing = gdrive.find_folder(bilingual, parent_id)
    if existing:
        return existing

    # Try old EN-only name and rename it
    old = gdrive.find_folder(en_key, parent_id)
    if old:
        gdrive.rename_file(old, bilingual)
        logger.info("Renamed folder '%s' → '%s' (%s)", en_key, bilingual, old)
        return old

    # Create new with bilingual name
    return gdrive.create_folder(bilingual, parent_id)


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
    """Find an existing folder or create a new one. Returns folder ID.

    Handles race conditions: if a duplicate is created concurrently,
    detects it and returns the first one found.
    """
    existing = gdrive.find_folder(name, parent_id)
    if existing:
        return existing
    new_id = gdrive.create_folder(name, parent_id)
    # Race condition check: see if another folder was created concurrently
    # by listing all matching folders
    try:
        import time

        time.sleep(0.5)  # brief delay for eventual consistency
        all_matching = gdrive.find_all_folders(name, parent_id)
        if len(all_matching) > 1:
            # Keep the first (oldest), trash duplicates
            keep = all_matching[0]
            for dup_id in all_matching[1:]:
                try:
                    gdrive.trash_file(dup_id)
                    logger.warning(
                        "Trashed duplicate folder '%s' (%s), keeping %s", name, dup_id, keep
                    )
                except Exception:
                    pass
            return keep
    except Exception:
        pass
    return new_id


def get_category_folder_path(
    category: str, document_date_iso: str | None
) -> tuple[str, str | None]:
    """Determine the category folder name and year-month subfolder name.

    Returns:
        (category_folder_name, year_month_name_or_none)
    """
    year_month = document_date_iso[:7] if document_date_iso else None
    return category, year_month
