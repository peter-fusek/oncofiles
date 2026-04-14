"""Tests for document splitting engine."""

from __future__ import annotations

from oncofiles.consolidate import _add_part_suffix
from oncofiles.models import Document

# ── _add_part_suffix ─────────────────────────────────────────────────────────


def test_add_part_suffix_standard_format():
    """Adds Part suffix to standard filename."""
    result = _add_part_suffix("20260115_ErikaFusekova_NOU_Labs_BloodResults.pdf", 1, 3)
    assert result == "20260115_ErikaFusekova_NOU_Labs_BloodResults_Part1of3.pdf"


def test_add_part_suffix_no_extension():
    """Works without file extension."""
    result = _add_part_suffix("document", 2, 3)
    assert result == "document_Part2of3"


def test_add_part_suffix_removes_existing():
    """Removes existing PartNofM before adding new one."""
    result = _add_part_suffix("20260115_ErikaFusekova_NOU_Labs_BloodResults_Part1of2.pdf", 2, 3)
    assert result == "20260115_ErikaFusekova_NOU_Labs_BloodResults_Part2of3.pdf"


def test_add_part_suffix_preserves_extension():
    """Preserves original file extension."""
    result = _add_part_suffix("report.jpg", 1, 2)
    assert result == "report_Part1of2.jpg"


# ── Document model with group fields ────────────────────────────────────────


def test_document_model_group_fields():
    """Document model supports group_id, part_number, total_parts, split_source_doc_id."""
    doc = Document(
        file_id="test",
        filename="test.pdf",
        original_filename="test.pdf",
        group_id="abc-123",
        part_number=2,
        total_parts=3,
        split_source_doc_id=42,
    )
    assert doc.group_id == "abc-123"
    assert doc.part_number == 2
    assert doc.total_parts == 3
    assert doc.split_source_doc_id == 42


def test_document_model_group_fields_default_none():
    """Group fields default to None."""
    doc = Document(
        file_id="test",
        filename="test.pdf",
        original_filename="test.pdf",
    )
    assert doc.group_id is None
    assert doc.part_number is None
    assert doc.total_parts is None
    assert doc.split_source_doc_id is None


# ── _doc_to_dict with group fields ──────────────────────────────────────────


def test_doc_to_dict_includes_group_fields():
    """_doc_to_dict includes group fields when set."""
    from oncofiles.tools._helpers import _doc_to_dict

    doc = Document(
        id=1,
        file_id="test",
        filename="test.pdf",
        original_filename="test.pdf",
        group_id="grp-123",
        part_number=1,
        total_parts=2,
    )
    result = _doc_to_dict(doc)
    assert result["group_id"] == "grp-123"
    assert result["part_number"] == 1
    assert result["total_parts"] == 2


def test_doc_to_dict_excludes_group_fields_when_none():
    """_doc_to_dict omits group fields when None."""
    from oncofiles.tools._helpers import _doc_to_dict

    doc = Document(
        id=1,
        file_id="test",
        filename="test.pdf",
        original_filename="test.pdf",
    )
    result = _doc_to_dict(doc)
    assert "group_id" not in result
    assert "part_number" not in result
    assert "total_parts" not in result


def test_doc_to_dict_includes_split_source():
    """_doc_to_dict includes split_source_doc_id when set."""
    from oncofiles.tools._helpers import _doc_to_dict

    doc = Document(
        id=2,
        file_id="test",
        filename="test.pdf",
        original_filename="test.pdf",
        split_source_doc_id=1,
    )
    result = _doc_to_dict(doc)
    assert result["split_source_doc_id"] == 1
