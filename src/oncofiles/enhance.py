"""AI enhancement layer for documents — summaries and tags (#v0.9)."""

from __future__ import annotations

import json
import logging
import re
import time

import anthropic

from oncofiles.config import ANTHROPIC_API_KEY
from oncofiles.prompt_logger import log_ai_call

logger = logging.getLogger(__name__)

ENHANCE_MODEL = "claude-haiku-4-5-20251001"

# Provider name fragments → institution code (case-insensitive matching)
PROVIDER_TO_INSTITUTION: list[tuple[list[str], str]] = [
    (["nou", "narodny onkologicky", "klenova", "porsok", "pazderova"], "NOU"),
    (["bory", "rychly", "rychlý", "stefanik", "štefánik", "nemocnica bory"], "BoryNemocnica"),
    (["ousa", "onkologicky ustav"], "OUSA"),
    (["unb", "univerzitna nemocnica"], "UNB"),
    (["medirex"], "Medirex"),
    (["alpha", "alpha medical"], "Alpha"),
    (["synlab"], "Synlab"),
    (["cytopathos"], "Cytopathos"),
    (["bioptika"], "BIOPTIKA"),
    (["agel"], "Agel"),
    (["minnesota"], "MinnesotaUniversity"),
    (["socialna poistovna", "socialnapoistovna"], "SocialnaPoistovna"),
    # General healthcare providers
    (["procare", "pro care"], "ProCare"),
    (["medante"], "Medante"),
    (["euromedic"], "Euromedic"),
    (["iscare"], "ISCare"),
    (["nemocnica sv. michala", "sv michala", "svateho michala", "sv. michala"], "SvMichal"),
    (["kramarska", "kramárska", "kramare"], "Kramarska"),
    (["medifera"], "Medifera"),
    (["unilabs"], "Unilabs"),
    (["vesely", "vesela"], "VeselyKlinika"),
    (["urosanus"], "Urosanus"),
    (["sportmed"], "Sportmed"),
    (["pro sanus", "prosanus"], "ProSanus"),
    (["unz mesta", "uzemna poliklinika"], "UNZBratislava"),
    (["aseseta"], "Aseseta"),
    # v5.3.2 — backfill gaps from q1b + e5g audit
    (["mediros"], "Mediros"),
    (["europacolon"], "Europacolon"),
]


def _strip_diacritics(text: str) -> str:
    """Strip diacritics from text for fuzzy matching (ščťžýáíé → sctzyaie)."""
    import unicodedata

    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def infer_institution_from_providers(providers: list[str]) -> str | None:
    """Map provider/institution names from structured metadata to a known institution code.

    Returns the first matching institution code, or None if no match.
    Uses diacritic-insensitive matching for Slovak/Czech names.
    """
    if not providers:
        return None
    combined = _strip_diacritics(" ".join(providers).lower())
    for keywords, institution in PROVIDER_TO_INSTITUTION:
        if any(kw in combined for kw in keywords):
            return institution
    return None


async def backfill_missing_institutions(db) -> dict:
    """Re-run institution inference on docs that have structured_metadata but null institution.

    This is a second-pass backfill for documents where:
    - institution is NULL
    - structured_metadata exists with a providers array
    - providers can now be mapped (new keywords added)

    Args:
        db: The raw aiosqlite/libsql connection (Database.db), not the Database wrapper.

    Returns stats dict with counts.
    """
    import json as _json

    stats = {"checked": 0, "updated": 0, "still_missing": 0}
    async with db.execute(
        "SELECT id, structured_metadata FROM documents "
        "WHERE deleted_at IS NULL AND (institution IS NULL OR institution = '') "
        "AND structured_metadata IS NOT NULL"
    ) as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        stats["checked"] += 1
        try:
            meta = _json.loads(row["structured_metadata"])
        except (ValueError, TypeError):
            stats["still_missing"] += 1
            continue
        providers = meta.get("providers", [])
        inst = infer_institution_from_providers(providers)
        if inst:
            await db.execute(
                "UPDATE documents SET institution = ? WHERE id = ?",
                (inst, row["id"]),
            )
            stats["updated"] += 1
            logger.info("backfill_institution: doc %d → %s (from %s)", row["id"], inst, providers)
        else:
            stats["still_missing"] += 1
    if stats["updated"]:
        await db.commit()
    logger.info("backfill_missing_institutions: %s", stats)
    return stats


def _get_client() -> anthropic.Anthropic:
    """Create Anthropic client. Kept as a function for testability."""
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _strip_markdown_fencing(text: str) -> str:
    """Strip markdown code fencing (```json ... ```) from AI responses."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = stripped.index("\n") if "\n" in stripped else len(stripped)
        stripped = stripped[first_newline + 1 :]
        # Remove closing fence
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3].rstrip()
    return stripped


ENHANCE_SYSTEM_PROMPT = (
    "You are a medical document analyst. Given the extracted text of a medical document, "
    "produce a JSON object with exactly these keys:\n"
    '- "summary": A 2-3 sentence summary of the document in English. Include key findings, '
    "diagnoses, or results.\n"
    '- "summary_sk": The same summary translated to Slovak (slovenčina).\n'
    '- "tags": An array of 3-8 lowercase tags describing the document content '
    '(e.g. ["labs", "blood-count", "oncology", "NOU", "chemotherapy"]).\n\n'
    "Respond ONLY with the JSON object, no markdown fencing or extra text."
)


def enhance_document_text(
    text: str,
    *,
    db=None,
    document_id: int | None = None,
) -> tuple[str, str]:
    """Generate AI summary and tags from document text.

    Args:
        text: Extracted text from the document (OCR or native PDF).
        db: Database instance for prompt logging (optional).
        document_id: Document ID for prompt logging (optional).

    Returns:
        Tuple of (summary, tags_json).
    """
    if not text.strip():
        return "", "[]"

    client = _get_client()
    truncated = text[:8000]
    user_prompt = f"Document text:\n\n{truncated}"

    start = time.perf_counter()
    response = client.messages.create(
        model=ENHANCE_MODEL,
        max_tokens=512,
        system=ENHANCE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    duration_ms = int((time.perf_counter() - start) * 1000)

    raw = response.content[0].text if response.content else "{}"

    log_ai_call(
        db,
        call_type="summary_tags",
        document_id=document_id,
        model=ENHANCE_MODEL,
        system_prompt=ENHANCE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        raw_response=raw,
        input_tokens=getattr(response.usage, "input_tokens", None),
        output_tokens=getattr(response.usage, "output_tokens", None),
        duration_ms=duration_ms,
    )

    try:
        parsed = json.loads(_strip_markdown_fencing(raw))
        summary = parsed.get("summary", "")
        tags = parsed.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        return summary, json.dumps(tags)
    except json.JSONDecodeError:
        logger.warning("Failed to parse AI enhancement response: %s", raw[:200])
        return raw[:500], "[]"


METADATA_SYSTEM_PROMPT = (
    "You are a medical document analyst. Given the extracted text of a medical document, "
    "produce a JSON object with exactly these keys:\n"
    '- "document_type": detected type (one of: lab_report, discharge_summary, pathology, '
    "imaging, surgical_report, prescription, referral, "
    "genetics, chemo_sheet, consultation, vaccination, dental, preventive_checkup, other)\n"
    '- "findings": array of key findings as short strings (max 10)\n'
    '- "diagnoses": array of objects with "name" and optional "icd_code" keys\n'
    '- "medications": array of medication names mentioned\n'
    '- "dates_mentioned": array of dates found in document (YYYY-MM-DD format when possible)\n'
    '- "providers": array of healthcare provider/institution names\n'
    '- "doctors": array of doctor/physician names found in the document '
    '(e.g. ["MUDr. Novak", "Dr. Smith"])\n'
    '- "handwritten": boolean — true if any part of the document appears handwritten\n'
    '- "plain_summary": 3-sentence patient-friendly summary in English\n'
    '- "plain_summary_sk": The same patient-friendly summary in Slovak (slovenčina)\n'
    '- "institution_code": Identify the medical institution from letterhead, stamp, address, '
    "provider names, or any context in the document. Known codes: "
    "NOU, BoryNemocnica, OUSA, UNB, Medirex, Alpha, Synlab, Cytopathos, BIOPTIKA, Agel, "
    "ProCare, Medante, Euromedic, ISCare, SvMichal, Kramarska, Medifera, Unilabs, "
    "VeselyKlinika, Urosanus, Sportmed, ProSanus, UNZBratislava, Aseseta, Mediros, "
    "Europacolon, MinnesotaUniversity, SocialnaPoistovna, PacientAdvokat, VitalSource, NCCN. "
    "If the institution is identifiable but not in this list, create a new CamelCase code "
    "(e.g. NemocnicaPetrzalka). Return null for insurance companies, pharmaceutical "
    "companies, or truly unidentifiable sources.\n"
    '- "category": Classify the document into exactly one of: labs, report, imaging, '
    "pathology, genetics, surgery, consultation, prescription, referral, discharge, "
    "chemo_sheet, reference, advocate, other, vaccination, dental, preventive. "
    'Use "genetics" for molecular/DNA testing (KRAS, MSI, HER2, BRAF panels). '
    'Use "pathology" for tissue histology/biopsy morphology reports. '
    'Use "reference" for textbook excerpts, clinical guidelines (DeVita, NCCN, ESMO). '
    'Use "advocate" for patient advocacy documents.\n'
    '- "document_date": The date of the actual clinical encounter, exam, or report creation '
    "in YYYY-MM-DD format. This should be the visit/procedure date, NOT the patient's date "
    "of birth, NOT future appointment dates, NOT printing timestamps. "
    "Return null if the document date cannot be determined.\n\n"
    "Respond ONLY with the JSON object, no markdown fencing or extra text."
)


def extract_structured_metadata(
    text: str,
    *,
    db=None,
    document_id: int | None = None,
) -> dict:
    """Extract structured medical metadata from document text.

    Args:
        text: Extracted text from the document.
        db: Database instance for prompt logging (optional).
        document_id: Document ID for prompt logging (optional).

    Returns:
        Dict with structured metadata fields.
    """
    if not text.strip():
        return {
            "document_type": "other",
            "findings": [],
            "diagnoses": [],
            "medications": [],
            "dates_mentioned": [],
            "providers": [],
            "doctors": [],
            "handwritten": False,
            "plain_summary": "",
            "plain_summary_sk": "",
            "institution_code": None,
            "category": "other",
            "document_date": None,
        }

    client = _get_client()
    truncated = text[:8000]
    user_prompt = f"Document text:\n\n{truncated}"

    start = time.perf_counter()
    response = client.messages.create(
        model=ENHANCE_MODEL,
        max_tokens=2048,
        system=METADATA_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    duration_ms = int((time.perf_counter() - start) * 1000)

    raw = response.content[0].text if response.content else "{}"

    log_ai_call(
        db,
        call_type="structured_metadata",
        document_id=document_id,
        model=ENHANCE_MODEL,
        system_prompt=METADATA_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        raw_response=raw,
        input_tokens=getattr(response.usage, "input_tokens", None),
        output_tokens=getattr(response.usage, "output_tokens", None),
        duration_ms=duration_ms,
    )

    try:
        parsed = json.loads(_strip_markdown_fencing(raw))
        return {
            "document_type": parsed.get("document_type", "other"),
            "findings": parsed.get("findings", []),
            "diagnoses": parsed.get("diagnoses", []),
            "medications": parsed.get("medications", []),
            "dates_mentioned": parsed.get("dates_mentioned", []),
            "providers": parsed.get("providers", []),
            "doctors": parsed.get("doctors", []),
            "handwritten": parsed.get("handwritten", False),
            "plain_summary": parsed.get("plain_summary", ""),
            "plain_summary_sk": parsed.get("plain_summary_sk", ""),
        }
    except json.JSONDecodeError:
        logger.warning("Failed to parse metadata response: %s", raw[:200])
        return {
            "document_type": "other",
            "findings": [],
            "diagnoses": [],
            "medications": [],
            "dates_mentioned": [],
            "providers": [],
            "doctors": [],
            "handwritten": False,
            "plain_summary": "",
            "plain_summary_sk": "",
        }


FILENAME_DESC_SYSTEM_PROMPT = (
    "You are a medical document analyst. Given the extracted text of a medical document, "
    "generate a short CamelCase English description suitable for a filename.\n\n"
    "Rules:\n"
    "- No spaces, no special characters (only letters and digits)\n"
    "- CamelCase format (e.g., BloodResultsBeforeCycle2DrPorsok)\n"
    "- Include key content: document type, procedure/test. "
    "ALWAYS append DrLastName if any doctor/physician is identified\n"
    "- Maximum 60 characters\n"
    "- If the text is in Slovak, translate the key concepts to English\n\n"
    "Respond ONLY with the CamelCase description, no quotes or extra text."
)


def generate_filename_description(
    text: str,
    *,
    db=None,
    document_id: int | None = None,
) -> str:
    """Generate a short CamelCase English description for a medical document filename.

    Args:
        text: Extracted text from the document (OCR or native PDF).
        db: Database instance for prompt logging (optional).
        document_id: Document ID for prompt logging (optional).

    Returns:
        CamelCase English description, max 60 chars.
    """
    if not text.strip():
        return ""

    client = _get_client()
    truncated = text[:4000]
    user_prompt = f"Document text:\n\n{truncated}"

    start = time.perf_counter()
    response = client.messages.create(
        model=ENHANCE_MODEL,
        max_tokens=100,
        system=FILENAME_DESC_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    duration_ms = int((time.perf_counter() - start) * 1000)

    raw = response.content[0].text.strip() if response.content else ""

    log_ai_call(
        db,
        call_type="filename_description",
        document_id=document_id,
        model=ENHANCE_MODEL,
        system_prompt=FILENAME_DESC_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        raw_response=raw,
        input_tokens=getattr(response.usage, "input_tokens", None),
        output_tokens=getattr(response.usage, "output_tokens", None),
        duration_ms=duration_ms,
    )

    cleaned = re.sub(r"[^a-zA-Z0-9]", "", raw)
    return cleaned[:60]
