"""AI-powered document composition analysis — splitting, consolidation, relationships."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from oncofiles.enhance import _get_client, _strip_markdown_fencing
from oncofiles.models import Document
from oncofiles.prompt_logger import log_ai_call

logger = logging.getLogger(__name__)

ANALYSIS_MODEL = "claude-haiku-4-5-20251001"


# ── Multi-document detection ────────────────────────────────────────────────

COMPOSITION_SYSTEM_PROMPT = """\
You are a medical document analyst. Given the extracted text of a PDF document, \
determine whether this PDF contains ONE logical document or MULTIPLE distinct \
logical documents combined into a single file.

Analyze the content semantically — look for:
- Changes in letterhead, institution header, or logo references
- Different dates on different sections (not just dates mentioned in text, \
but dates that indicate separate document creation)
- Different signing physicians or departments
- Transitions between completely different document types (e.g., lab results \
followed by a discharge summary)
- Separate page numbering restarts
- Different patient visit contexts within the same file

Do NOT split a document just because it mentions multiple dates in the text body. \
Only split when there are genuinely distinct, independent documents combined into \
one PDF (e.g., a hospital printed multiple unrelated reports into one file).

Respond with a JSON object:
{
  "document_count": <int>,
  "documents": [
    {
      "page_range": [<start>, <end>],
      "document_date": "<YYYY-MM-DD or null>",
      "institution": "<institution name or null>",
      "category": "<one of: labs, report, imaging, pathology, genetics, surgery, \
consultation, prescription, referral, discharge, chemo_sheet, vaccination, \
dental, preventive, other>",
      "description": "<short CamelCase English description, max 60 chars>",
      "confidence": <0.0-1.0>,
      "reasoning": "<brief explanation why this is a distinct document>"
    }
  ]
}

If the PDF contains only one document, return document_count: 1 with a single \
element in the documents array.

Respond ONLY with the JSON object, no markdown fencing or extra text."""


def analyze_document_composition(
    text: str,
    *,
    db: Any = None,
    document_id: int | None = None,
) -> list[dict]:
    """Analyze a document's OCR text to detect multiple logical documents.

    Returns a list of dicts, each describing a sub-document. A single-element
    list means no split is needed.
    """
    if not text.strip():
        return []

    client = _get_client()
    truncated = text[:12000]  # more context for composition analysis
    user_prompt = f"Document text:\n\n{truncated}"

    start = time.perf_counter()
    response = client.messages.create(
        model=ANALYSIS_MODEL,
        max_tokens=1024,
        system=COMPOSITION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    duration_ms = int((time.perf_counter() - start) * 1000)

    raw = response.content[0].text if response.content else "{}"

    log_ai_call(
        db,
        call_type="doc_composition",
        document_id=document_id,
        model=ANALYSIS_MODEL,
        system_prompt=COMPOSITION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        raw_response=raw,
        input_tokens=getattr(response.usage, "input_tokens", None),
        output_tokens=getattr(response.usage, "output_tokens", None),
        duration_ms=duration_ms,
    )

    try:
        parsed = json.loads(_strip_markdown_fencing(raw))
        documents = parsed.get("documents", [])
        if not isinstance(documents, list) or len(documents) == 0:
            return []
        return documents
    except json.JSONDecodeError:
        logger.warning("Failed to parse composition response: %s", raw[:200])
        return []


# ── Consolidation detection ─────────────────────────────────────────────────

CONSOLIDATION_SYSTEM_PROMPT = """\
You are a medical document analyst. Given a list of documents (each with its \
metadata and extracted text excerpt), determine which documents are PARTS of \
the same logical document that was split across multiple files.

Documents that belong together typically show:
- Continuation of text mid-sentence or mid-section from one file to the next
- Sequential page numbers across files
- Same letterhead/header and same visit/encounter
- Same diagnostic procedure with results spanning files (e.g., multi-page \
pathology report scanned as separate PDFs)
- Same date, institution, and category with complementary content

Do NOT group documents just because they are from the same date or institution. \
Only group them if their content clearly forms one continuous logical document \
that was artificially split into separate files.

Respond with a JSON object:
{
  "groups": [
    {
      "document_ids": [<int>, <int>, ...],
      "reasoning": "<brief explanation why these belong together>",
      "confidence": <0.0-1.0>
    }
  ]
}

If no documents should be consolidated, return {"groups": []}.
Order document_ids within each group by the logical reading sequence.

Respond ONLY with the JSON object, no markdown fencing or extra text."""


def analyze_consolidation(
    doc_texts: list[tuple[Document, str]],
    *,
    db: Any = None,
) -> list[dict]:
    """Analyze multiple documents to find ones that should be consolidated.

    Args:
        doc_texts: List of (Document, extracted_text) pairs.
        db: Database instance for prompt logging.

    Returns:
        List of consolidation groups, each with document_ids, reasoning, confidence.
    """
    if len(doc_texts) < 2:
        return []

    client = _get_client()

    # Build document summaries for the AI
    doc_descriptions = []
    for doc, text in doc_texts:
        excerpt = text[:2000] if text else "(no text)"
        doc_descriptions.append(
            f"--- Document ID: {doc.id} ---\n"
            f"Filename: {doc.filename}\n"
            f"Date: {doc.document_date}\n"
            f"Institution: {doc.institution}\n"
            f"Category: {doc.category.value}\n"
            f"Text excerpt:\n{excerpt}\n"
        )

    user_prompt = f"Documents to analyze ({len(doc_texts)} files):\n\n" + "\n".join(
        doc_descriptions
    )

    # Truncate if too long
    if len(user_prompt) > 30000:
        user_prompt = user_prompt[:30000] + "\n\n[truncated]"

    start = time.perf_counter()
    response = client.messages.create(
        model=ANALYSIS_MODEL,
        max_tokens=1024,
        system=CONSOLIDATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    duration_ms = int((time.perf_counter() - start) * 1000)

    raw = response.content[0].text if response.content else "{}"

    log_ai_call(
        db,
        call_type="doc_consolidation",
        document_id=None,
        model=ANALYSIS_MODEL,
        system_prompt=CONSOLIDATION_SYSTEM_PROMPT,
        user_prompt=f"[{len(doc_texts)} documents, prompt truncated for logging]",
        raw_response=raw,
        input_tokens=getattr(response.usage, "input_tokens", None),
        output_tokens=getattr(response.usage, "output_tokens", None),
        duration_ms=duration_ms,
    )

    try:
        parsed = json.loads(_strip_markdown_fencing(raw))
        groups = parsed.get("groups", [])
        if not isinstance(groups, list):
            return []
        return groups
    except json.JSONDecodeError:
        logger.warning("Failed to parse consolidation response: %s", raw[:200])
        return []


# ── AI-powered cross-references ─────────────────────────────────────────────

RELATIONSHIPS_SYSTEM_PROMPT = """\
You are a medical document analyst. Given a target document and a list of \
candidate documents (with metadata and text summaries), determine the \
relationships between the target and each candidate.

Relationship types:
- "same_visit": documents from the exact same medical visit/encounter \
(same appointment, same day, same procedure)
- "follow_up": the target is a follow-up to a previous document \
(e.g., post-treatment labs after a chemo session documented earlier)
- "supersedes": the target replaces or updates information from a previous document \
(e.g., corrected lab report, updated pathology after re-analysis)
- "related": documents that share clinical context but are from different visits \
(e.g., pre-treatment imaging and surgery report weeks later)
- "contradicts": findings in the target conflict with findings in a candidate \
(rare but important — e.g., different biomarker results)

Only report relationships you are confident about. Do not force relationships \
where none exist.

Respond with a JSON object:
{
  "relationships": [
    {
      "target_id": <int>,
      "relationship": "<same_visit|follow_up|supersedes|related|contradicts>",
      "confidence": <0.0-1.0>,
      "reasoning": "<brief explanation>"
    }
  ]
}

Respond ONLY with the JSON object, no markdown fencing or extra text."""


def analyze_document_relationships(
    doc_text: str,
    doc_id: int,
    candidates: list[dict],
    *,
    db: Any = None,
) -> list[dict]:
    """Analyze relationships between a document and candidates using AI.

    Args:
        doc_text: OCR text of the target document.
        doc_id: ID of the target document.
        candidates: List of dicts with id, filename, document_date, institution,
                    category, ai_summary for candidate documents.
        db: Database instance for prompt logging.

    Returns:
        List of relationship dicts with target_id, relationship, confidence, reasoning.
    """
    if not candidates:
        return []

    client = _get_client()

    # Build candidate descriptions
    candidate_descriptions = []
    for c in candidates:
        candidate_descriptions.append(
            f"- ID {c['id']}: {c['filename']} | "
            f"{c.get('document_date', '?')} | {c.get('institution', '?')} | "
            f"{c.get('category', '?')} | "
            f"Summary: {c.get('ai_summary', 'N/A')}"
        )

    doc_excerpt = doc_text[:4000] if doc_text else "(no text)"
    user_prompt = (
        f"Target document (ID {doc_id}):\n{doc_excerpt}\n\n"
        f"Candidate documents:\n" + "\n".join(candidate_descriptions)
    )

    if len(user_prompt) > 20000:
        user_prompt = user_prompt[:20000] + "\n\n[truncated]"

    start = time.perf_counter()
    response = client.messages.create(
        model=ANALYSIS_MODEL,
        max_tokens=1024,
        system=RELATIONSHIPS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    duration_ms = int((time.perf_counter() - start) * 1000)

    raw = response.content[0].text if response.content else "{}"

    log_ai_call(
        db,
        call_type="doc_relationships",
        document_id=doc_id,
        model=ANALYSIS_MODEL,
        system_prompt=RELATIONSHIPS_SYSTEM_PROMPT,
        user_prompt=f"[target doc {doc_id} + {len(candidates)} candidates, truncated]",
        raw_response=raw,
        input_tokens=getattr(response.usage, "input_tokens", None),
        output_tokens=getattr(response.usage, "output_tokens", None),
        duration_ms=duration_ms,
    )

    try:
        parsed = json.loads(_strip_markdown_fencing(raw))
        relationships = parsed.get("relationships", [])
        if not isinstance(relationships, list):
            return []
        return relationships
    except json.JSONDecodeError:
        logger.warning("Failed to parse relationships response: %s", raw[:200])
        return []
