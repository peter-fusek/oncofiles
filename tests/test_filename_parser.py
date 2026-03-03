"""Tests for filename parser."""

from datetime import date

from erika_files_mcp.filename_parser import parse_filename
from erika_files_mcp.models import DocumentCategory


def test_full_convention():
    r = parse_filename("20240115_NOUonko_labs_krvnyObraz.pdf")
    assert r.document_date == date(2024, 1, 15)
    assert r.institution == "NOUonko"
    assert r.category == DocumentCategory.LABS
    assert r.description == "krvnyObraz"
    assert r.extension == "pdf"


def test_different_institution():
    r = parse_filename("20240220_OUSA_report_kontrola.pdf")
    assert r.document_date == date(2024, 2, 20)
    assert r.institution == "OUSA"
    assert r.category == DocumentCategory.REPORT
    assert r.description == "kontrola"


def test_imaging_with_hyphen_description():
    r = parse_filename("20231005_UNB_imaging_CT-abdomen.pdf")
    assert r.document_date == date(2023, 10, 5)
    assert r.institution == "UNB"
    assert r.category == DocumentCategory.IMAGING
    assert r.description == "CT abdomen"


def test_no_date():
    r = parse_filename("NOUonko_labs_krvnyObraz.pdf")
    assert r.document_date is None
    assert r.institution == "NOUonko"
    assert r.category == DocumentCategory.LABS


def test_date_only():
    r = parse_filename("20240115.pdf")
    assert r.document_date == date(2024, 1, 15)
    assert r.institution is None
    assert r.category == DocumentCategory.OTHER
    assert r.extension == "pdf"


def test_unknown_format():
    r = parse_filename("random_document.pdf")
    assert r.document_date is None
    assert r.institution is None
    assert r.category == DocumentCategory.OTHER
    assert r.description is not None


def test_category_aliases_slovak():
    r = parse_filename("20240101_NOUonko_epikryza_poOperacii.pdf")
    assert r.category == DocumentCategory.DISCHARGE
    assert r.description == "poOperacii"


def test_invalid_date_falls_through():
    r = parse_filename("20241350_NOUonko_labs.pdf")
    assert r.document_date is None  # month 13 is invalid
    assert r.institution == "NOUonko"


def test_empty_description():
    r = parse_filename("20240115_NOUonko_labs.pdf")
    assert r.document_date == date(2024, 1, 15)
    assert r.institution == "NOUonko"
    assert r.category == DocumentCategory.LABS
    assert r.description is None


def test_medirex_lab():
    r = parse_filename("20240301_Medirex_labs_onkomarkery.pdf")
    assert r.institution == "Medirex"
    assert r.category == DocumentCategory.LABS
    assert r.description == "onkomarkery"


def test_extension_variety():
    r = parse_filename("20240101_NOUonko_report_sprava.jpg")
    assert r.extension == "jpg"
    assert r.category == DocumentCategory.REPORT
