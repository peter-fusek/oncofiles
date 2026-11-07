"""Lock tests for the README filename standard (#562)."""

import pytest

from oncofiles.curated.naming import (
    MAX_LEN,
    UNKNOWN,
    NameFields,
    build_filename,
    camel,
    is_standard,
    parse_filename,
    surname_token,
    with_collision_suffix,
)

# Synthetic names only — this repo is public.
README_EXAMPLES = [
    "20260805-JanaNovakova-NOU-Report-Kovacova-KlinikaKlinickejOnkologie.pdf",
    "20260805-JanaNovakova-NOU-Imaging-Unknown-RDO.pdf",
    "20260709-JanaNovakova-NOU-Labs-Hruskova-KlinikaOnkohematologie.pdf",
    "20260122-JanaNovakova-SampleHospital-Discharge-Mrkva-Chirurgia.pdf",
    "20260223-JanaNovakova-SampleHospital-Pathology-Rychly-Patologia.pdf",
    "20260311-JanaNovakova-SamplePublisher-Reference-Unknown-TextbookCh40.pdf",
]


@pytest.mark.parametrize("name", README_EXAMPLES)
def test_readme_examples_round_trip(name):
    fields = parse_filename(name)
    assert fields is not None
    assert build_filename(fields, name.rsplit(".", 1)[1]) == name


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Nováková", "Novakova"),
        ("Pôrsôk", "Porsok"),
        ("Klinika klinickej onkológie", "KlinikaKlinickejOnkologie"),
        ("SampleHospital", "SampleHospital"),
        ("ORO/RAAKON", "ORORAAKON"),
        ("", UNKNOWN),
        (None, UNKNOWN),
        ("  --  ", UNKNOWN),
    ],
)
def test_camel(raw, expected):
    assert camel(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Kováčová, Jana (MUDr. Mgr., PhD.)", "Kovacova"),
        ("MUDr. Jana Kováčová", "Kovacova"),
        ("Ing. Martin Čierny", "Cierny"),
        ("doc. MUDr. Pavol Pôrsôk PhD", "Porsok"),
        ("", UNKNOWN),
        (None, UNKNOWN),
    ],
)
def test_surname_token(raw, expected):
    assert surname_token(raw) == expected


def test_no_diacritics_spaces_or_underscores_ever():
    name = build_filename(
        NameFields(
            date="20260805",
            patient="Jana Nováková",
            institution="Národný onkologický ústav",
            doc_type="Report",
            doctor="Kováčová",
            hospital="Klinika klinickej onkológie_Amb",
            subject="pred chemo 3",
        ),
        "PDF",
    )
    assert name.isascii() and " " not in name and "_" not in name
    assert name.endswith(".pdf")
    assert parse_filename(name) is not None


def test_unknown_for_missing_fields():
    name = build_filename(NameFields("20260805", "JanaNovakova", "", "Imaging", None, "RDO"), "pdf")
    assert name == "20260805-JanaNovakova-Unknown-Imaging-Unknown-RDO.pdf"


@pytest.mark.parametrize("bad", ["", "Unknown", "1976-04-02", "2026080", "20260805x"])
def test_date_is_mandatory_and_never_unknown(bad):
    with pytest.raises(ValueError):
        build_filename(NameFields(bad, "P", "I", "Report", "D", "H"), "pdf")


def test_length_truncates_hospital_then_subject():
    long = "A" * 80
    name = build_filename(
        NameFields("20260805", "JanaNovakova", "NOU", "Report", "Kovacova", long, "Subj"),
        "pdf",
    )
    assert len(name) <= MAX_LEN
    assert name.endswith("-Subj.pdf")  # subject survives while hospital can shrink
    name2 = build_filename(
        NameFields("20260805", "JanaNovakova", "NOU", "Report", "Kovacova", "H", "S" * 30),
        "pdf",
        max_len=60,
    )
    assert len(name2) <= 60


def test_collision_suffix():
    base = README_EXAMPLES[0]
    taken = {base, base.replace(".pdf", "-02.pdf")}
    assert with_collision_suffix(base, taken) == base.replace(".pdf", "-03.pdf")
    assert with_collision_suffix(base, set()) == base


@pytest.mark.parametrize(
    "name",
    [
        "20260805_JanaNovakova_NOU_Report_Desc.pdf",  # legacy underscore format
        "0254_261104213825_001.pdf",  # scanner name
        "20260805-JanaNovakova-NOU-Report.pdf",  # too few fields
        "20260805-Jana Novakova-NOU-Report-X-Y.pdf",  # space
        "20260805-JanaNovakova-NOU-Report-X-Y.PDF",  # uppercase ext
    ],
)
def test_non_standard_names_rejected(name):
    assert not is_standard(name)


def test_is_standard_checks_patient_token():
    assert is_standard(README_EXAMPLES[0], patient_token="JanaNovakova")
    assert not is_standard(README_EXAMPLES[0], patient_token="PeterNovak")
