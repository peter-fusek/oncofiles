"""Tests for GDrive folder structure management."""

from __future__ import annotations

from unittest.mock import MagicMock

from oncofiles.gdrive_folders import (
    bilingual_name,
    en_key_from_folder_name,
    ensure_folder_structure,
    ensure_year_month_folder,
    find_or_create_folder,
    get_category_folder_path,
    resolve_category_folder,
)


def _mock_gdrive():
    """Create a mock GDriveClient for folder operations."""
    gdrive = MagicMock()
    # find_folder returns None by default (no existing folders)
    gdrive.find_folder.return_value = None
    # create_folder returns a fake ID
    gdrive.create_folder.side_effect = lambda name, parent: f"folder_{name}"
    gdrive.rename_file.return_value = None
    return gdrive


def test_ensure_folder_structure_creates_all():
    """Creates all category + metadata folders with bilingual names."""
    gdrive = _mock_gdrive()
    result = ensure_folder_structure(gdrive, "root123")

    # Result keys are EN keys
    assert "labs" in result
    assert "imaging" in result
    assert "conversations" in result
    assert "treatment" in result
    assert "research" in result
    assert len(result) >= 12  # 17 categories + 3 metadata

    # Folders are created with bilingual names
    created_names = [call[0][0] for call in gdrive.create_folder.call_args_list]
    assert "labs — laboratórne výsledky" in created_names
    assert "treatment — priebeh liečby" in created_names


def test_ensure_folder_structure_finds_existing():
    """Uses existing folders instead of creating new ones."""
    gdrive = _mock_gdrive()
    gdrive.find_folder.return_value = "existing_id"

    result = ensure_folder_structure(gdrive, "root123")

    # All folders should use the existing ID
    for folder_id in result.values():
        assert folder_id == "existing_id"
    gdrive.create_folder.assert_not_called()


def test_ensure_folder_structure_renames_old_folders():
    """Old EN-only folders get renamed to bilingual format."""
    gdrive = _mock_gdrive()
    # find_folder: bilingual name → None, old EN name → found
    gdrive.find_folder.side_effect = lambda name, parent: (
        "old_folder_id" if name == "labs" else None
    )
    result = ensure_folder_structure(gdrive, "root123")

    assert result["labs"] == "old_folder_id"
    gdrive.rename_file.assert_any_call("old_folder_id", "labs — laboratórne výsledky")


def test_ensure_year_month_folder():
    """Creates a YYYY-MM subfolder under a category folder."""
    gdrive = _mock_gdrive()
    folder_id = ensure_year_month_folder(gdrive, "cat_folder", "2026-03-01")

    gdrive.find_folder.assert_called_once_with("2026-03", "cat_folder")
    gdrive.create_folder.assert_called_once_with("2026-03", "cat_folder")
    assert folder_id == "folder_2026-03"


def test_ensure_year_month_folder_existing():
    """Finds existing year-month subfolder."""
    gdrive = _mock_gdrive()
    gdrive.find_folder.return_value = "existing_ym"

    folder_id = ensure_year_month_folder(gdrive, "cat_folder", "2026-03-15")
    assert folder_id == "existing_ym"
    gdrive.create_folder.assert_not_called()


def test_find_or_create_folder_creates():
    gdrive = _mock_gdrive()
    result = find_or_create_folder(gdrive, "test", "parent")
    assert result == "folder_test"


def test_find_or_create_folder_finds():
    gdrive = _mock_gdrive()
    gdrive.find_folder.return_value = "found_id"
    result = find_or_create_folder(gdrive, "test", "parent")
    assert result == "found_id"


def test_get_category_folder_path_with_date():
    cat, ym = get_category_folder_path("labs", "2026-03-01")
    assert cat == "labs"
    assert ym == "2026-03"


def test_get_category_folder_path_without_date():
    cat, ym = get_category_folder_path("other", None)
    assert cat == "other"
    assert ym is None


# ── Bilingual helpers (#56) ─────────────────────────────────────────────


def test_bilingual_name():
    assert bilingual_name("labs") == "labs — laboratórne výsledky"
    assert bilingual_name("treatment") == "treatment — priebeh liečby"
    assert bilingual_name("advocate") == "advocate — záznamy advokáta pacienta"
    assert bilingual_name("unknown") == "unknown"


def test_en_key_from_folder_name_bilingual():
    assert en_key_from_folder_name("labs — laboratórne výsledky") == "labs"
    assert en_key_from_folder_name("treatment — priebeh liečby") == "treatment"


def test_en_key_from_folder_name_legacy():
    assert en_key_from_folder_name("labs") == "labs"
    assert en_key_from_folder_name("treatment") == "treatment"


def test_en_key_from_folder_name_legacy_merges():
    """Legacy categories (surgical_report, discharge_summary) must be recognized."""
    assert en_key_from_folder_name("surgical_report") == "surgical_report"
    assert en_key_from_folder_name("discharge_summary") == "discharge_summary"
    assert en_key_from_folder_name("surgical_report — operačné protokoly") == "surgical_report"


def test_en_key_from_folder_name_unknown():
    assert en_key_from_folder_name("2026-03") is None
    assert en_key_from_folder_name("random_folder") is None


# ── resolve_category_folder (#273) ─────────────────────────────────────


def test_resolve_category_folder_found():
    """Returns the folder ID when category is in folder_map."""
    folder_map = {"labs": "labs_id", "other": "other_id"}
    assert resolve_category_folder(folder_map, "labs", "root_id") == "labs_id"


def test_resolve_category_folder_missing_falls_back_to_other():
    """Falls back to 'other' folder when category is missing — never root (#273)."""
    folder_map = {"labs": "labs_id", "other": "other_id"}
    assert resolve_category_folder(folder_map, "surgical_report", "root_id") == "other_id"
    assert resolve_category_folder(folder_map, "vaccination", "root_id") == "other_id"


def test_resolve_category_folder_no_other_falls_back_to_root():
    """Falls back to root only when even 'other' folder is missing (shouldn't happen)."""
    folder_map = {"labs": "labs_id"}
    assert resolve_category_folder(folder_map, "vaccination", "root_id") == "root_id"


# ── ensure_year_month_folder validation (#273) ─────────────────────────


def test_ensure_year_month_folder_rejects_short_date():
    """Returns parent folder as-is when date_str produces invalid year_month."""
    gdrive = _mock_gdrive()
    # "2025" is only 4 chars — [:7] = "2025" which has no dash at position 4
    result = ensure_year_month_folder(gdrive, "cat_folder", "2025")
    assert result == "cat_folder"  # Returns parent, doesn't create a "2025" folder
    gdrive.create_folder.assert_not_called()


def test_ensure_year_month_folder_rejects_year_only():
    """A bare year string should not create a folder."""
    gdrive = _mock_gdrive()
    result = ensure_year_month_folder(gdrive, "cat_folder", "2026")
    assert result == "cat_folder"
    gdrive.create_folder.assert_not_called()


def test_ensure_year_month_folder_accepts_valid_date():
    """Normal YYYY-MM-DD date string works correctly."""
    gdrive = _mock_gdrive()
    result = ensure_year_month_folder(gdrive, "cat_folder", "2026-03-15")
    gdrive.find_folder.assert_called_once_with("2026-03", "cat_folder")
    assert result == "folder_2026-03"


# ── Preventive: refuse to nest YYYY-MM under YYYY-MM (#457) ─────────────


def _clear_ym_cache():
    from oncofiles.gdrive_folders import _looks_like_year_month_folder

    _looks_like_year_month_folder._cache.clear()


def test_ensure_year_month_folder_refuses_ym_parent():
    """If the caller passed a YYYY-MM folder as the category parent, return
    the parent as-is instead of creating a nested YYYY-MM. Covers the root
    cause of #457's 71 nested folders on q1b."""
    _clear_ym_cache()
    gdrive = _mock_gdrive()
    # The preventive check calls gdrive._service.files().get().execute()
    gdrive._service.files.return_value.get.return_value.execute.return_value = {
        "name": "2026-02"  # Parent is itself YYYY-MM — must trigger refusal
    }

    result = ensure_year_month_folder(gdrive, "ym_folder_id", "2026-03-15")

    assert result == "ym_folder_id"  # returned parent unchanged
    gdrive.find_folder.assert_not_called()  # never asked find_folder
    gdrive.create_folder.assert_not_called()  # never tried to create


def test_ensure_year_month_folder_allows_category_parent():
    """Normal case: parent is a category folder (not YYYY-MM), proceed."""
    _clear_ym_cache()
    gdrive = _mock_gdrive()
    gdrive._service.files.return_value.get.return_value.execute.return_value = {
        "name": "labs — laboratórne výsledky"  # Bilingual category name
    }

    result = ensure_year_month_folder(gdrive, "labs_cat_id", "2026-03-15")

    assert result == "folder_2026-03"  # created, proceeded normally
    gdrive.find_folder.assert_called_once_with("2026-03", "labs_cat_id")


def test_ensure_year_month_folder_preventive_never_breaks_on_gdrive_error():
    """If the preventive lookup errors out, we still proceed — the check
    is best-effort and must not break the sync hot path."""
    _clear_ym_cache()
    gdrive = _mock_gdrive()
    gdrive._service.files.return_value.get.side_effect = RuntimeError("GDrive down")

    result = ensure_year_month_folder(gdrive, "cat_folder", "2026-03-15")

    assert result == "folder_2026-03"
