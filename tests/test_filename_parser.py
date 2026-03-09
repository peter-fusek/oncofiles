"""Tests for filename parser — real naming convention.

Real format: YYYYMMDD ErikaFusekova-Institution-DescriptionDoctor.ext
"""

from datetime import date

from oncofiles.filename_parser import parse_filename, rename_to_bilingual
from oncofiles.models import DocumentCategory

# ── Real filenames from Google Drive ─────────────────────────────────────────


class TestNewFormat:
    """Tests for the real YYYYMMDD ErikaFusekova-... naming convention."""

    def test_lab_results(self):
        r = parse_filename("20260227 ErikaFusekova-NOU-LabVysledkyPred2chemoMudrPorsok.pdf")
        assert r.document_date == date(2026, 2, 27)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.LABS
        assert r.description == "LabVysledkyPred2chemoMudrPorsok"
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
        assert r.category == DocumentCategory.IMAGING_US

    def test_imaging_ct(self):
        r = parse_filename("20260130 ErikaFusekova-NOU-CTobjednavkaMudrPorsokPrimarOnkolog.pdf")
        assert r.document_date == date(2026, 1, 30)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.IMAGING_CT

    def test_report_sprava(self):
        r = parse_filename("20260130 ErikaFusekova-NOU-SpravaMudrPorsokPrimarOnkolog.pdf")
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
        assert r.category == DocumentCategory.GENETICS

    def test_genetic_report_is_genetics(self):
        r = parse_filename("20260212 ErikaFusekova-NOU-SpravaZgenetickehoVysetreniaMudrCernak.JPG")
        assert r.category == DocumentCategory.GENETICS

    def test_blood_krv_is_labs(self):
        r = parse_filename("20260213 ErikaFusekova-NOU-KrvPredChemoMudrPittichova.JPG")
        assert r.category == DocumentCategory.LABS

    def test_krv_page_labs(self):
        r = parse_filename("20260227 ErikaFusekova-NOU-KRV1z2SpravaPredChemoMudrPazderkova.JPG")
        assert r.category == DocumentCategory.LABS

    def test_priebezna_sprava_report(self):
        r = parse_filename("20260216 ErikaFusekova-NOU-PriebeznaSpravaZhospitalizaciePo1chemo.pdf")
        assert r.category == DocumentCategory.REPORT

    def test_prijem_report(self):
        r = parse_filename("20260213 ErikaFusekova-NOU-PrijemNaChemoMudrPazderkova.JPG")
        assert r.category == DocumentCategory.REPORT

    def test_konzultacia_report(self):
        r = parse_filename("20260220 ErikaFusekova-NOU-KonzultaciaBioLiecbyPo1chemoMudrPorsok.JPG")
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
        assert r.category == DocumentCategory.GENETICS
        assert r.extension == ""

    def test_nursing_discharge(self):
        r = parse_filename(
            "20260122 ErikaFusekova-BoryNemocnica-OsetrovatelskaPrepustaciaSpravaOperacia.pdf"
        )
        assert r.category == DocumentCategory.DISCHARGE

    def test_vysetrenie_report(self):
        r = parse_filename("20260129 ErikaFusekova-BoryNemocnica-PooperacneVysetrenieChirurg.pdf")
        assert r.category == DocumentCategory.REPORT

    def test_chemo_sheet(self):
        r = parse_filename("20260213 ErikaFusekova-NOU-ChemoterapeutickyProtokolFOLFOX.pdf")
        assert r.category == DocumentCategory.CHEMO_SHEET

    def test_discharge_summary_legacy_alias(self):
        # Legacy parser splits on _ so "discharge_summary" → token "discharge" matched first
        r = parse_filename("20260122_BoryNemocnica_discharge_summary_operacia.pdf")
        assert r.category == DocumentCategory.DISCHARGE

    def test_surgical_report_alias(self):
        # Legacy parser splits on _ so tokens "surgical" + "report" (REPORT)
        r = parse_filename("20260118_BoryNemocnica_surgical_report_resection.pdf")
        assert r.category == DocumentCategory.REPORT

    def test_genetics_alias(self):
        r = parse_filename("20260226_NOU_genetics_KRAS.pdf")
        assert r.category == DocumentCategory.GENETICS

    def test_imaging_ct_alias(self):
        r = parse_filename("20260130_NOU_ct_abdomen.pdf")
        assert r.category == DocumentCategory.IMAGING_CT

    def test_imaging_us_alias(self):
        r = parse_filename("20260129_BoryNemocnica_usg_abdomen.pdf")
        assert r.category == DocumentCategory.IMAGING_US


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


# ── Bilingual rename (#57) ──────────────────────────────────────────────────


class TestBilingualRename:
    """Tests for rename_to_bilingual — adding EN category prefix."""

    def test_lab_results(self):
        result = rename_to_bilingual(
            "20260227 ErikaFusekova-NOU-LabVysledkyPred2chemoMudrPorsok.pdf"
        )
        assert result == "20260227 ErikaFusekova-NOU-Labs-LabVysledkyPred2chemoMudrPorsok.pdf"

    def test_discharge_summary(self):
        result = rename_to_bilingual(
            "20260122 ErikaFusekova-BoryNemocnica-LekarskaPrepustaciaSpravaOperacia.pdf"
        )
        assert result == (
            "20260122 ErikaFusekova-BoryNemocnica-Discharge-LekarskaPrepustaciaSpravaOperacia.pdf"
        )

    def test_biopsy(self):
        result = rename_to_bilingual("20260127 ErikaFusekova-BoryNemocnica-BiopsiaMudrRychly.JPG")
        assert result == "20260127 ErikaFusekova-BoryNemocnica-Pathology-BiopsiaMudrRychly.JPG"

    def test_genetics(self):
        result = rename_to_bilingual("20260226 ErikaFusekova-NOU-GenetikaMudrMalejcikova1z2.JPG")
        assert result == "20260226 ErikaFusekova-NOU-Genetics-GenetikaMudrMalejcikova1z2.JPG"

    def test_imaging_ct(self):
        result = rename_to_bilingual(
            "20260130 ErikaFusekova-NOU-CTobjednavkaMudrPorsokPrimarOnkolog.pdf"
        )
        assert result == ("20260130 ErikaFusekova-NOU-CT-CTobjednavkaMudrPorsokPrimarOnkolog.pdf")

    def test_imaging_usg(self):
        result = rename_to_bilingual("20260129 ErikaFusekova-BoryNemocnica-USGMudrTulenkova.pdf")
        assert result == "20260129 ErikaFusekova-BoryNemocnica-USG-USGMudrTulenkova.pdf"

    def test_chemo_sheet(self):
        result = rename_to_bilingual(
            "20260213 ErikaFusekova-NOU-ChemoterapeutickyProtokolFOLFOX.pdf"
        )
        assert result == (
            "20260213 ErikaFusekova-NOU-ChemoSheet-ChemoterapeutickyProtokolFOLFOX.pdf"
        )

    def test_report(self):
        result = rename_to_bilingual("20260130 ErikaFusekova-NOU-SpravaMudrPorsokPrimarOnkolog.pdf")
        assert result == ("20260130 ErikaFusekova-NOU-Report-SpravaMudrPorsokPrimarOnkolog.pdf")

    def test_other_category(self):
        result = rename_to_bilingual(
            "20260209 ErikaFusekova-SocialnaPoistovna-UznanieNemocenskeho.pdf"
        )
        assert result == ("20260209 ErikaFusekova-SocialnaPoistovna-Other-UznanieNemocenskeho.pdf")

    def test_already_bilingual_unchanged(self):
        """Already-renamed files should not be double-prefixed."""
        fn = "20260227 ErikaFusekova-NOU-Labs-LabVysledkyPred2chemoMudrPorsok.pdf"
        assert rename_to_bilingual(fn) == fn

    def test_explicit_category_override(self):
        result = rename_to_bilingual(
            "20260209 ErikaFusekova-SocialnaPoistovna-UznanieNemocenskeho.pdf",
            category="referral",
        )
        assert "Referral-" in result

    def test_no_description_unchanged(self):
        """Files without parseable description stay unchanged."""
        assert rename_to_bilingual("20240115.pdf") == "20240115.pdf"
