"""Parse medical document filenames following the real naming convention.

Primary format: YYYYMMDD ErikaFusekova-Institution-DescriptionDoctor.ext
Examples:
    20260227 ErikaFusekova-NOU-LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf
    20260122 ErikaFusekova-BoryNemocnica-LekarskaPrepustaciaSpravaOperacia.pdf
    20260127 ErikaFusekova-BoryNemocnica-BiopsiaMudrRychly.JPG
    202602xx ErikaFusekova-BoryNemocnica-PNkaPreSocialnaPoistovnaOprava.pdf

Legacy format (still supported): YYYYMMDD_institution_category_description.ext
"""

from __future__ import annotations

import contextlib
import re
from datetime import date
from pathlib import PurePosixPath

from oncofiles.models import DocumentCategory, ParsedFilename

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
}

# Category inference from CamelCase description keywords.
# Order matters — more specific matches first.
_CATEGORY_KEYWORDS: list[tuple[list[str], DocumentCategory]] = [
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
    # Imaging subtypes (before generic imaging)
    (["usg", "sono", "ultrazvuk"], DocumentCategory.IMAGING_US),
    # Discharge summaries (before report — they contain "sprava")
    (["prepustaci", "prepusten", "epikryza"], DocumentCategory.DISCHARGE),
    # Surgical reports (before generic surgery)
    (["operacna sprava", "operacnyprotokol"], DocumentCategory.SURGICAL_REPORT),
    # Report (broad — many things are reports)
    (
        ["sprava", "anamneza", "kontrola", "vysetren", "prijem", "konzultaci", "priebezn"],
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
    "kontrola": DocumentCategory.REPORT,
    "imaging": DocumentCategory.IMAGING,
    "imaging_ct": DocumentCategory.IMAGING_CT,
    "ct": DocumentCategory.IMAGING_CT,
    "imaging_us": DocumentCategory.IMAGING_US,
    "mri": DocumentCategory.IMAGING,
    "pet": DocumentCategory.IMAGING,
    "rtg": DocumentCategory.IMAGING,
    "usg": DocumentCategory.IMAGING_US,
    "sono": DocumentCategory.IMAGING_US,
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
    "discharge": DocumentCategory.DISCHARGE,
    "discharge_summary": DocumentCategory.DISCHARGE_SUMMARY,
    "prepustenie": DocumentCategory.DISCHARGE,
    "epikryza": DocumentCategory.DISCHARGE,
    "chemo_sheet": DocumentCategory.CHEMO_SHEET,
    "reference": DocumentCategory.REFERENCE,
    "referencne": DocumentCategory.REFERENCE,
    "advocate": DocumentCategory.ADVOCATE,
    "advokat": DocumentCategory.ADVOCATE,
}

_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")
_DATE_XX_RE = re.compile(r"^(\d{4})(\d{2})(xx)", re.IGNORECASE)
_PATIENT_PREFIX_RE = re.compile(r"^ErikaFusekova[-]?", re.IGNORECASE)

# Bilingual format: EN category prefix already present
_BILINGUAL_PREFIX_RE = re.compile(
    r"^(labs|report|pathology|imaging_ct|imaging_us|imaging|genetics|surgery|"
    r"surgical_report|prescription|referral|discharge_summary|discharge|"
    r"chemo_sheet|reference|advocate|other)-",
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
        return DocumentCategory.IMAGING_CT
    if lower.startswith("mri") or lower.startswith("pet"):
        return DocumentCategory.IMAGING
    if lower.startswith("rtg"):
        return DocumentCategory.IMAGING

    # Check keyword groups in priority order
    for keywords, category in _CATEGORY_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return category

    return DocumentCategory.OTHER


def _parse_new_format(stem: str, ext: str) -> ParsedFilename | None:
    """Try to parse as: YYYYMMDD ErikaFusekova-Institution-Description."""
    result = ParsedFilename(extension=ext)

    # Extract date
    date_match = _DATE_RE.match(stem)
    xx_match = _DATE_XX_RE.match(stem) if not date_match else None

    if date_match:
        with contextlib.suppress(ValueError):
            result.document_date = date(
                int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
            )
        remaining = stem[8:].lstrip(" ")
    elif xx_match:
        # Handle dates like 202602xx — use first of month
        with contextlib.suppress(ValueError):
            result.document_date = date(int(xx_match.group(1)), int(xx_match.group(2)), 1)
        remaining = stem[8:].lstrip(" ")  # YYYYMMXX = 8 chars
    else:
        return None  # No date prefix → not new format

    # Strip patient prefix
    remaining = _PATIENT_PREFIX_RE.sub("", remaining).lstrip("-")

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


def parse_filename(filename: str) -> ParsedFilename:
    """Parse a filename into structured metadata.

    Handles both the real naming convention (space-separated, ErikaFusekova prefix)
    and the legacy underscore-separated format.
    """
    stem = PurePosixPath(filename).stem
    ext = PurePosixPath(filename).suffix.lstrip(".")

    # Try new format first: YYYYMMDD ErikaFusekova-Institution-Description
    if " " in stem and "ErikaFusekova" in stem:
        result = _parse_new_format(stem, ext)
        if result:
            return result

    # Legacy format: YYYYMMDD_institution_category_description.ext
    result = ParsedFilename(extension=ext)

    # Try to extract leading YYYYMMDD date
    m = _DATE_RE.match(stem)
    xx = _DATE_XX_RE.match(stem) if not m else None
    if m:
        with contextlib.suppress(ValueError):
            result.document_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        stem = stem[8:].lstrip("_- ")
    elif xx:
        with contextlib.suppress(ValueError):
            result.document_date = date(int(xx.group(1)), int(xx.group(2)), 1)
        stem = stem[8:].lstrip("_- ")  # YYYYMMXX = 8 chars

    # Split remaining parts on underscores and hyphens
    parts = [p for p in re.split(r"[_\-]+", stem) if p]

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

    # Try to match category from remaining parts
    for i, part in enumerate(parts):
        cat = CATEGORY_ALIASES.get(part.lower())
        if cat:
            result.category = cat
            parts = parts[:i] + parts[i + 1 :]
            break

    # Remaining parts become the description
    if parts:
        result.description = " ".join(parts)

    return result


def rename_to_bilingual(filename: str, category: DocumentCategory | str | None = None) -> str:
    """Add EN category prefix to a filename for bilingual display.

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
    cat_prefix = cat.value.capitalize()
    if cat.value == "imaging_ct":
        cat_prefix = "CT"
    elif cat.value == "imaging_us":
        cat_prefix = "USG"
    elif cat.value == "surgical_report":
        cat_prefix = "SurgicalReport"
    elif cat.value == "discharge_summary":
        cat_prefix = "DischargeSummary"
    elif cat.value == "chemo_sheet":
        cat_prefix = "ChemoSheet"
    elif cat.value == "advocate":
        cat_prefix = "Advocate"

    if parsed.description.lower().startswith(cat_prefix.lower() + "-"):
        return filename

    # Build new filename
    new_desc = f"{cat_prefix}-{parsed.description}"

    # Reconstruct: YYYYMMDD ErikaFusekova-Institution-Category-Description.ext
    parts = []
    if parsed.document_date:
        parts.append(parsed.document_date.strftime("%Y%m%d"))

    name_parts = []
    if parsed.institution:
        name_parts.append(f"ErikaFusekova-{parsed.institution}-{new_desc}")
    else:
        name_parts.append(f"ErikaFusekova-{new_desc}")

    new_stem = f"{parts[0]} {name_parts[0]}" if parts else name_parts[0]
    return f"{new_stem}{ext}"
