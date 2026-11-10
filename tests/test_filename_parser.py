"""Tests for filename parser — all naming conventions.

Standard format (v3.15+): YYYYMMDD_JanaTestova_Institution_Category_DescriptionEN.ext
Bilingual format (v3.5-3.14): YYYYMMDD JanaTestova-Institution-Category-SKDescription.ext
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
    """Tests for the real YYYYMMDD JanaTestova-... naming convention."""

    def test_lab_results(self):
        fn = "20300227 JanaTestova-NOU-LabVysledkyPred2chemoDrNovak.pdf"
        r = parse_filename(fn)
        assert r.document_date == date(2030, 2, 27)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.LABS
        assert r.description == "LabVysledkyPred2chemoDrNovak"
        assert r.extension == "pdf"

    def test_discharge_summary(self):
        r = parse_filename(
            "20300122 JanaTestova-BoryNemocnica-LekarskaPrepustaciaSpravaOperacia.pdf"
        )
        assert r.document_date == date(2030, 1, 22)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.DISCHARGE
        assert r.description == "LekarskaPrepustaciaSpravaOperacia"

    def test_biopsy_jpg(self):
        r = parse_filename("20300127 JanaTestova-BoryNemocnica-BiopsiaMudrRychly.JPG")
        assert r.document_date == date(2030, 1, 27)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.PATHOLOGY
        assert r.extension == "JPG"

    def test_imaging_usg(self):
        r = parse_filename("20300129 JanaTestova-BoryNemocnica-USGMudrTulenkova.pdf")
        assert r.document_date == date(2030, 1, 29)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.IMAGING

    def test_imaging_ct(self):
        r = parse_filename("20300130 JanaTestova-NOU-CTobjednavkaDrNovakPrimarOnkolog.pdf")
        assert r.document_date == date(2030, 1, 30)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.IMAGING

    def test_report_sprava(self):
        r = parse_filename("20300130 JanaTestova-NOU-SpravaDrNovakPrimarOnkolog.pdf")
        assert r.document_date == date(2030, 1, 30)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.REPORT

    def test_report_anamneza(self):
        r = parse_filename(
            "20291201 JanaTestova-BoryNemocnica-AnamnezaMudrDanihelPrimarChirurg.pdf"
        )
        assert r.document_date == date(2029, 12, 1)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.REPORT

    def test_genetic_pathology(self):
        r = parse_filename("20300226 JanaTestova-NOU-GenetikaMudrMalejcikova1z2.JPG")
        assert r.document_date == date(2030, 2, 26)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.GENETICS

    def test_genetic_report_is_genetics(self):
        r = parse_filename("20300212 JanaTestova-NOU-SpravaZgenetickehoVysetreniaMudrCernak.JPG")
        assert r.category == DocumentCategory.GENETICS

    def test_hereditary_genetics_standard_token(self):
        r = parse_filename("20300415_JanaTestova_NOU_HereditaryGenetics_BRCA1Positive.pdf")
        assert r.document_date == date(2030, 4, 15)
        assert r.category == DocumentCategory.HEREDITARY_GENETICS

    def test_hereditary_genetics_brca_keyword(self):
        r = parse_filename("20300415 JanaTestova-NOU-BRCA2PathogenicVariant.pdf")
        assert r.category == DocumentCategory.HEREDITARY_GENETICS

    def test_hereditary_genetics_germline_keyword(self):
        r = parse_filename("20300415 JanaTestova-Ambry-GermlinePanelNegative.pdf")
        assert r.category == DocumentCategory.HEREDITARY_GENETICS

    def test_hereditary_genetics_dedicna_sk(self):
        r = parse_filename("20300415 JanaTestova-NOU-DedicnaMutaciaBRCA1.pdf")
        assert r.category == DocumentCategory.HEREDITARY_GENETICS

    def test_blood_krv_is_labs(self):
        r = parse_filename("20300213 JanaTestova-NOU-KrvPredChemoMudrPittichova.JPG")
        assert r.category == DocumentCategory.LABS

    def test_krv_page_labs(self):
        r = parse_filename("20300227 JanaTestova-NOU-KRV1z2SpravaPredChemoDrNovak.JPG")
        assert r.category == DocumentCategory.LABS

    def test_priebezna_sprava_report(self):
        r = parse_filename("20300216 JanaTestova-NOU-PriebeznaSpravaZhospitalizaciePo1chemo.pdf")
        assert r.category == DocumentCategory.REPORT

    def test_prijem_consultation(self):
        r = parse_filename("20300213 JanaTestova-NOU-PrijemNaChemoDrNovak.JPG")
        assert r.category == DocumentCategory.CONSULTATION

    def test_konzultacia_consultation(self):
        r = parse_filename("20300220 JanaTestova-NOU-KonzultaciaBioLiecbyPo1chemoDrNovak.JPG")
        assert r.category == DocumentCategory.CONSULTATION

    def test_advocate_institution(self):
        r = parse_filename(
            "20290127 JanaTestova-PacientAdvokat-Other-SumarPrepustacejSpravyPoOperacii.md"
        )
        assert r.institution == "PacientAdvokat"
        assert r.document_date == date(2029, 1, 27)

    def test_socialna_poistovna(self):
        r = parse_filename("20300209 JanaTestova-SocialnaPoistovna-UznanieNemocenskeho.pdf")
        assert r.institution == "SocialnaPoistovna"
        assert r.category == DocumentCategory.OTHER

    def test_minnesota_university(self):
        r = parse_filename("20300220 JanaTestova-MinnesotaUniversity-SecondOpinion.gdoc")
        assert r.institution == "MinnesotaUniversity"
        assert r.extension == "gdoc"

    def test_pn_admin_is_other(self):
        r = parse_filename("20300128 JanaTestova-BoryNemocnica-UkonceniePapierovejPNChirurg.JPG")
        assert r.category == DocumentCategory.OTHER

    def test_xx_date(self):
        r = parse_filename("203002xx JanaTestova-BoryNemocnica-PNkaPreSocialnaPoistovnaOprava.pdf")
        assert r.document_date == date(2030, 2, 1)  # first of month for xx
        assert r.institution == "BoryNemocnica"

    def test_no_extension(self):
        r = parse_filename("20300227 JanaTestova-NOU-GenetickeVysetrenie")
        assert r.document_date == date(2030, 2, 27)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.GENETICS
        assert r.extension == ""

    def test_nursing_discharge(self):
        r = parse_filename(
            "20300122 JanaTestova-BoryNemocnica-OsetrovatelskaPrepustaciaSpravaOperacia.pdf"
        )
        assert r.category == DocumentCategory.DISCHARGE

    def test_vysetrenie_consultation(self):
        r = parse_filename("20300129 JanaTestova-BoryNemocnica-PooperacneVysetrenieChirurg.pdf")
        assert r.category == DocumentCategory.CONSULTATION

    def test_chemo_sheet(self):
        r = parse_filename("20300213 JanaTestova-NOU-ChemoterapeutickyProtokolFOLFOX.pdf")
        assert r.category == DocumentCategory.CHEMO_SHEET

    def test_discharge_summary_legacy_alias(self):
        # Legacy parser splits on _ so "discharge_summary" → token "discharge" matched first
        r = parse_filename("20300122_BoryNemocnica_discharge_summary_operacia.pdf")
        assert r.category == DocumentCategory.DISCHARGE

    def test_surgical_report_alias(self):
        # Legacy parser splits on _ so tokens "surgical" + "report" (REPORT)
        r = parse_filename("20300118_BoryNemocnica_surgical_report_resection.pdf")
        assert r.category == DocumentCategory.REPORT

    def test_genetics_alias(self):
        r = parse_filename("20300226_NOU_genetics_KRAS.pdf")
        assert r.category == DocumentCategory.GENETICS

    def test_imaging_ct_alias(self):
        r = parse_filename("20300130_NOU_ct_abdomen.pdf")
        assert r.category == DocumentCategory.IMAGING

    def test_imaging_us_alias(self):
        r = parse_filename("20300129_BoryNemocnica_usg_abdomen.pdf")
        assert r.category == DocumentCategory.IMAGING

    def test_reference_alias(self):
        r = parse_filename("20300301_NOU_reference_guidelines.pdf")
        assert r.category == DocumentCategory.REFERENCE

    def test_reference_keyword_infer(self):
        r = parse_filename("20300301 JanaTestova-NOU-ReferencneMaterialyKRAS.pdf")
        assert r.category == DocumentCategory.REFERENCE


# ── Legacy format tests ──────────────────────────────────────────────────────


class TestLegacyFormat:
    """Tests for the original YYYYMMDD_institution_category_description.ext format."""

    def test_full_convention(self):
        r = parse_filename("20280115_NOUonko_labs_krvnyObraz.pdf")
        assert r.document_date == date(2028, 1, 15)
        assert r.institution == "NOUonko"
        assert r.category == DocumentCategory.LABS
        assert r.description == "krvnyObraz"
        assert r.extension == "pdf"

    def test_different_institution(self):
        r = parse_filename("20280220_OUSA_report_kontrola.pdf")
        assert r.document_date == date(2028, 2, 20)
        assert r.institution == "OUSA"
        assert r.category == DocumentCategory.REPORT

    def test_no_date(self):
        r = parse_filename("NOUonko_labs_krvnyObraz.pdf")
        assert r.document_date is None
        assert r.institution == "NOUonko"
        assert r.category == DocumentCategory.LABS

    def test_date_only(self):
        r = parse_filename("20280115.pdf")
        assert r.document_date == date(2028, 1, 15)
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
        r = parse_filename("Jana Testová - 2030-02-20 15.51.29.jpg")
        assert r.extension == "jpg"

    def test_analyzy_strategic_plan(self):
        r = parse_filename("20300217 Jana_Strategicky_Rozhodovaci_Plan_mCRC_20300217_220735.pdf")
        assert r.document_date == date(2030, 2, 17)
        assert r.extension == "pdf"


# ── Bilingual rename (#57) ──────────────────────────────────────────────────


class TestBilingualRename:
    """Tests for rename_to_bilingual — adding EN category prefix."""

    def test_lab_results(self):
        result = rename_to_bilingual("20300227 JanaTestova-NOU-LabVysledkyPred2chemoDrNovak.pdf")
        assert result == "20300227 JanaTestova-NOU-Labs-LabVysledkyPred2chemoDrNovak.pdf"

    def test_discharge_summary(self):
        result = rename_to_bilingual(
            "20300122 JanaTestova-BoryNemocnica-LekarskaPrepustaciaSpravaOperacia.pdf"
        )
        assert result == (
            "20300122 JanaTestova-BoryNemocnica-Discharge-LekarskaPrepustaciaSpravaOperacia.pdf"
        )

    def test_biopsy(self):
        result = rename_to_bilingual("20300127 JanaTestova-BoryNemocnica-BiopsiaMudrRychly.JPG")
        assert result == "20300127 JanaTestova-BoryNemocnica-Pathology-BiopsiaMudrRychly.JPG"

    def test_genetics(self):
        result = rename_to_bilingual("20300226 JanaTestova-NOU-GenetikaMudrMalejcikova1z2.JPG")
        assert result == "20300226 JanaTestova-NOU-Genetics-GenetikaMudrMalejcikova1z2.JPG"

    def test_imaging_ct(self):
        result = rename_to_bilingual(
            "20300130 JanaTestova-NOU-CTobjednavkaDrNovakPrimarOnkolog.pdf"
        )
        assert result == ("20300130 JanaTestova-NOU-Imaging-CTobjednavkaDrNovakPrimarOnkolog.pdf")

    def test_imaging_usg(self):
        result = rename_to_bilingual("20300129 JanaTestova-BoryNemocnica-USGMudrTulenkova.pdf")
        assert result == ("20300129 JanaTestova-BoryNemocnica-Imaging-USGMudrTulenkova.pdf")

    def test_chemo_sheet(self):
        result = rename_to_bilingual("20300213 JanaTestova-NOU-ChemoterapeutickyProtokolFOLFOX.pdf")
        assert result == ("20300213 JanaTestova-NOU-ChemoSheet-ChemoterapeutickyProtokolFOLFOX.pdf")

    def test_report(self):
        result = rename_to_bilingual("20300130 JanaTestova-NOU-SpravaDrNovakPrimarOnkolog.pdf")
        assert result == ("20300130 JanaTestova-NOU-Report-SpravaDrNovakPrimarOnkolog.pdf")

    def test_other_category(self):
        result = rename_to_bilingual(
            "20300209 JanaTestova-SocialnaPoistovna-UznanieNemocenskeho.pdf"
        )
        assert result == ("20300209 JanaTestova-SocialnaPoistovna-Other-UznanieNemocenskeho.pdf")

    def test_already_bilingual_unchanged(self):
        """Already-renamed files should not be double-prefixed."""
        fn = "20300227 JanaTestova-NOU-Labs-LabVysledkyPred2chemoDrNovak.pdf"
        assert rename_to_bilingual(fn) == fn

    def test_explicit_category_override(self):
        result = rename_to_bilingual(
            "20300209 JanaTestova-SocialnaPoistovna-UznanieNemocenskeho.pdf",
            category="referral",
        )
        assert "Referral-" in result

    def test_reference_category(self):
        result = rename_to_bilingual(
            "20300301 JanaTestova-NOU-ReferencneMaterialyKRAS.pdf",
            category="reference",
        )
        assert "Reference-" in result

    def test_advocate_category(self):
        result = rename_to_bilingual(
            "20290127 JanaTestova-PacientAdvokat-Other-SumarPrepustacejSpravyPoOperacii.md",
            category="advocate",
        )
        assert "Advocate-" in result

    def test_no_description_unchanged(self):
        """Files without parseable description stay unchanged."""
        assert rename_to_bilingual("20280115.pdf") == "20280115.pdf"


# ── Standard format tests (v3.15+) ──────────────────────────────────────────


class TestStandardFormat:
    """Tests for YYYYMMDD_JanaTestova_Institution_Category_Description format."""

    def test_full_standard(self):
        r = parse_filename("20300227_JanaTestova_NOU_Labs_BloodResultsBeforeCycle2DrPorsok.pdf")
        assert r.document_date == date(2030, 2, 27)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.LABS
        assert r.description == "BloodResultsBeforeCycle2DrPorsok"
        assert r.extension == "pdf"

    def test_discharge(self):
        r = parse_filename(
            "20300122_JanaTestova_BoryNemocnica_Discharge_DischargeSummaryAfterSurgery.pdf"
        )
        assert r.document_date == date(2030, 1, 22)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.DISCHARGE

    def test_pathology(self):
        r = parse_filename("20300127_JanaTestova_BoryNemocnica_Pathology_BiopsyDrRychly.JPG")
        assert r.document_date == date(2030, 1, 27)
        assert r.institution == "BoryNemocnica"
        assert r.category == DocumentCategory.PATHOLOGY
        assert r.extension == "JPG"

    def test_genetics(self):
        r = parse_filename("20300226_JanaTestova_NOU_Genetics_KRASMutationAnalysis.JPG")
        assert r.document_date == date(2030, 2, 26)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.GENETICS

    def test_imaging_ct(self):
        r = parse_filename("20300130_JanaTestova_NOU_CT_AbdomenScanDrPorsok.pdf")
        assert r.document_date == date(2030, 1, 30)
        assert r.institution == "NOU"
        assert r.category == DocumentCategory.IMAGING

    def test_imaging_usg(self):
        r = parse_filename("20300129_JanaTestova_BoryNemocnica_USG_AbdomenDrTulenkova.pdf")
        assert r.category == DocumentCategory.IMAGING

    def test_chemo_sheet(self):
        r = parse_filename("20300213_JanaTestova_NOU_ChemoSheet_FOLFOXProtocol.pdf")
        assert r.category == DocumentCategory.CHEMO_SHEET

    def test_surgical_report(self):
        r = parse_filename("20300118_JanaTestova_BoryNemocnica_SurgicalReport_ColonResection.pdf")
        assert r.category == DocumentCategory.SURGICAL_REPORT

    def test_reference(self):
        r = parse_filename("20300301_JanaTestova_NOU_Reference_DeVitaCh40ColorectalCancer.pdf")
        assert r.category == DocumentCategory.REFERENCE

    def test_advocate(self):
        r = parse_filename("20290127_JanaTestova_PacientAdvokat_Advocate_SummaryAfterSurgery.md")
        assert r.institution == "PacientAdvokat"
        assert r.category == DocumentCategory.ADVOCATE

    def test_discharge_summary(self):
        r = parse_filename("20300122_JanaTestova_BoryNemocnica_DischargeSummary_PostOpCare.pdf")
        assert r.category == DocumentCategory.DISCHARGE_SUMMARY

    def test_description_with_underscores(self):
        r = parse_filename("20300227_JanaTestova_NOU_Labs_Blood_Results_Cycle2.pdf")
        assert r.category == DocumentCategory.LABS
        assert r.description == "Blood_Results_Cycle2"

    def test_no_description(self):
        r = parse_filename("20300227_JanaTestova_NOU_Labs.pdf")
        assert r.category == DocumentCategory.LABS
        assert r.institution == "NOU"
        assert r.description is None

    def test_unknown_category_token_infers(self):
        """If category token is not recognized, fall back to inference."""
        r = parse_filename("20300227_JanaTestova_NOU_LabVysledkyPred2chemo.pdf")
        assert r.category == DocumentCategory.LABS
        assert r.description == "LabVysledkyPred2chemo"

    def test_other_category(self):
        r = parse_filename("20300209_JanaTestova_SocialnaPoistovna_Other_SickLeaveConfirmation.pdf")
        assert r.category == DocumentCategory.OTHER
        assert r.institution == "SocialnaPoistovna"


class TestIsStandardFormat:
    """Tests for is_standard_format() detection."""

    def test_standard_format(self):
        assert is_standard_format("20300227_JanaTestova_NOU_Labs_BloodResults.pdf") is True

    def test_bilingual_format_not_standard(self):
        assert is_standard_format("20300227 JanaTestova-NOU-Labs-LabVysledky.pdf") is False

    def test_legacy_format_not_standard(self):
        assert is_standard_format("20300227_NOU_labs_krvnyObraz.pdf") is False

    def test_no_date_not_standard(self):
        assert is_standard_format("JanaTestova_NOU_Labs_Blood.pdf") is False


class TestRenameToStandard:
    """Tests for rename_to_standard — converting to standard format."""

    def test_from_bilingual(self):
        result = rename_to_standard("20300227 JanaTestova-NOU-LabVysledkyPred2chemoDrNovak.pdf")
        assert result == "20300227_JanaTestova_NOU_Labs_LabVysledkyPred2chemoDrNovak.pdf"

    def test_from_bilingual_with_en_description(self):
        result = rename_to_standard(
            "20300227 JanaTestova-NOU-LabVysledkyPred2chemoDrNovak.pdf",
            en_description="BloodResultsBeforeCycle2DrPorsok",
        )
        assert result == "20300227_JanaTestova_NOU_Labs_BloodResultsBeforeCycle2DrPorsok.pdf"

    def test_from_bilingual_with_category_override(self):
        result = rename_to_standard(
            "20300209 JanaTestova-SocialnaPoistovna-UznanieNemocenskeho.pdf",
            category="referral",
            en_description="SickLeaveConfirmation",
        )
        assert result == (
            "20300209_JanaTestova_SocialnaPoistovna_Referral_SickLeaveConfirmation.pdf"
        )

    def test_already_standard_unchanged(self):
        fn = "20300227_JanaTestova_NOU_Labs_BloodResultsBeforeCycle2DrPorsok.pdf"
        assert rename_to_standard(fn) == fn

    def test_from_bilingual_with_prefix(self):
        """Bilingual files with category prefix should strip it."""
        result = rename_to_standard("20300227 JanaTestova-NOU-Labs-LabVysledkyPred2chemo.pdf")
        assert result == "20300227_JanaTestova_NOU_Labs_LabVysledkyPred2chemo.pdf"

    def test_no_date_unchanged(self):
        assert rename_to_standard("random_document.pdf") == "random_document.pdf"

    def test_ct_category_token(self):
        result = rename_to_standard(
            "20300130 JanaTestova-NOU-CTobjednavkaDrNovakPrimarOnkolog.pdf",
            en_description="AbdomenScanOrderDrPorsok",
        )
        assert result == "20300130_JanaTestova_NOU_Imaging_AbdomenScanOrderDrPorsok.pdf"

    def test_usg_category_token(self):
        result = rename_to_standard(
            "20300129 JanaTestova-BoryNemocnica-USGMudrTulenkova.pdf",
            en_description="AbdomenUltrasoundDrTulenkova",
        )
        assert result == (
            "20300129_JanaTestova_BoryNemocnica_Imaging_AbdomenUltrasoundDrTulenkova.pdf"
        )

    def test_discharge(self):
        result = rename_to_standard(
            "20300122 JanaTestova-BoryNemocnica-LekarskaPrepustaciaSpravaOperacia.pdf",
            en_description="DischargeSummaryAfterSurgery",
        )
        assert result == (
            "20300122_JanaTestova_BoryNemocnica_Discharge_DischargeSummaryAfterSurgery.pdf"
        )

    def test_keeps_original_description_if_no_en(self):
        result = rename_to_standard("20300226 JanaTestova-NOU-GenetikaMudrMalejcikova1z2.JPG")
        assert result == "20300226_JanaTestova_NOU_Genetics_GenetikaMudrMalejcikova1z2.JPG"

    def test_chemo_sheet(self):
        result = rename_to_standard(
            "20300213 JanaTestova-NOU-ChemoterapeutickyProtokolFOLFOX.pdf",
            en_description="FOLFOXChemotherapyProtocol",
        )
        assert result == "20300213_JanaTestova_NOU_ChemoSheet_FOLFOXChemotherapyProtocol.pdf"

    def test_no_institution_returns_input_unchanged(self):
        """#404 Option A: when no institution can be resolved, return the
        input unchanged. Pre-#404 the renamer wrote ``_Unknown_`` back into
        the filename, which is_standard_format then rejected, creating an
        infinite oscillation in the dashboard's NÁZOV column."""
        src = "20300301 JanaTestova-ReferencneMaterialyKRAS.pdf"
        result = rename_to_standard(src, en_description="KRASReferenceMaterials")
        assert result == src
        assert "_Unknown_" not in result


class TestCorruptedFilename:
    """Tests for is_corrupted_filename — detecting broken filenames."""

    def test_normal_filename_not_corrupted(self):
        assert is_corrupted_filename("20300227_JanaTestova_NOU_Labs_Blood.pdf") is False

    def test_repeating_pattern_corrupted(self):
        """3+ repetitions of patient name = corrupted."""
        fn = "JanaTestova-Report-" + "JanaTestova " * 50 + "CRC.pdf"
        assert is_corrupted_filename(fn) is True

    def test_excessive_length_corrupted(self):
        """Filenames > 255 chars are corrupted."""
        fn = "20300227_" + "A" * 250 + ".pdf"
        assert is_corrupted_filename(fn) is True

    def test_two_repetitions_not_corrupted(self):
        """Only 2 occurrences is okay (filename + description)."""
        fn = "20300227_JanaTestova_NOU_Report_JanaTestovaConsult.pdf"
        assert is_corrupted_filename(fn) is False

    def test_real_corrupted_filename(self):
        """Real corrupted filename from production."""
        fn = (
            "JanaTestova-Report-JanaTestova JanaTestova "
            "JanaTestova JanaTestova JanaTestova Jana "
            "Testova CRC Baseline 2026.pdf"
        )
        assert is_corrupted_filename(fn) is True
