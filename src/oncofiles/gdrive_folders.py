"""Google Drive folder structure management for organized sync."""

from __future__ import annotations

import logging
import re

from oncofiles.models import DocumentCategory

logger = logging.getLogger(__name__)

_YM_RE = re.compile(r"^\d{4}-\d{2}$")

# Legacy folder names that should be merged into active categories.
# Used by housekeeping to detect and merge obsolete GDrive folders.
CATEGORY_MERGES: dict[str, str] = {
    "surgical_report": "surgery",
    "discharge_summary": "discharge",
}

# Active category folders (excludes legacy aliases like surgical_report)
CATEGORY_FOLDERS = [cat.value for cat in DocumentCategory if cat.value not in CATEGORY_MERGES]

# Additional metadata folders (not document categories)
METADATA_FOLDERS = ["conversations", "treatment", "research"]

ALL_FOLDERS = CATEGORY_FOLDERS + METADATA_FOLDERS

# Patient-type-aware folder filtering
ONCOLOGY_ONLY_CATEGORIES = {"chemo_sheet", "pathology", "genetics", "hereditary_genetics"}
GENERAL_ONLY_CATEGORIES = {"vaccination", "dental", "preventive"}

# Bilingual display names: EN key → SK translation (for GDrive folder display)
FOLDER_SK: dict[str, str] = {
    "labs": "laboratórne výsledky",
    "report": "lekárske správy",
    "pathology": "patológia",
    "imaging": "zobrazovanie",
    "surgery": "operácie",
    "consultation": "konzultácie",
    "prescription": "recepty",
    "referral": "odporúčania",
    "discharge": "prepúšťacie správy",
    "chemo_sheet": "chemoterapeutické protokoly",
    "genetics": "genetické vyšetrenia",
    "hereditary_genetics": "dedičná genetika",
    "reference": "referenčné materiály",
    "advocate": "záznamy advokáta pacienta",
    "other": "ostatné",
    "vaccination": "očkovanie",
    "dental": "zubné záznamy",
    "preventive": "preventívne prehliadky",
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
    # Legacy categories (surgical_report, discharge_summary) are not in ALL_FOLDERS
    # but must be recognized so the cleanup job can find and merge them.
    if candidate in CATEGORY_MERGES:
        return candidate
    return None


def _folders_for_patient_type(patient_type: str = "oncology") -> list[str]:
    """Return the list of folders appropriate for a patient type."""
    if patient_type == "general":
        return [f for f in ALL_FOLDERS if f not in ONCOLOGY_ONLY_CATEGORIES]
    # oncology: skip general-only categories
    return [f for f in ALL_FOLDERS if f not in GENERAL_ONLY_CATEGORIES]


def ensure_folder_structure(
    gdrive, root_folder_id: str, *, patient_type: str = "oncology"
) -> dict[str, str]:
    """Create category + metadata folders under root. Returns {en_key: folder_id} map.

    Uses bilingual display names. Finds existing folders by either old (EN-only)
    or new (bilingual) name, renames old folders to bilingual format.
    Idempotent.

    Args:
        patient_type: "oncology" (default) or "general" — controls which folders
            are created. General patients skip chemo_sheet/pathology/genetics;
            oncology patients skip vaccination/dental/preventive.
    """
    folders = _folders_for_patient_type(patient_type)
    folder_map: dict[str, str] = {}
    for name in folders:
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


def resolve_category_folder(folder_map: dict[str, str], cat_name: str, root_folder_id: str) -> str:
    """Resolve a category name to its folder ID, falling back to 'other' — never root.

    When a category is missing from folder_map (e.g., legacy merged categories like
    'surgical_report', or patient-type-excluded categories like 'vaccination' for
    oncology patients), falls back to the 'other' folder instead of root_folder_id.
    This prevents year-month subfolders from being created directly under root (#273).
    """
    folder_id = folder_map.get(cat_name)
    if folder_id:
        return folder_id
    # Fall back to 'other' category folder — NEVER root
    other_id = folder_map.get("other")
    if other_id:
        logger.warning(
            "Category '%s' not in folder_map — falling back to 'other' folder (#273)",
            cat_name,
        )
        return other_id
    # Last resort: root (should never happen — 'other' is always created)
    logger.error(
        "Category '%s' and 'other' both missing from folder_map — using root (#273)",
        cat_name,
    )
    return root_folder_id


def _looks_like_year_month_folder(gdrive, folder_id: str) -> bool:
    """Best-effort check: is this folder_id actually a YYYY-MM folder?

    Returns True iff GDrive answers with a name matching ``YYYY-MM``. Any
    GDrive error is swallowed and returns False — this check must never
    break the sync hot path. Uses a per-process cache to avoid an extra
    API round-trip on every ``ensure_year_month_folder`` call.
    """
    cache: dict[str, bool] = _looks_like_year_month_folder._cache  # type: ignore[attr-defined]
    if folder_id in cache:
        return cache[folder_id]
    try:
        meta = (
            gdrive._service.files().get(fileId=folder_id, fields="name").execute()  # type: ignore[attr-defined]
        )
        name = meta.get("name", "")
        result = bool(_YM_RE.match(name))
    except Exception:
        return False
    cache[folder_id] = result
    if len(cache) > 512:
        # Bound cache size — keep newest entries, drop earliest insertions.
        for k in list(cache.keys())[:256]:
            cache.pop(k, None)
    return result


_looks_like_year_month_folder._cache = {}  # type: ignore[attr-defined]


def ensure_year_month_folder(gdrive, category_folder_id: str, date_str: str) -> str:
    """Create or find a YYYY-MM subfolder under a category folder.

    Args:
        date_str: ISO date string like "2026-03-01" — uses first 7 chars for "2026-03".

    Returns:
        Folder ID of the year-month subfolder.
    """
    year_month = date_str[:7]  # "2026-03"
    # Validate: year_month must be YYYY-MM format (7 chars with dash at position 4)
    if len(year_month) < 7 or year_month[4:5] != "-":
        logger.warning(
            "ensure_year_month_folder: invalid date_str '%s' — expected YYYY-MM-DD, "
            "got year_month='%s'. Skipping folder creation to prevent stray folders (#273).",
            date_str,
            year_month,
        )
        return category_folder_id  # Return parent as-is rather than creating bad folder

    # Preventive: if the caller passed a YYYY-MM folder as the "category" parent,
    # do NOT create another YYYY-MM under it. Return the parent unchanged and log
    # an error so the upstream bug is visible. Caught every nested-YYYY-MM dup on
    # q1b in #457. The check is best-effort (cached GDrive lookup) and never
    # breaks the sync path if GDrive is unreachable.
    if _looks_like_year_month_folder(gdrive, category_folder_id):
        logger.error(
            "ensure_year_month_folder: refusing to nest YYYY-MM under YYYY-MM. "
            "category_folder_id=%s appears to itself be a year-month folder. "
            "Returning parent as-is — upstream caller passed the wrong parent (#457).",
            category_folder_id,
        )
        return category_folder_id

    return find_or_create_folder(gdrive, year_month, category_folder_id)


def find_or_create_folder(gdrive, name: str, parent_id: str) -> str:
    """Find an existing folder or create a new one. Returns folder ID.

    Handles race conditions: after creating, re-lists all matching folders
    and trashes any duplicates (keeps the oldest by createdTime).
    """
    existing = gdrive.find_folder(name, parent_id)
    if existing:
        return existing
    new_id = gdrive.create_folder(name, parent_id)
    # Race condition check: GDrive eventual consistency needs ~2s
    try:
        import time

        time.sleep(2)
        # List ALL matching folders (not just the first) via direct API query
        all_matching = (
            gdrive._service.files()
            .list(
                q=(
                    f"'{parent_id}' in parents"
                    f" and name = '{name}'"
                    f" and mimeType = 'application/vnd.google-apps.folder'"
                    f" and trashed = false"
                ),
                fields="files(id, createdTime)",
                orderBy="createdTime",
                pageSize=10,
            )
            .execute()
            .get("files", [])
        )
        if len(all_matching) > 1:
            keep = all_matching[0]["id"]  # oldest by createdTime
            for dup in all_matching[1:]:
                try:
                    gdrive.trash_file(dup["id"])
                    logger.warning(
                        "Trashed duplicate folder '%s' (%s), keeping %s", name, dup["id"], keep
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
