"""Tests for filename parser — real naming convention.

Real format: YYYYMMDD ErikaFusekova-Institution-DescriptionDoctor.ext
"""

from datetime import date

from erika_files_mcp.filename_parser import parse_filename
from erika_files_mcp.models import DocumentCategory

# ── Real filenames from Google Drive ─────────────────────────────────────────


class TestNewFormat:
    """Tests for the real YYYYMMDD ErikaFusekova-... naming convention."""

    def test_lab_results(self):
        r = parse_filename("20260227 ErikaFusekova-NOU-LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf")
        assert r.document_date == date(2026, 2, 27)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.LABS
        assert r.description == "LabVysledkyPred2chemo[PHYSICIAN_REDACTED]"
        assert r.extension == "pdf"

    def test_discharge_summary(self):
        r = parse_filename(
            "20260122 ErikaFusekova-BoryNemocnica-LekarskaPrepustaciaSpravaOperacia.pdf"
        )
        assert r.document_date == date(2026, 1, 22)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.DISCHARGE
        assert r.description == "LekarskaPrepustaciaSpravaOperacia"

    def test_biopsy_jpg(self):
        r = parse_filename("20260127 ErikaFusekova-BoryNemocnica-BiopsiaMudrRychly.JPG")
        assert r.document_date == date(2026, 1, 27)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.PATHOLOGY
        assert r.extension == "JPG"

    def test_imaging_usg(self):
        r = parse_filename("20260129 ErikaFusekova-BoryNemocnica-USGMudrTulenkova.pdf")
        assert r.document_date == date(2026, 1, 29)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.IMAGING

    def test_imaging_ct(self):
        r = parse_filename("20260130 ErikaFusekova-NOU-CTobjednavka[PHYSICIAN_REDACTED]PrimarOnkolog.pdf")
        assert r.document_date == date(2026, 1, 30)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.IMAGING

    def test_report_sprava(self):
        r = parse_filename("20260130 ErikaFusekova-NOU-Sprava[PHYSICIAN_REDACTED]PrimarOnkolog.pdf")
        assert r.document_date == date(2026, 1, 30)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.REPORT

    def test_report_anamneza(self):
        r = parse_filename(
            "20251201 ErikaFusekova-BoryNemocnica-AnamnezaMudrDanihelPrimarChirurg.pdf"
        )
        assert r.document_date == date(2025, 12, 1)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.REPORT

    def test_genetic_pathology(self):
        r = parse_filename("20260226 ErikaFusekova-NOU-GenetikaMudrMalejcikova1z2.JPG")
        assert r.document_date == date(2026, 2, 26)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.PATHOLOGY

    def test_genetic_report_is_pathology(self):
        r = parse_filename("20260212 ErikaFusekova-NOU-SpravaZgenetickehoVysetreniaMudrCernak.JPG")
        assert r.category == DocumentCategory.PATHOLOGY

    def test_blood_krv_is_labs(self):
        r = parse_filename("20260213 ErikaFusekova-NOU-KrvPredChemoMudrPittichova.JPG")
        assert r.category == DocumentCategory.LABS

    def test_krv_page_labs(self):
        r = parse_filename("20260227 ErikaFusekova-NOU-KRV1z2SpravaPredChemo[PHYSICIAN_REDACTED].JPG")
        assert r.category == DocumentCategory.LABS

    def test_priebezna_sprava_report(self):
        r = parse_filename("20260216 ErikaFusekova-NOU-PriebeznaSpravaZhospitalizaciePo1chemo.pdf")
        assert r.category == DocumentCategory.REPORT

    def test_prijem_report(self):
        r = parse_filename("20260213 ErikaFusekova-NOU-PrijemNaChemo[PHYSICIAN_REDACTED].JPG")
        assert r.category == DocumentCategory.REPORT

    def test_konzultacia_report(self):
        r = parse_filename("20260220 ErikaFusekova-NOU-KonzultaciaBioLiecbyPo1chemo[PHYSICIAN_REDACTED].JPG")
        assert r.category == DocumentCategory.REPORT

    def test_socialna_poistovna(self):
        r = parse_filename("20260209 ErikaFusekova-SocialnaPoistovna-UznanieNemocenskeho.pdf")
        assert r.institution == "SocialnaPoistovna"
        assert r.category == DocumentCategory.OTHER

    def test_minnesota_university(self):
        r = parse_filename("20260220 ErikaFusekova-MinnesotaUniversity-SecondOpinion.gdoc")
        assert r.institution == "MinnesotaUniversity"
        assert r.extension == "gdoc"

    def test_pn_admin_is_other(self):
        r = parse_filename("20260128 ErikaFusekova-BoryNemocnica-UkonceniePapierovejPNChirurg.JPG")
        assert r.category == DocumentCategory.OTHER

    def test_xx_date(self):
        r = parse_filename(
            "202602xx ErikaFusekova-BoryNemocnica-PNkaPreSocialnaPoistovnaOprava.pdf"
        )
        assert r.document_date == date(2026, 2, 1)  # first of month for xx
        assert r.institution == "BoryNemocnica"

    def test_no_extension(self):
        r = parse_filename("20260227 ErikaFusekova-NOU-GenetickeVysetrenie")
        assert r.document_date == date(2026, 2, 27)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.PATHOLOGY
        assert r.extension == ""

    def test_nursing_discharge(self):
        r = parse_filename(
            "20260122 ErikaFusekova-BoryNemocnica-OsetrovatelskaPrepustaciaSpravaOperacia.pdf"
        )
        assert r.category == DocumentCategory.DISCHARGE

    def test_vysetrenie_report(self):
        r = parse_filename("20260129 ErikaFusekova-BoryNemocnica-PooperacneVysetrenieChirurg.pdf")
        assert r.category == DocumentCategory.REPORT


# ── Legacy format tests ──────────────────────────────────────────────────────


class TestLegacyFormat:
    """Tests for the original YYYYMMDD_institution_category_description.ext format."""

    def test_full_convention(self):
        r = parse_filename("20240115_NOUonko_labs_krvnyObraz.pdf")
        assert r.document_date == date(2024, 1, 15)
        assert r.institution == "NOUonko"
        assert r.category == DocumentCategory.LABS
        assert r.description == "krvnyObraz"
        assert r.extension == "pdf"

    def test_different_institution(self):
        r = parse_filename("20240220_OUSA_report_kontrola.pdf")
        assert r.document_date == date(2024, 2, 20)
        assert r.institution == "OUSA"
        assert r.category == DocumentCategory.REPORT

    def test_no_date(self):
        r = parse_filename("NOUonko_labs_krvnyObraz.pdf")
        assert r.document_date is None
        assert r.institution == "NOUonko"
        assert r.category == DocumentCategory.LABS

    def test_date_only(self):
        r = parse_filename("20240115.pdf")
        assert r.document_date == date(2024, 1, 15)
        assert r.institution is None
        assert r.extension == "pdf"

    def test_unknown_format(self):
        r = parse_filename("random_document.pdf")
        assert r.document_date is None
        assert r.institution is None
        assert r.description is not None

    def test_invalid_date_falls_through(self):
        r = parse_filename("20241350_NOUonko_labs.pdf")
        assert r.document_date is None  # month 13 is invalid


# ── Non-standard filenames from subdirs ──────────────────────────────────────


class TestSubdirFilenames:
    """Tests for non-standard filenames from subdirectories."""

    def test_guidelines_colon(self):
        r = parse_filename("colon-patient.pdf")
        assert r.document_date is None
        assert r.institution is None
        assert r.extension == "pdf"

    def test_guidelines_modra_kniha(self):
        r = parse_filename("Modra kniha 2025.pdf")
        assert r.document_date is None
        assert r.extension == "pdf"

    def test_insurance_card_photo(self):
        r = parse_filename("Erika Fuseková - 2026-02-20 15.51.29.jpg")
        assert r.extension == "jpg"

    def test_analyzy_strategic_plan(self):
        r = parse_filename("20260217 Erika_Strategicky_Rozhodovaci_Plan_mCRC_20260217_220735.pdf")
        assert r.document_date == date(2026, 2, 17)
        assert r.extension == "pdf"
