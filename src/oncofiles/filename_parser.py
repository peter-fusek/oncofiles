"""Parse medical document filenames following the naming convention.

Standard format (v3.15+): YYYYMMDD_PatientName_Institution_Category_DescriptionEN.ext
Examples:
    20260227_PatientName_NOU_Labs_BloodResultsBeforeCycle2DrPorsok.pdf
    20260122_PatientName_BoryNemocnica_Discharge_DischargeSummaryAfterSurgery.pdf

Bilingual format (v3.5-3.14): YYYYMMDD PatientName-Institution-Category-SKDescription.ext
Legacy format (still supported): YYYYMMDD_institution_category_description.ext
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import date
from pathlib import PurePosixPath

from oncofiles.models import DocumentCategory, ParsedFilename

logger = logging.getLogger(__name__)

# Known institution codes (expandable)
KNOWN_INSTITUTIONS = {
    # Real institutions from Google Drive
    "NOU",
    "BoryNemocnica",
    "SocialnaPoistovna",
    "MinnesotaUniversity",
    "PacientAdvokat",
    # Legacy / planned
    "NOUonko",
    "OUSA",
    "UNB",
    "VFN",
    "UNLP",
    "FN",
    "SZU",
    "Agel",
    "Medirex",
    "Alpha",
    "Synlab",
    "Cytopathos",
    "BIOPTIKA",
    # General healthcare providers
    "ProCare",
    "Medante",
    "Euromedic",
    "ISCare",
    "SvMichal",
    "Kramarska",
}

# Institution normalization: variant → canonical code
# Used to resolve institutions from OCR text, filenames, metadata
INSTITUTION_NORMALIZE: dict[str, str] = {
    "nou": "NOU",
    "noú": "NOU",
    "národný onkologický ústav": "NOU",
    "narodny onkologicky ustav": "NOU",
    "national oncology institute": "NOU",
    "onkologicky ustav": "NOU",
    "nou bratislava": "NOU",
    "bory": "BoryNemocnica",
    "bory nemocnica": "BoryNemocnica",
    "nemocnica bory": "BoryNemocnica",
    "socialna poistovna": "SocialnaPoistovna",
    "socialnapoistovna": "SocialnaPoistovna",
    "minnesota": "MinnesotaUniversity",
    "university of minnesota": "MinnesotaUniversity",
    "pacient advokat": "PacientAdvokat",
    "pacientadvokat": "PacientAdvokat",
    "ousa": "OUSA",
    "unb": "UNB",
    "medirex": "Medirex",
    "alpha": "Alpha",
    "synlab": "Synlab",
    "bioptika": "BIOPTIKA",
    "procare": "ProCare",
    "pro care": "ProCare",
    "medante": "Medante",
    "euromedic": "Euromedic",
    "iscare": "ISCare",
    "sv michala": "SvMichal",
    "nemocnica sv. michala": "SvMichal",
    "kramarska": "Kramarska",
    "kramárska": "Kramarska",
}


def normalize_institution(raw: str | None) -> str | None:
    """Normalize an institution name to canonical code.

    Returns canonical institution code or None if unrecognized.
    """
    if not raw:
        return None
    # Already a known code
    if raw in KNOWN_INSTITUTIONS:
        return raw
    # Lookup in normalization map (case-insensitive)
    return INSTITUTION_NORMALIZE.get(raw.lower().strip())


# Standard format: category → filename token (CamelCase, no spaces)
CATEGORY_FILENAME_TOKENS: dict[DocumentCategory, str] = {
    DocumentCategory.LABS: "Labs",
    DocumentCategory.REPORT: "Report",
    DocumentCategory.PATHOLOGY: "Pathology",
    DocumentCategory.IMAGING: "Imaging",
    DocumentCategory.GENETICS: "Genetics",
    DocumentCategory.HEREDITARY_GENETICS: "HereditaryGenetics",
    DocumentCategory.SURGERY: "Surgery",
    DocumentCategory.SURGICAL_REPORT: "SurgicalReport",  # legacy alias
    DocumentCategory.CONSULTATION: "Consultation",
    DocumentCategory.PRESCRIPTION: "Prescription",
    DocumentCategory.REFERRAL: "Referral",
    DocumentCategory.DISCHARGE: "Discharge",
    DocumentCategory.DISCHARGE_SUMMARY: "DischargeSummary",
    DocumentCategory.CHEMO_SHEET: "ChemoSheet",
    DocumentCategory.REFERENCE: "Reference",
    DocumentCategory.ADVOCATE: "Advocate",
    DocumentCategory.OTHER: "Other",
    DocumentCategory.VACCINATION: "Vaccination",
    DocumentCategory.DENTAL: "Dental",
    DocumentCategory.PREVENTIVE: "Preventive",
}

# Reverse lookup: filename token (lowercase) → category
_TOKEN_TO_CATEGORY: dict[str, DocumentCategory] = {
    token.lower(): cat for cat, token in CATEGORY_FILENAME_TOKENS.items()
}
# Legacy tokens that still parse to imaging (backward compat for existing filenames)
_TOKEN_TO_CATEGORY["ct"] = DocumentCategory.IMAGING
_TOKEN_TO_CATEGORY["usg"] = DocumentCategory.IMAGING

# Category inference from CamelCase description keywords.
# Order matters — more specific matches first.
_CATEGORY_KEYWORDS: list[tuple[list[str], DocumentCategory]] = [
    # Vaccination (before consultation — more specific)
    (
        ["vakcinaci", "ockovani", "vaccination", "vaccine", "vakcina", "ockovac"],
        DocumentCategory.VACCINATION,
    ),
    # Dental (before consultation — distinct specialty)
    (["dental", "zubny", "zubna", "zubne", "stomatolog", "orthodon"], DocumentCategory.DENTAL),
    # Preventive care (before consultation — distinct purpose)
    (
        ["preventiv", "screening", "annual checkup", "rocna prehliadka", "prehliadka"],
        DocumentCategory.PREVENTIVE,
    ),
    # Hereditary / germline genetics (before plain genetics — more specific)
    (
        [
            "hereditary",
            "germline",
            "germinaln",
            "dedicn",
            "zarodocn",
            "zárodočn",
            "brca1",
            "brca2",
            "lynch",
            "li-fraumeni",
            "cascade test",
            "kaskadove",
            "kaskadové",
            "inherited dna",
            "familial cancer",
        ],
        DocumentCategory.HEREDITARY_GENETICS,
    ),
    # Genetics (before pathology — genetics is a separate category)
    (["genetick", "genetik", "geneticke"], DocumentCategory.GENETICS),
    # Pathology / biopsy
    (["biopsia", "biopsii"], DocumentCategory.PATHOLOGY),
    # Chemo sheets
    (
        ["chemoterapeuticky", "chemosheet", "chemo_sheet", "protokol chemo"],
        DocumentCategory.CHEMO_SHEET,
    ),
    # Labs / blood work (before report — lab results may contain "vysledky")
    (["labvysledky", "lab vysledky", "krvpred", "krvny"], DocumentCategory.LABS),
    # Imaging subtypes (all route to generic imaging)
    (["usg", "sono", "ultrazvuk"], DocumentCategory.IMAGING),
    # Discharge summaries (before report — they contain "sprava")
    (["prepustaci", "prepusten", "epikryza"], DocumentCategory.DISCHARGE),
    # Surgical reports (before generic surgery)
    (["operacna sprava", "operacnyprotokol"], DocumentCategory.SURGICAL_REPORT),
    # Consultation / doctor visits (before report — more specific)
    (
        ["konzultaci", "kontrola", "vysetren", "prijem", "ambulan"],
        DocumentCategory.CONSULTATION,
    ),
    # Report (broad — remaining reports that aren't consultations)
    (
        ["sprava", "anamneza", "priebezn"],
        DocumentCategory.REPORT,
    ),
    # Prescription / referral
    (["recept", "prescription"], DocumentCategory.PRESCRIPTION),
    (["odporucanie", "ziadanka", "referral"], DocumentCategory.REFERRAL),
    # Surgery
    (["operacia"], DocumentCategory.SURGERY),
    # Reference materials
    (["referencn", "informacn"], DocumentCategory.REFERENCE),
    # Patient advocate notes
    (["advokat", "advocate", "pacientadvokat"], DocumentCategory.ADVOCATE),
]

# Legacy: map explicit category tokens to categories (underscore-separated format)
CATEGORY_ALIASES: dict[str, DocumentCategory] = {
    "labs": DocumentCategory.LABS,
    "lab": DocumentCategory.LABS,
    "blood": DocumentCategory.LABS,
    "krv": DocumentCategory.LABS,
    "report": DocumentCategory.REPORT,
    "sprava": DocumentCategory.REPORT,
    "imaging": DocumentCategory.IMAGING,
    "imaging_ct": DocumentCategory.IMAGING,
    "ct": DocumentCategory.IMAGING,
    "imaging_us": DocumentCategory.IMAGING,
    "mri": DocumentCategory.IMAGING,
    "pet": DocumentCategory.IMAGING,
    "rtg": DocumentCategory.IMAGING,
    "usg": DocumentCategory.IMAGING,
    "sono": DocumentCategory.IMAGING,
    "pathology": DocumentCategory.PATHOLOGY,
    "histo": DocumentCategory.PATHOLOGY,
    "biopsia": DocumentCategory.PATHOLOGY,
    "genetics": DocumentCategory.GENETICS,
    "genetika": DocumentCategory.GENETICS,
    "surgery": DocumentCategory.SURGERY,
    "operacia": DocumentCategory.SURGERY,
    "surgical_report": DocumentCategory.SURGICAL_REPORT,
    "prescription": DocumentCategory.PRESCRIPTION,
    "recept": DocumentCategory.PRESCRIPTION,
    "referral": DocumentCategory.REFERRAL,
    "odporucanie": DocumentCategory.REFERRAL,
    "ziadanka": DocumentCategory.REFERRAL,
    "consultation": DocumentCategory.CONSULTATION,
    "konzultacia": DocumentCategory.CONSULTATION,
    "kontrola": DocumentCategory.CONSULTATION,
    "vysetrenie": DocumentCategory.CONSULTATION,
    "discharge": DocumentCategory.DISCHARGE,
    "discharge_summary": DocumentCategory.DISCHARGE,  # merged
    "prepustenie": DocumentCategory.DISCHARGE,
    "epikryza": DocumentCategory.DISCHARGE,
    "chemo_sheet": DocumentCategory.CHEMO_SHEET,
    "reference": DocumentCategory.REFERENCE,
    "referencne": DocumentCategory.REFERENCE,
    "advocate": DocumentCategory.ADVOCATE,
    "advokat": DocumentCategory.ADVOCATE,
    "vaccination": DocumentCategory.VACCINATION,
    "vaccine": DocumentCategory.VACCINATION,
    "ockovanie": DocumentCategory.VACCINATION,
    "dental": DocumentCategory.DENTAL,
    "zubne": DocumentCategory.DENTAL,
    "stomatologia": DocumentCategory.DENTAL,
    "preventive": DocumentCategory.PREVENTIVE,
    "screening": DocumentCategory.PREVENTIVE,
    "prehliadka": DocumentCategory.PREVENTIVE,
}

_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")
_DATE_XX_RE = re.compile(r"^(\d{4})(\d{2})(xx)", re.IGNORECASE)
# Dashed ISO date: YYYY-M-D, YYYY-MM-DD, YYYY-MM-D, etc. Consumed inclusive of
# trailing separator so callers can consistently take `stem[match.end():]`.
# Mattias's hospital chart uses this: "2025-09-19_kontrola Gonsorcikova.pdf".
_DATE_DASH_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?=[_\- ]|$)")


def _match_any_date(stem: str) -> tuple[date, int] | None:
    """Try all three date patterns. Returns (parsed_date, chars_consumed) or None.

    Used by all three parse_* paths so dashed-date filenames like Mattias's
    2025-09-19_kontrola*.pdf work wherever the 8-digit YYYYMMDD path works.
    """
    m = _DATE_RE.match(stem)
    if m:
        with contextlib.suppress(ValueError):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), 8
    xx = _DATE_XX_RE.match(stem)
    if xx:
        with contextlib.suppress(ValueError):
            return date(int(xx.group(1)), int(xx.group(2)), 1), 8
    dash = _DATE_DASH_RE.match(stem)
    if dash:
        with contextlib.suppress(ValueError):
            return date(int(dash.group(1)), int(dash.group(2)), int(dash.group(3))), dash.end()
    return None


_cached_patient_re: dict[str, re.Pattern] = {}


def _patient_prefix_re(patient_id: str = "") -> re.Pattern:
    """Build patient prefix regex from patient context name (cached per patient)."""
    if patient_id in _cached_patient_re:
        return _cached_patient_re[patient_id]

    from oncofiles.patient_context import get_patient_name

    name = get_patient_name(patient_id)
    if name:
        compact = name.replace(" ", "")
        pattern = re.compile(rf"^{re.escape(compact)}[-]?", re.IGNORECASE)
    else:
        pattern = re.compile(r"^Patient[-]?", re.IGNORECASE)
    _cached_patient_re[patient_id] = pattern
    return pattern


# Bilingual format: EN category prefix already present
_BILINGUAL_PREFIX_RE = re.compile(
    r"^(labs|report|pathology|imaging|genetics|surgery|"
    r"surgical_report|prescription|referral|discharge_summary|discharge|"
    r"chemo_sheet|reference|advocate|other|vaccination|dental|preventive)-",
    re.IGNORECASE,
)


def _infer_category(description: str) -> DocumentCategory:
    """Infer document category from description keywords."""
    lower = description.lower()

    # Check if description starts with lab/krv prefix
    if lower.startswith("lab") or lower.startswith("krv"):
        return DocumentCategory.LABS

    # Check if description starts with imaging prefix
    if lower.startswith("ct"):
        return DocumentCategory.IMAGING
    if lower.startswith("mri") or lower.startswith("pet"):
        return DocumentCategory.IMAGING
    if lower.startswith("rtg"):
        return DocumentCategory.IMAGING

    # Check keyword groups in priority order
    for keywords, category in _CATEGORY_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return category

    return DocumentCategory.OTHER


def _parse_standard_format(stem: str, ext: str, patient_id: str = "") -> ParsedFilename | None:
    """Try to parse as: YYYYMMDD_PatientName_Institution_Category_Description."""
    result = ParsedFilename(extension=ext)

    # Extract date (YYYYMMDD, YYYYMMXX, or YYYY-MM-DD)
    date_result = _match_any_date(stem)
    if not date_result:
        return None

    result.document_date, consumed = date_result
    remaining = stem[consumed:].lstrip("_")

    if not remaining:
        return result

    # Split on underscores
    parts = remaining.split("_")
    if len(parts) < 2:
        return None  # Need at least patient name + something

    # First part should be patient name (e.g. "ErikaFusekova")
    from oncofiles.patient_context import get_patient_name

    patient_name_compact = get_patient_name(patient_id).replace(" ", "") or "Patient"
    if parts[0].lower() != patient_name_compact.lower():
        return None  # Not standard format

    if len(parts) < 3:
        return result

    # Second part: institution
    result.institution = parts[1]

    if len(parts) == 3:
        # YYYYMMDD_Patient_Institution_X — X could be category token or description
        token = parts[2].lower()
        cat = _TOKEN_TO_CATEGORY.get(token)
        if cat:
            result.category = cat
        else:
            result.description = parts[2]
            result.category = _infer_category(parts[2])
        return result

    # Third part: category token
    cat_token = parts[2].lower()
    cat = _TOKEN_TO_CATEGORY.get(cat_token)
    if cat:
        result.category = cat
        # Everything after category is the description
        if len(parts) > 3:
            result.description = "_".join(parts[3:])
    else:
        # Third part is not a known category token — treat as part of description
        result.description = "_".join(parts[2:])
        result.category = _infer_category(result.description)

    return result


def _parse_new_format(stem: str, ext: str, patient_id: str = "") -> ParsedFilename | None:
    """Try to parse as: YYYYMMDD ErikaFusekova-Institution-Description."""
    result = ParsedFilename(extension=ext)

    # Extract date (YYYYMMDD, YYYYMMXX, or YYYY-MM-DD)
    date_result = _match_any_date(stem)
    if not date_result:
        return None  # No date prefix → not new format
    result.document_date, consumed = date_result
    remaining = stem[consumed:].lstrip(" _-")

    # Strip patient prefix
    remaining = _patient_prefix_re(patient_id).sub("", remaining).lstrip("-")

    if not remaining:
        return result

    # Split on first dash to get institution, then description on second dash
    parts = remaining.split("-", 2)

    if len(parts) >= 2:
        # Check if first part is a known institution
        inst_candidate = parts[0]
        for inst in KNOWN_INSTITUTIONS:
            if inst_candidate.lower() == inst.lower():
                result.institution = inst
                break
        if not result.institution:
            # Accept unknown institutions too — just store what we found
            result.institution = inst_candidate

        if len(parts) >= 3:
            desc = parts[2]
        elif len(parts) == 2:
            desc = parts[1]
        else:
            desc = ""

        if desc:
            result.description = desc
            result.category = _infer_category(desc)
    else:
        result.description = parts[0]
        result.category = _infer_category(parts[0])

    return result


def parse_filename(filename: str, patient_id: str = "") -> ParsedFilename:
    """Parse a filename into structured metadata.

    Tries formats in order:
    1. Standard (v3.15+): YYYYMMDD_PatientName_Institution_Category_Description
    2. Bilingual (v3.5-3.14): YYYYMMDD PatientName-Institution-Description
    3. Legacy: YYYYMMDD_institution_category_description
    """
    stem = PurePosixPath(filename).stem
    ext = PurePosixPath(filename).suffix.lstrip(".")

    from oncofiles.patient_context import get_patient_name

    patient_name_compact = get_patient_name(patient_id).replace(" ", "") or "Patient"

    # Try standard format first: YYYYMMDD_PatientName_Institution_Category_Description
    if f"_{patient_name_compact}_" in stem or stem.startswith(f"{patient_name_compact}_"):
        result = _parse_standard_format(stem, ext, patient_id=patient_id)
        if result:
            return result

    # Try bilingual format: YYYYMMDD PatientName-Institution-Description
    if " " in stem and patient_name_compact.lower() in stem.lower():
        result = _parse_new_format(stem, ext, patient_id=patient_id)
        if result:
            return result

    # Legacy format: YYYYMMDD_institution_category_description.ext
    result = ParsedFilename(extension=ext)

    # Try to extract leading date (YYYYMMDD, YYYYMMXX, or YYYY-MM-DD)
    date_result = _match_any_date(stem)
    if date_result:
        result.document_date, consumed = date_result
        stem = stem[consumed:].lstrip("_- ")

    # Split remaining parts on underscores, hyphens, AND whitespace. Whitespace
    # matters because Mattias's hospital files look like "kontrola Gonsorcikova"
    # — token-level category alias lookup would miss "kontrola" otherwise (#439).
    parts = [p for p in re.split(r"[_\-\s]+", stem) if p]

    if not parts:
        return result

    # Try to match institution from first part
    for i, part in enumerate(parts):
        for inst in KNOWN_INSTITUTIONS:
            if part.lower() == inst.lower():
                result.institution = inst
                parts = parts[:i] + parts[i + 1 :]
                break
        if result.institution:
            break

    if not parts:
        return result

    # Try to match category from remaining parts (exact ALIAS hit)
    for i, part in enumerate(parts):
        cat = CATEGORY_ALIASES.get(part.lower())
        if cat:
            result.category = cat
            parts = parts[:i] + parts[i + 1 :]
            break

    # Remaining parts become the description
    if parts:
        result.description = " ".join(parts)

    # If no category matched the ALIAS map, fall back to keyword inference on
    # the full description. Catches Slovak variants like "kontrolny" that the
    # exact-match ALIAS doesn't cover but _CATEGORY_KEYWORDS does.
    if result.category == DocumentCategory.OTHER and result.description:
        result.category = _infer_category(result.description)

    return result


def rename_to_standard(
    filename: str,
    category: DocumentCategory | str | None = None,
    en_description: str | None = None,
    patient_id: str = "",
    institution_override: str | None = None,
) -> str:
    """Rename a filename to the standard format.

    Transforms any format to: YYYYMMDD_PatientName_Institution_Category_Description.ext

    Args:
        filename: Current filename in any supported format.
        category: Category override. If None, infers from filename.
        en_description: English CamelCase description. If None, keeps existing description.
        institution_override: Use this institution instead of parsing from filename.

    Returns the new filename in standard format (or unchanged if can't be parsed).
    """
    path = PurePosixPath(filename)
    ext = path.suffix  # includes the dot

    parsed = parse_filename(filename)
    cat = DocumentCategory(category) if category else parsed.category
    cat_token = CATEGORY_FILENAME_TOKENS.get(cat, "Other")

    # Can't rename without at least a date
    if not parsed.document_date:
        return filename

    from oncofiles.patient_context import get_patient_name

    patient_compact = get_patient_name(patient_id).replace(" ", "") or "Patient"

    # Use provided EN description, or fall back to existing description
    desc = en_description or parsed.description or ""

    # Clean description: remove bilingual prefix if present
    # e.g. "Labs-LabVysledky..." → "LabVysledky..."
    if desc and "-" in desc:
        prefix_part = desc.split("-", 1)[0].lower()
        if prefix_part in _TOKEN_TO_CATEGORY:
            desc = desc.split("-", 1)[1]

    # Build standard filename
    date_str = parsed.document_date.strftime("%Y%m%d")
    # Use override (from DB) if provided; otherwise try to normalize from filename
    institution = (
        institution_override or normalize_institution(parsed.institution) or parsed.institution
    )
    if not institution or institution == "Unknown":
        institution = "Unknown"
        logger.warning(
            "Institution unknown for %s — consider re-extracting",
            filename,
        )

    parts = [date_str, patient_compact, institution, cat_token]
    if desc:
        parts.append(desc)

    new_stem = "_".join(parts)

    # Check if already in standard format with same components
    existing_standard = f"{new_stem}{ext}"
    if filename == existing_standard:
        return filename

    return f"{new_stem}{ext}"


def is_standard_format(filename: str, patient_id: str = "") -> bool:
    """Check if a filename is already in the standard format."""
    stem = PurePosixPath(filename).stem
    from oncofiles.patient_context import get_patient_name

    patient_name_compact = get_patient_name(patient_id).replace(" ", "") or "Patient"

    # Standard format: YYYYMMDD_PatientName_Institution_CategoryToken_Description
    if not _DATE_RE.match(stem):
        return False

    remaining = stem[8:]
    if not remaining.startswith(f"_{patient_name_compact}_"):
        return False

    # Check that there's a valid category token
    after_patient = remaining[len(f"_{patient_name_compact}_") :]
    parts = after_patient.split("_", 2)  # institution, category, description
    if len(parts) < 2:
        return False

    # Reject "Unknown" institution — needs re-rename with actual institution (#280)
    if parts[0] == "Unknown":
        return False

    return parts[1].lower() in _TOKEN_TO_CATEGORY


def is_corrupted_filename(filename: str, patient_id: str = "") -> bool:
    """Detect corrupted filenames (repeating patterns, excessive length)."""
    stem = PurePosixPath(filename).stem

    if len(filename) > 255:
        return True

    from oncofiles.patient_context import get_patient_name

    patient = get_patient_name(patient_id).replace(" ", "") or "Patient"
    return stem.count(patient) >= 3


def rename_to_bilingual(filename: str, category: DocumentCategory | str | None = None) -> str:
    """Add EN category prefix to a filename for bilingual display.

    .. deprecated:: 3.15.0
        Use :func:`rename_to_standard` instead.

    Transforms:
        20260227 ErikaFusekova-NOU-LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf
    →   20260227 ErikaFusekova-NOU-Labs-LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf

    If the filename already has a bilingual prefix, returns it unchanged.
    If category is not provided, infers it from the filename.

    Returns the new filename (or unchanged if already bilingual or can't be parsed).
    """
    path = PurePosixPath(filename)
    stem = path.stem
    ext = path.suffix  # includes the dot

    # Already has bilingual prefix?
    if _BILINGUAL_PREFIX_RE.search(stem.split("-")[-1] if "-" in stem else stem):
        # Check the description part after institution
        parsed = parse_filename(filename)
        if parsed.description and _BILINGUAL_PREFIX_RE.match(parsed.description):
            return filename

    # Parse to get components
    parsed = parse_filename(filename)
    cat = DocumentCategory(category) if category else parsed.category

    # Can't add prefix to files without a description
    if not parsed.description:
        return filename

    # Check if description already starts with the category prefix
    cat_prefix = CATEGORY_FILENAME_TOKENS.get(cat, cat.value.capitalize())

    if parsed.description.lower().startswith(cat_prefix.lower() + "-"):
        return filename

    # Build new filename
    new_desc = f"{cat_prefix}-{parsed.description}"

    # Reconstruct: YYYYMMDD PatientName-Institution-Category-Description.ext
    from oncofiles.patient_context import get_patient_name

    patient_compact = get_patient_name().replace(" ", "") or "Patient"
    parts = []
    if parsed.document_date:
        parts.append(parsed.document_date.strftime("%Y%m%d"))

    name_parts = []
    if parsed.institution:
        name_parts.append(f"{patient_compact}-{parsed.institution}-{new_desc}")
    else:
        name_parts.append(f"{patient_compact}-{new_desc}")

    new_stem = f"{parts[0]} {name_parts[0]}" if parts else name_parts[0]
    return f"{new_stem}{ext}"
