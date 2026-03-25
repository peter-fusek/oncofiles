"""Tests for filename parser — all naming conventions.

Standard format (v3.15+): YYYYMMDD_ErikaFusekova_Institution_Category_DescriptionEN.ext
Bilingual format (v3.5-3.14): YYYYMMDD ErikaFusekova-Institution-Category-SKDescription.ext
Legacy format: YYYYMMDD_institution_category_description.ext
"""

from datetime import date

from oncofiles.filename_parser import (
    is_corrupted_filename,
    is_standard_format,
    parse_filename,
    rename_to_bilingual,
    rename_to_standard,
)
from oncofiles.models import DocumentCategory

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
        assert r.category == DocumentCategory.GENETICS

    def test_genetic_report_is_genetics(self):
        r = parse_filename("20260212 ErikaFusekova-NOU-SpravaZgenetickehoVysetreniaMudrCernak.JPG")
        assert r.category == DocumentCategory.GENETICS

    def test_blood_krv_is_labs(self):
        r = parse_filename("20260213 ErikaFusekova-NOU-KrvPredChemoMudrPittichova.JPG")
        assert r.category == DocumentCategory.LABS

    def test_krv_page_labs(self):
        r = parse_filename("20260227 ErikaFusekova-NOU-KRV1z2SpravaPredChemo[PHYSICIAN_REDACTED].JPG")
        assert r.category == DocumentCategory.LABS

    def test_priebezna_sprava_report(self):
        r = parse_filename("20260216 ErikaFusekova-NOU-PriebeznaSpravaZhospitalizaciePo1chemo.pdf")
        assert r.category == DocumentCategory.REPORT

    def test_prijem_consultation(self):
        r = parse_filename("20260213 ErikaFusekova-NOU-PrijemNaChemo[PHYSICIAN_REDACTED].JPG")
        assert r.category == DocumentCategory.CONSULTATION

    def test_konzultacia_consultation(self):
        r = parse_filename("20260220 ErikaFusekova-NOU-KonzultaciaBioLiecbyPo1chemo[PHYSICIAN_REDACTED].JPG")
        assert r.category == DocumentCategory.CONSULTATION

    def test_advocate_institution(self):
        r = parse_filename(
            "20250127 ErikaFusekova-PacientAdvokat-Other-SumarPrepustacejSpravyPoOperacii.md"
        )
        assert r.institution == "PacientAdvokat"
        assert r.document_date == date(2025, 1, 27)

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

    def test_vysetrenie_consultation(self):
        r = parse_filename("20260129 ErikaFusekova-BoryNemocnica-PooperacneVysetrenieChirurg.pdf")
        assert r.category == DocumentCategory.CONSULTATION

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
        assert r.category == DocumentCategory.IMAGING

    def test_imaging_us_alias(self):
        r = parse_filename("20260129_BoryNemocnica_usg_abdomen.pdf")
        assert r.category == DocumentCategory.IMAGING

    def test_reference_alias(self):
        r = parse_filename("20260301_NOU_reference_guidelines.pdf")
        assert r.category == DocumentCategory.REFERENCE

    def test_reference_keyword_infer(self):
        r = parse_filename("20260301 ErikaFusekova-NOU-ReferencneMaterialyKRAS.pdf")
        assert r.category == DocumentCategory.REFERENCE


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
            "20260227 ErikaFusekova-NOU-LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf"
        )
        assert result == "20260227 ErikaFusekova-NOU-Labs-LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf"

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
            "20260130 ErikaFusekova-NOU-CTobjednavka[PHYSICIAN_REDACTED]PrimarOnkolog.pdf"
        )
        assert result == (
            "20260130 ErikaFusekova-NOU-Imaging-CTobjednavka[PHYSICIAN_REDACTED]PrimarOnkolog.pdf"
        )

    def test_imaging_usg(self):
        result = rename_to_bilingual("20260129 ErikaFusekova-BoryNemocnica-USGMudrTulenkova.pdf")
        assert result == ("20260129 ErikaFusekova-BoryNemocnica-Imaging-USGMudrTulenkova.pdf")

    def test_chemo_sheet(self):
        result = rename_to_bilingual(
            "20260213 ErikaFusekova-NOU-ChemoterapeutickyProtokolFOLFOX.pdf"
        )
        assert result == (
            "20260213 ErikaFusekova-NOU-ChemoSheet-ChemoterapeutickyProtokolFOLFOX.pdf"
        )

    def test_report(self):
        result = rename_to_bilingual("20260130 ErikaFusekova-NOU-Sprava[PHYSICIAN_REDACTED]PrimarOnkolog.pdf")
        assert result == ("20260130 ErikaFusekova-NOU-Report-Sprava[PHYSICIAN_REDACTED]PrimarOnkolog.pdf")

    def test_other_category(self):
        result = rename_to_bilingual(
            "20260209 ErikaFusekova-SocialnaPoistovna-UznanieNemocenskeho.pdf"
        )
        assert result == ("20260209 ErikaFusekova-SocialnaPoistovna-Other-UznanieNemocenskeho.pdf")

    def test_already_bilingual_unchanged(self):
        """Already-renamed files should not be double-prefixed."""
        fn = "20260227 ErikaFusekova-NOU-Labs-LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf"
        assert rename_to_bilingual(fn) == fn

    def test_explicit_category_override(self):
        result = rename_to_bilingual(
            "20260209 ErikaFusekova-SocialnaPoistovna-UznanieNemocenskeho.pdf",
            category="referral",
        )
        assert "Referral-" in result

    def test_reference_category(self):
        result = rename_to_bilingual(
            "20260301 ErikaFusekova-NOU-ReferencneMaterialyKRAS.pdf",
            category="reference",
        )
        assert "Reference-" in result

    def test_advocate_category(self):
        result = rename_to_bilingual(
            "20250127 ErikaFusekova-PacientAdvokat-Other-SumarPrepustacejSpravyPoOperacii.md",
            category="advocate",
        )
        assert "Advocate-" in result

    def test_no_description_unchanged(self):
        """Files without parseable description stay unchanged."""
        assert rename_to_bilingual("20240115.pdf") == "20240115.pdf"


# ── Standard format tests (v3.15+) ──────────────────────────────────────────


class TestStandardFormat:
    """Tests for YYYYMMDD_ErikaFusekova_Institution_Category_Description format."""

    def test_full_standard(self):
        r = parse_filename("20260227_ErikaFusekova_NOU_Labs_BloodResultsBeforeCycle2DrPorsok.pdf")
        assert r.document_date == date(2026, 2, 27)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.LABS
        assert r.description == "BloodResultsBeforeCycle2DrPorsok"
        assert r.extension == "pdf"

    def test_discharge(self):
        r = parse_filename(
            "20260122_ErikaFusekova_BoryNemocnica_Discharge_DischargeSummaryAfterSurgery.pdf"
        )
        assert r.document_date == date(2026, 1, 22)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.DISCHARGE

    def test_pathology(self):
        r = parse_filename("20260127_ErikaFusekova_BoryNemocnica_Pathology_BiopsyDrRychly.JPG")
        assert r.document_date == date(2026, 1, 27)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.PATHOLOGY
        assert r.extension == "JPG"

    def test_genetics(self):
        r = parse_filename("20260226_ErikaFusekova_NOU_Genetics_KRASMutationAnalysis.JPG")
        assert r.document_date == date(2026, 2, 26)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.GENETICS

    def test_imaging_ct(self):
        r = parse_filename("20260130_ErikaFusekova_NOU_CT_AbdomenScanDrPorsok.pdf")
        assert r.document_date == date(2026, 1, 30)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.IMAGING

    def test_imaging_usg(self):
        r = parse_filename("20260129_ErikaFusekova_BoryNemocnica_USG_AbdomenDrTulenkova.pdf")
        assert r.category == DocumentCategory.IMAGING

    def test_chemo_sheet(self):
        r = parse_filename("20260213_ErikaFusekova_NOU_ChemoSheet_FOLFOXProtocol.pdf")
        assert r.category == DocumentCategory.CHEMO_SHEET

    def test_surgical_report(self):
        r = parse_filename("20260118_ErikaFusekova_BoryNemocnica_SurgicalReport_ColonResection.pdf")
        assert r.category == DocumentCategory.SURGICAL_REPORT

    def test_reference(self):
        r = parse_filename("20260301_ErikaFusekova_NOU_Reference_DeVitaCh40ColorectalCancer.pdf")
        assert r.category == DocumentCategory.REFERENCE

    def test_advocate(self):
        r = parse_filename("20250127_ErikaFusekova_PacientAdvokat_Advocate_SummaryAfterSurgery.md")
        assert r.institution == "PacientAdvokat"
        assert r.category == DocumentCategory.ADVOCATE

    def test_discharge_summary(self):
        r = parse_filename("20260122_ErikaFusekova_BoryNemocnica_DischargeSummary_PostOpCare.pdf")
        assert r.category == DocumentCategory.DISCHARGE_SUMMARY

    def test_description_with_underscores(self):
        r = parse_filename("20260227_ErikaFusekova_NOU_Labs_Blood_Results_Cycle2.pdf")
        assert r.category == DocumentCategory.LABS
        assert r.description == "Blood_Results_Cycle2"

    def test_no_description(self):
        r = parse_filename("20260227_ErikaFusekova_NOU_Labs.pdf")
        assert r.category == DocumentCategory.LABS
        assert r.institution == "NOU"
        assert r.description is None

    def test_unknown_category_token_infers(self):
        """If category token is not recognized, fall back to inference."""
        r = parse_filename("20260227_ErikaFusekova_NOU_LabVysledkyPred2chemo.pdf")
        assert r.category == DocumentCategory.LABS
        assert r.description == "LabVysledkyPred2chemo"

    def test_other_category(self):
        r = parse_filename(
            "20260209_ErikaFusekova_SocialnaPoistovna_Other_SickLeaveConfirmation.pdf"
        )
        assert r.category == DocumentCategory.OTHER
        assert r.institution == "SocialnaPoistovna"


class TestIsStandardFormat:
    """Tests for is_standard_format() detection."""

    def test_standard_format(self):
        assert is_standard_format("20260227_ErikaFusekova_NOU_Labs_BloodResults.pdf") is True

    def test_bilingual_format_not_standard(self):
        assert is_standard_format("20260227 ErikaFusekova-NOU-Labs-LabVysledky.pdf") is False

    def test_legacy_format_not_standard(self):
        assert is_standard_format("20260227_NOU_labs_krvnyObraz.pdf") is False

    def test_no_date_not_standard(self):
        assert is_standard_format("ErikaFusekova_NOU_Labs_Blood.pdf") is False


class TestRenameToStandard:
    """Tests for rename_to_standard — converting to standard format."""

    def test_from_bilingual(self):
        result = rename_to_standard(
            "20260227 ErikaFusekova-NOU-LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf"
        )
        assert result == "20260227_ErikaFusekova_NOU_Labs_LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf"

    def test_from_bilingual_with_en_description(self):
        result = rename_to_standard(
            "20260227 ErikaFusekova-NOU-LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf",
            en_description="BloodResultsBeforeCycle2DrPorsok",
        )
        assert result == "20260227_ErikaFusekova_NOU_Labs_BloodResultsBeforeCycle2DrPorsok.pdf"

    def test_from_bilingual_with_category_override(self):
        result = rename_to_standard(
            "20260209 ErikaFusekova-SocialnaPoistovna-UznanieNemocenskeho.pdf",
            category="referral",
            en_description="SickLeaveConfirmation",
        )
        assert result == (
            "20260209_ErikaFusekova_SocialnaPoistovna_Referral_SickLeaveConfirmation.pdf"
        )

    def test_already_standard_unchanged(self):
        fn = "20260227_ErikaFusekova_NOU_Labs_BloodResultsBeforeCycle2DrPorsok.pdf"
        assert rename_to_standard(fn) == fn

    def test_from_bilingual_with_prefix(self):
        """Bilingual files with category prefix should strip it."""
        result = rename_to_standard("20260227 ErikaFusekova-NOU-Labs-LabVysledkyPred2chemo.pdf")
        assert result == "20260227_ErikaFusekova_NOU_Labs_LabVysledkyPred2chemo.pdf"

    def test_no_date_unchanged(self):
        assert rename_to_standard("random_document.pdf") == "random_document.pdf"

    def test_ct_category_token(self):
        result = rename_to_standard(
            "20260130 ErikaFusekova-NOU-CTobjednavka[PHYSICIAN_REDACTED]PrimarOnkolog.pdf",
            en_description="AbdomenScanOrderDrPorsok",
        )
        assert result == "20260130_ErikaFusekova_NOU_Imaging_AbdomenScanOrderDrPorsok.pdf"

    def test_usg_category_token(self):
        result = rename_to_standard(
            "20260129 ErikaFusekova-BoryNemocnica-USGMudrTulenkova.pdf",
            en_description="AbdomenUltrasoundDrTulenkova",
        )
        assert result == (
            "20260129_ErikaFusekova_BoryNemocnica_Imaging_AbdomenUltrasoundDrTulenkova.pdf"
        )

    def test_discharge(self):
        result = rename_to_standard(
            "20260122 ErikaFusekova-BoryNemocnica-LekarskaPrepustaciaSpravaOperacia.pdf",
            en_description="DischargeSummaryAfterSurgery",
        )
        assert result == (
            "20260122_ErikaFusekova_BoryNemocnica_Discharge_DischargeSummaryAfterSurgery.pdf"
        )

    def test_keeps_original_description_if_no_en(self):
        result = rename_to_standard("20260226 ErikaFusekova-NOU-GenetikaMudrMalejcikova1z2.JPG")
        assert result == "20260226_ErikaFusekova_NOU_Genetics_GenetikaMudrMalejcikova1z2.JPG"

    def test_chemo_sheet(self):
        result = rename_to_standard(
            "20260213 ErikaFusekova-NOU-ChemoterapeutickyProtokolFOLFOX.pdf",
            en_description="FOLFOXChemotherapyProtocol",
        )
        assert result == "20260213_ErikaFusekova_NOU_ChemoSheet_FOLFOXChemotherapyProtocol.pdf"

    def test_no_institution_uses_unknown(self):
        result = rename_to_standard(
            "20260301 ErikaFusekova-ReferencneMaterialyKRAS.pdf",
            en_description="KRASReferenceMaterials",
        )
        # When no institution parsed, uses "Unknown"
        assert "_Unknown_" in result or "_Reference_" in result


class TestCorruptedFilename:
    """Tests for is_corrupted_filename — detecting broken filenames."""

    def test_normal_filename_not_corrupted(self):
        assert is_corrupted_filename("20260227_ErikaFusekova_NOU_Labs_Blood.pdf") is False

    def test_repeating_pattern_corrupted(self):
        """3+ repetitions of patient name = corrupted."""
        fn = "ErikaFusekova-Report-" + "ErikaFusekova " * 50 + "CRC.pdf"
        assert is_corrupted_filename(fn) is True

    def test_excessive_length_corrupted(self):
        """Filenames > 255 chars are corrupted."""
        fn = "20260227_" + "A" * 250 + ".pdf"
        assert is_corrupted_filename(fn) is True

    def test_two_repetitions_not_corrupted(self):
        """Only 2 occurrences is okay (filename + description)."""
        fn = "20260227_ErikaFusekova_NOU_Report_ErikaFusekovaConsult.pdf"
        assert is_corrupted_filename(fn) is False

    def test_real_corrupted_filename(self):
        """Real corrupted filename from production."""
        fn = (
            "ErikaFusekova-Report-ErikaFusekova ErikaFusekova "
            "ErikaFusekova ErikaFusekova ErikaFusekova Erika "
            "Fusekova CRC Baseline 2026.pdf"
        )
        assert is_corrupted_filename(fn) is True
