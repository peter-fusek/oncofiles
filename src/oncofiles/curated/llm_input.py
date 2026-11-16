"""The one door through which a document's text reaches the local model.

Every LLM path that reads a stored document for the model loads it through
``load_for_llm`` (lab shadow / lab write / clinical preview + write / the
extraction scorecard, via ``tools.curated._doc_work``). The re-OCR and the
verify-only re-read select their candidates through ``reocr_exclusion`` (same
rule, same reason), and the inbox classification of a curated run checks
``inbox_exclusion`` before its model call. ``tests/test_llm_input_exclusion.py``
locks this with an AST sweep: a new caller of the local model that reads page
text without going through here fails CI.

The rule (#815 infra review, P0): raw genotype data never reaches the model.
An excluded document gets NO model call, no rows, no Drive write, and shows
up in its job report as ``result: skipped``, ``reason: raw_genotype_excluded``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from oncofiles.curated.ocr_markers import strip_charts_pages

# ── raw genotype data: never sent to the model (#815 infra review, P0) ──
# A raw export (CSV / TSV / plain text / VCF) of genetics data holds thousands
# of genotype calls; it is excluded in code BEFORE any model call, for every
# LLM job (lab and clinical preview / write, scorecard, re-OCR, re-read, inbox
# classification), both profiles. Conservative: when
# unsure, exclude. An explicit opt-out for any document is the flag
# ``exclude_extract: true`` in ``structured_metadata`` or in its curated part
# (``structured_metadata.curated``, mirrored from the sidecar front matter).
EXCLUDED_RAW_GENOTYPE = "raw_genotype_excluded"
RAW_MIME_TYPES = frozenset({
    "text/csv", "text/tab-separated-values", "text/plain", "text/x-vcf", "text/vcf",
    "application/csv",
})  # fmt: skip
RAW_EXTENSIONS = (".csv", ".tsv", ".txt", ".vcf", ".vcf.gz")
_ALWAYS_RAW = (".vcf", ".vcf.gz")  # variant call files: raw genotype data by definition
_GENOTYPE_MARKERS = re.compile(
    r"\brs\d{3,}\b|genotyp|genom|23andme|ancestry|myheritage|\bvcf\b|raw[ _-]?data|\bsnps?\b"
    r"|\bdna\b|chromosome|chromozóm",
    re.I,
)
_RSID = re.compile(r"\brs\d{3,}\b", re.I)
GENETICS_CATEGORIES = frozenset({"genetics"})


def _truthy(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "1"))


def extraction_exclusion(doc: dict, pages=()) -> str | None:
    """``raw_genotype_excluded`` when a document must never reach the model,
    else None. ``doc`` is a candidate row (filename, mime_type, category,
    structured_metadata); ``pages`` optionally its (number, text) pages.

    Excluded when (a) it is a raw data export (mime type or extension of a
    CSV / TSV / plain text / VCF file) that is a VCF, in a genetics category,
    or carries genotype markers in its filename, metadata / front matter or
    page text (rsIDs); or (b) it carries ``exclude_extract: true``. Metadata
    that is not a mapping counts as unsure → excluded for a raw export."""
    meta = doc.get("structured_metadata")
    cur = (meta.get("curated") if isinstance(meta, dict) else None) or {}
    if isinstance(meta, dict) and (
        _truthy(meta.get("exclude_extract")) or (isinstance(cur, dict)
                                                 and _truthy(cur.get("exclude_extract")))
    ):  # fmt: skip
        return EXCLUDED_RAW_GENOTYPE
    name = str(doc.get("filename") or "").lower()
    mime = str(doc.get("mime_type") or "").lower().split(";")[0].strip()
    if not (mime in RAW_MIME_TYPES or name.endswith(RAW_EXTENSIONS)):
        return None
    if name.endswith(_ALWAYS_RAW) or "vcf" in mime:
        return EXCLUDED_RAW_GENOTYPE
    if str(doc.get("category") or "").lower() in GENETICS_CATEGORIES:
        return EXCLUDED_RAW_GENOTYPE
    if meta is not None and not isinstance(meta, dict):
        return EXCLUDED_RAW_GENOTYPE  # unreadable metadata: unsure
    blob = json.dumps(meta or {}, ensure_ascii=False, default=str)
    if _GENOTYPE_MARKERS.search(name) or _GENOTYPE_MARKERS.search(blob):
        return EXCLUDED_RAW_GENOTYPE
    text = "\n".join(t for _, t in pages or () if t)
    if len(_RSID.findall(text)) >= 3:
        return EXCLUDED_RAW_GENOTYPE
    return None


def is_raw_export(doc: dict) -> bool:
    """A raw data export by mime type or extension (CSV / TSV / text / VCF):
    the only kind of document whose page text can still make it excluded."""
    name = str(doc.get("filename") or "").lower()
    mime = str(doc.get("mime_type") or "").lower().split(";")[0].strip()
    return mime in RAW_MIME_TYPES or name.endswith(RAW_EXTENSIONS)


@dataclass(frozen=True)
class Excluded:
    """``load_for_llm``'s answer for a document that must never reach the model."""

    reason: str


def skipped_entry(doc: dict, reason: str) -> dict:
    """The job-report entry of an excluded document (ids and codes only)."""
    return {"document_id": doc.get("id", doc.get("document_id")), "category": doc.get("category"),
            "result": "skipped", "reason": reason, "error": reason}  # fmt: skip


async def _pages(db, pid: str, doc: dict) -> list[tuple[int, str]]:
    return [(int(p["page_number"]), p.get("extracted_text") or "")
            for p in await db.get_ocr_pages(doc["id"], patient_id=pid)]  # fmt: skip


async def load_for_llm(db, pid: str, doc: dict) -> list[tuple[int, str]] | Excluded:
    """A document's stored page text for an LLM call, or ``Excluded``.

    The document row is checked first (flag, raw export by type / name /
    category / metadata), so an excluded row's pages are never even read;
    then the page text (rsIDs in a raw export). The ONLY reader of
    ``document_pages`` for a model call (locked by test).

    ``[chart: …]`` blocks (#609) are replaced by ``[chart]`` here: chart axis
    ticks are never data, so neither the extractors nor the verifiers (which
    check evidence against this same page text) ever see them."""
    if reason := extraction_exclusion(doc):
        return Excluded(reason)
    pages = await _pages(db, pid, doc)
    if reason := extraction_exclusion(doc, pages):
        return Excluded(reason)
    return strip_charts_pages(pages)


async def reocr_exclusion(db, pid: str, doc: dict) -> str | None:
    """The same rule for a re-OCR / verify-only re-read candidate (a
    ``curated_reocr_candidates`` row): the row, and for a raw export also its
    stored page text. Those jobs send the ORIGINAL file to a vision pass, so
    they are filtered here, before any Drive download."""
    if reason := extraction_exclusion(doc):
        return reason
    if not is_raw_export(doc):
        return None  # page text only ever excludes a raw export
    return extraction_exclusion(doc, await _pages(db, pid, doc))


def inbox_exclusion(name: str, mime: str, text: str) -> str | None:
    """The rule for a new inbox file before its classification call (a curated
    run; no document row yet: name, mime type and the extracted text)."""
    return extraction_exclusion({"filename": name, "mime_type": mime}, [(1, text or "")])
