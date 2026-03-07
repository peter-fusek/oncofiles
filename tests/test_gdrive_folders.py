"""Tests for GDrive folder structure management."""

from __future__ import annotations

from unittest.mock import MagicMock

from oncofiles.gdrive_folders import (
    ensure_folder_structure,
    ensure_year_month_folder,
    find_or_create_folder,
    get_category_folder_path,
)


def _mock_gdrive():
    """Create a mock GDriveClient for folder operations."""
    gdrive = MagicMock()
    # find_folder returns None by default (no existing folders)
    gdrive.find_folder.return_value = None
    # create_folder returns a fake ID
    gdrive.create_folder.side_effect = lambda name, parent: f"folder_{name}"
    return gdrive


def test_ensure_folder_structure_creates_all():
    """Creates all category + metadata folders."""
    gdrive = _mock_gdrive()
    result = ensure_folder_structure(gdrive, "root123")

    assert "labs" in result
    assert "imaging" in result
    assert "conversations" in result
    assert "treatment" in result
    assert "research" in result
    assert len(result) >= 12  # 9 categories + 3 metadata


def test_ensure_folder_structure_finds_existing():
    """Uses existing folders instead of creating new ones."""
    gdrive = _mock_gdrive()
    gdrive.find_folder.return_value = "existing_id"

    result = ensure_folder_structure(gdrive, "root123")

    # All folders should use the existing ID
    for folder_id in result.values():
        assert folder_id == "existing_id"
    gdrive.create_folder.assert_not_called()


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
