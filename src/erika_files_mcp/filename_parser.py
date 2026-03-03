"""Parse medical document filenames following the YYYYMMDD convention.

Expected format: YYYYMMDD_institution_category_description.ext
Examples:
    20240115_NOUonko_labs_krvnyObraz.pdf
    20240220_OUSA_report_kontrola.pdf
    20231005_UNB_imaging_CT-abdomen.pdf
    nenazvany_dokument.pdf  (unparseable → best-effort)
"""

from __future__ import annotations

import contextlib
import re
from datetime import date
from pathlib import PurePosixPath

from erika_files_mcp.models import DocumentCategory, ParsedFilename

# Known institution codes (expandable)
KNOWN_INSTITUTIONS = {
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

# Map filename tokens to categories (case-insensitive matching)
CATEGORY_ALIASES: dict[str, DocumentCategory] = {
    "labs": DocumentCategory.LABS,
    "lab": DocumentCategory.LABS,
    "blood": DocumentCategory.LABS,
    "krv": DocumentCategory.LABS,
    "report": DocumentCategory.REPORT,
    "sprava": DocumentCategory.REPORT,
    "kontrola": DocumentCategory.REPORT,
    "imaging": DocumentCategory.IMAGING,
    "ct": DocumentCategory.IMAGING,
    "mri": DocumentCategory.IMAGING,
    "pet": DocumentCategory.IMAGING,
    "rtg": DocumentCategory.IMAGING,
    "usg": DocumentCategory.IMAGING,
    "sono": DocumentCategory.IMAGING,
    "pathology": DocumentCategory.PATHOLOGY,
    "histo": DocumentCategory.PATHOLOGY,
    "biopsia": DocumentCategory.PATHOLOGY,
    "surgery": DocumentCategory.SURGERY,
    "operacia": DocumentCategory.SURGERY,
    "prescription": DocumentCategory.PRESCRIPTION,
    "recept": DocumentCategory.PRESCRIPTION,
    "referral": DocumentCategory.REFERRAL,
    "odporucanie": DocumentCategory.REFERRAL,
    "ziadanka": DocumentCategory.REFERRAL,
    "discharge": DocumentCategory.DISCHARGE,
    "prepustenie": DocumentCategory.DISCHARGE,
    "epikryza": DocumentCategory.DISCHARGE,
}

_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")


def parse_filename(filename: str) -> ParsedFilename:
    """Parse a filename into structured metadata.

    Handles both convention-compliant filenames and best-effort parsing of
    non-standard names.
    """
    stem = PurePosixPath(filename).stem
    ext = PurePosixPath(filename).suffix.lstrip(".")

    result = ParsedFilename(extension=ext)

    # Try to extract leading YYYYMMDD date
    m = _DATE_RE.match(stem)
    if m:
        with contextlib.suppress(ValueError):
            result.document_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        stem = stem[8:].lstrip("_- ")

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
