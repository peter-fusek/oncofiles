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


# Categories where a doc without a provider letterhead (e.g., internal chemo sheet,
# pre-printed prescription) can safely be attributed to the patient's primary treating
# oncology clinic from patient_context. Labs / imaging / pathology are excluded because
# those are routinely outsourced to external facilities.
_PATIENT_CONTEXT_INSTITUTION_FALLBACK_CATEGORIES = {
    "chemo_sheet",
    "prescription",
    "discharge",
    "discharge_summary",
}


async def backfill_institution_from_patient_context(
    db_conn, *, patient_id: str, dry_run: bool = False
) -> dict:
    """Fallback institution inference using the patient's primary treating clinic.

    When the normal backfill_missing_institutions cannot infer an institution from
    structured_metadata.providers (common for chemo sheets with no letterhead), this
    function falls back to the institution recorded in patient_context.treatment.institution.

    Scoped to safe categories only (chemo_sheet, prescription, discharge) — categories
    that are virtually always issued by the treating oncology clinic. Labs / imaging /
    pathology are excluded because those are routinely outsourced.

    Args:
        db_conn: The raw aiosqlite/libsql connection (Database.db), not the Database wrapper.
        patient_id: The patient to process.
        dry_run: If True, report what would change without making updates.

    Returns stats dict with counts.
    """
    from oncofiles import patient_context as _pctx

    stats: dict = {
        "checked": 0,
        "updated": 0,
        "skipped_unsafe_category": 0,
        "skipped_no_context_institution": 0,
        "dry_run": dry_run,
    }

    # Resolve the patient's primary treating institution.
    ctx = _pctx.get_context(patient_id)
    if not ctx or not ctx.get("name"):
        try:
            ctx = await _pctx.load_from_db(db_conn, patient_id=patient_id)
        except Exception:
            ctx = None
    inst = None
    if ctx:
        tx = ctx.get("treatment") or {}
        inst = (tx.get("institution") or "").strip() or None
    if not inst:
        stats["skipped_no_context_institution"] = 1
        logger.info(
            "backfill_institution_from_patient_context [%s]: no institution in patient_context",
            patient_id,
        )
        return stats

    async with db_conn.execute(
        "SELECT id, category, filename FROM documents "
        "WHERE deleted_at IS NULL AND (institution IS NULL OR institution = '') "
        "AND patient_id = ?",
        (patient_id,),
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        stats["checked"] += 1
        cat = (row["category"] or "").lower()
        if cat not in _PATIENT_CONTEXT_INSTITUTION_FALLBACK_CATEGORIES:
            stats["skipped_unsafe_category"] += 1
            continue
        if not dry_run:
            await db_conn.execute(
                "UPDATE documents SET institution = ? WHERE id = ?",
                (inst, row["id"]),
            )
        stats["updated"] += 1
        logger.info(
            "backfill_institution_from_patient_context [%s]: doc %d (%s) → %s (dry_run=%s)",
            patient_id,
            row["id"],
            cat,
            inst,
            dry_run,
        )

    if stats["updated"] and not dry_run:
        await db_conn.commit()
    return stats


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


_shared_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """Return a shared Anthropic client. Reuses the same httpx connection pool
    to avoid leaking HTTP clients on every AI call."""
    global _shared_client
    if _shared_client is None:
        _shared_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _shared_client


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
    '- "institution_code": Identify the medical institution from letterhead, stamp, '
    "address, provider names, or any context. Known codes: "
    "NOU, BoryNemocnica, OUSA, UNB, Medirex, Alpha, Synlab, Cytopathos, BIOPTIKA, Agel, "
    "ProCare, Medante, ISCare, SvMichal, Kramarska, Medifera, Unilabs, "
    "VeselyKlinika, Urosanus, Sportmed, ProSanus, UNZBratislava, Aseseta, Mediros, "
    "Europacolon, PacientAdvokat, VitalSource, NCCN. "
    "If identifiable but not in this list, create a new CamelCase code. "
    "Return null for insurance/pharma/unidentifiable.\n"
    '- "category": One of: labs, report, imaging, pathology, genetics, surgery, '
    "consultation, prescription, referral, discharge, chemo_sheet, reference, "
    "advocate, other, vaccination, dental, preventive.\n"
    '- "document_date": The clinical encounter/exam date (YYYY-MM-DD). '
    "NOT DOB, NOT future appointments, NOT report-generation date, "
    "NOT next-visit date. null if unknown.\n\n"
    "Worked examples for document_date (#459). The right date is the one\n"
    "a caregiver would call 'when this medical thing happened':\n\n"
    "EXAMPLE 1 — Lab report with multiple dates\n"
    "  DOB: 1972-04-14, Date of sample: 2026-02-13, Date of report: 2026-02-15\n"
    "  → document_date = 2026-02-13  (the sample date, not DOB, not report gen)\n\n"
    "EXAMPLE 2 — Discharge summary\n"
    "  Admission: 2026-01-20, Discharge: 2026-01-27\n"
    "  → document_date = 2026-01-27  (discharge > admission; the file "
    "records the completed stay)\n\n"
    "EXAMPLE 3 — Imaging report\n"
    "  Exam performed: 2026-03-05, Report dictated: 2026-03-06\n"
    "  → document_date = 2026-03-05  (the scan itself is the clinical event)\n\n"
    "EXAMPLE 4 — Consultation with next-visit note\n"
    "  Visit: 2026-02-20, Next check-up scheduled: 2026-03-10\n"
    "  → document_date = 2026-02-20  (the visit just happened; next is FUTURE)\n\n"
    "EXAMPLE 5 — Chemo sheet for cycle 3 of 8\n"
    "  Cycle 1: 2026-01-05, Cycle 2: 2026-01-19, Cycle 3 (today): 2026-02-02, "
    "Cycle 4 scheduled: 2026-02-16\n"
    "  → document_date = 2026-02-02  (the cycle this specific sheet "
    "documents, not the protocol overall)\n\n"
    "If the document itself is a historical log covering multiple events "
    "with no single 'today', use the latest past date that appears.\n\n"
    '- "plain_summary": 3-sentence patient-friendly summary in English\n'
    '- "plain_summary_sk": The same summary in Slovak (slovenčina)\n\n'
    "Respond ONLY with the JSON object, no markdown fencing or extra text."
)


def _check_filename_date_agreement(
    filename: str | None, ai_document_date: str | None, document_id: int | None
) -> None:
    """#459 cross-check: warn when the AI-extracted document_date diverges
    from the date encoded in the filename by more than 30 days.

    Caregivers sometimes hand-name files with the correct clinical date in
    YYYYMMDD prefix; a >30d divergence is almost always an AI pick of the
    wrong candidate (DOB, report-gen, next-visit). We don't auto-override —
    the AI date may be right and the filename wrong — but we surface the
    mismatch so Peter can triage with search_prompt_log(text="date disagree").
    """
    if not filename or not ai_document_date:
        return
    try:
        from datetime import date

        from oncofiles.filename_parser import _match_any_date

        stem_date = _match_any_date(filename)
        if not stem_date:
            return
        filename_date, _ = stem_date
        ai_date = date.fromisoformat(ai_document_date)
        diff = abs((ai_date - filename_date).days)
        if diff > 30:
            logger.warning(
                "date disagree doc=%s filename=%s AI=%s filename_date=%s diff=%dd",
                document_id,
                filename,
                ai_document_date,
                filename_date.isoformat(),
                diff,
            )
    except Exception:
        logger.debug("date-disagree check failed", exc_info=True)


def extract_structured_metadata(
    text: str,
    *,
    db=None,
    document_id: int | None = None,
    filename: str | None = None,
) -> dict:
    """Extract structured medical metadata from document text.

    Args:
        text: Extracted text from the document.
        db: Database instance for prompt logging (optional).
        document_id: Document ID for prompt logging (optional).
        filename: Original filename — when set, enables the #459 date-agreement
            cross-check that warns if AI-extracted ``document_date`` differs
            from the filename's encoded date by more than 30 days.

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
        _check_filename_date_agreement(filename, parsed.get("document_date"), document_id)
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


CLASSIFY_SYSTEM_PROMPT = (
    "You are a medical document classifier. Given document text, return a JSON object "
    "with exactly 3 keys:\n"
    '- "institution_code": The medical institution. Known codes: '
    "NOU, BoryNemocnica, OUSA, UNB, Medirex, Alpha, Synlab, Cytopathos, BIOPTIKA, "
    "Agel, ProCare, Medante, ISCare, SvMichal, Kramarska, Medifera, Unilabs, "
    "VeselyKlinika, Urosanus, Sportmed, ProSanus, UNZBratislava, Aseseta, Mediros, "
    "Europacolon, PacientAdvokat, VitalSource, NCCN. "
    "Look at letterhead, stamps, addresses, provider names — the name often "
    "appears in a header, footer, signature block, or stamp even when the body "
    "is about labs or an exam. Apply diacritic-insensitive matching for Slovak "
    "names.\n"
    "  Examples of mapping to known codes:\n"
    "    'Národný onkologický ústav' / 'NÁRODNÝ ONKOLOGICKÝ ÚSTAV' / "
    "'Klinika klinickej onkológie, Klenová 1' → NOU\n"
    "    'Nemocnica Bory' / 'BoryNemocnica' / 'Nemocnica Rychlý' → BoryNemocnica\n"
    "    'SYNLAB Slovakia s.r.o.' → Synlab\n"
    "    'MEDIREX' / 'Medirex servis' → Medirex\n"
    "    'Poliklinika Kramáre' → Kramarska\n"
    "    'Mediros' / 'MEDIROS s.r.o.' → Mediros\n"
    "    'Alpha medical' / 'ALPHA MEDICAL' → Alpha\n"
    "    'Agel Skalica' / 'AGEL' → Agel\n"
    "  Prefer a known code whenever the letterhead matches one of the keyword "
    "groups above; only invent a new CamelCase code if no known one fits. "
    "Use null ONLY if there is truly no identifiable institution in the text.\n"
    '- "category": One of: labs, report, imaging, pathology, genetics, '
    "hereditary_genetics, surgery, "
    "consultation, prescription, referral, discharge, chemo_sheet, reference, "
    "advocate, other, vaccination, dental, preventive.\n"
    "  • Use `labs` ONLY when the document is primarily a lab-results "
    "printout: a tabular panel of measured values with reference ranges, "
    "typically issued by a lab (Medirex, Synlab, Alpha, Unilabs) or a "
    "hospital lab department. The dominant content is parameter/value/unit/"
    "range rows (CBC, biochemistry, tumor markers). No treatment plan, no "
    "regimen, no nurse notes.\n"
    "    Examples: 'Vyšetrenie krvi — WBC 6.8 × 10^9/L (4.0–10.0); NEUT% "
    "62.5; ...' on Medirex letterhead → labs. 'Laboratórne vyšetrenie' "
    "table from NOU with ABS_NEUT, HGB, PLT, CEA, CA19-9 columns → labs.\n"
    "  • Use `chemo_sheet` for a chemotherapy administration form / "
    "protocol — a structured treatment record listing regimen name "
    "(mFOLFOX6, FOLFIRI, FOLFOXIRI, bevacizumab…), cycle number, planned "
    "doses, infusion schedule, premedication, and/or nurse-administered "
    "notes. A chemo sheet MAY quote a few pre-chemo lab values inline — "
    "that does not make it a lab report. If the page centers on the "
    "regimen, the cycle, or the trial protocol, it is `chemo_sheet`.\n"
    "    Examples: 'Cyklus 4 / mFOLFOX6 / Oxaliplatina 85 mg/m² + "
    "5-FU bolus + infúzia 46h, Levofolic...' → chemo_sheet. 'INCA033890 "
    "trial eligibility assessment, Phase III FOLFOX + bevacizumab ± "
    "INCA33890 bispecific…' → chemo_sheet (trial protocol).\n"
    "  • Use `consultation` for a clinical encounter note / follow-up — a "
    "narrative describing what happened at a visit: symptoms, findings, "
    "plan, next steps. The doctor's name + Klinika/Oddelenie + date is "
    "the signature. Inline labs are commonly quoted ('WBC 12.11, neutr. "
    "77.9%') — those do not make the doc a lab report either. Febrile "
    "neutropenia workup, post-cycle check-in, DVT review — all "
    "`consultation`.\n"
    "    Example: 'Pacient na kontrole po 3. cykle mFOLFOX6, febrilná "
    "neutropénia…, plán: ATB Ciprinol, G-CSF Zarzio, CT 4.5.' on NOU / "
    "Klenová 1 letterhead, signed MUDr. Mináriková → consultation.\n"
    "  • Use `hereditary_genetics` when the document describes INHERITED / "
    "GERMLINE / FAMILIAL cancer risk testing — keywords: hereditary, germline, "
    "zárodočný, dedičný, BRCA1, BRCA2, Lynch syndrome, Li-Fraumeni, ACMG class, "
    "cascade testing, familial cancer, pathogenic variant in a patient's DNA "
    "(not tumor tissue).\n"
    "  • Use `genetics` for SOMATIC / TUMOR molecular profiling — keywords: "
    "MSI, MSS, TMB, tumor DNA, ctDNA, KRAS / NRAS / BRAF mutation status from "
    "tumor tissue, HER2 amplification, PD-L1 TPS, somatic panel.\n"
    '- "document_date": Clinical encounter date (YYYY-MM-DD). '
    "NOT DOB, NOT appointments. null if unknown.\n\n"
    "Respond ONLY with the JSON object."
)


def classify_document(
    text: str,
    *,
    db=None,
    document_id: int | None = None,
) -> dict:
    """Classify a document: institution, category, and date using AI.

    Lightweight dedicated call — more reliable than embedding these fields
    in the larger metadata extraction prompt.

    Returns dict with institution_code, category, document_date (all nullable).
    """
    if not text.strip():
        return {"institution_code": None, "category": "other", "document_date": None}

    client = _get_client()
    truncated = text[:6000]
    user_prompt = f"Document text:\n\n{truncated}"

    start = time.perf_counter()
    response = client.messages.create(
        model=ENHANCE_MODEL,
        max_tokens=256,
        system=CLASSIFY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    duration_ms = int((time.perf_counter() - start) * 1000)

    raw = response.content[0].text if response.content else "{}"

    log_ai_call(
        db,
        call_type="doc_classification",
        document_id=document_id,
        model=ENHANCE_MODEL,
        system_prompt=CLASSIFY_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        raw_response=raw,
        input_tokens=getattr(response.usage, "input_tokens", None),
        output_tokens=getattr(response.usage, "output_tokens", None),
        duration_ms=duration_ms,
    )

    try:
        parsed = json.loads(_strip_markdown_fencing(raw))
        return {
            "institution_code": parsed.get("institution_code"),
            "category": parsed.get("category", "other"),
            "document_date": parsed.get("document_date"),
        }
    except json.JSONDecodeError:
        logger.warning("Failed to parse classification response: %s", raw[:200])
        return {"institution_code": None, "category": "other", "document_date": None}
