"""How a fact was obtained, as shown to readers (#726 infra review of #729).

Rows written by the LLM extractor of the non-lab kinds (``extracted_by``
starting with ``llm-clinical``) keep ``verification = machine`` in the DB,
but every read output that shows them says so: ``method = "llm-clinical"``
and a ``verification_label`` that differs from rule-parsed / re-read machine
facts. The verifier only checks that the text is printed on the page; the
interpretation (which line is the diagnosis, a current vs a historical
regimen) is the model's and is not verified.

Until cross-document dedupe exists (#730), identical LLM rows from several
documents are grouped at read time into one item with ``sources``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable

from oncofiles.database._facts import LLM_CLINICAL_FAMILY

LLM_METHOD = LLM_CLINICAL_FAMILY
DEDUPE_ISSUE = "#730"
LLM_NOTE = (
    "Rows with method 'llm-clinical' were read by the local model: the verifier only "
    "checked that the text is printed on the page. The interpretation (which line is the "
    "diagnosis, a current vs a historical regimen) is the model's and is not verified. "
    "Identical rows from several documents are one item with 'sources'."
)

_LABELS = {
    "llm": {"en": "text verified on page, read by the model",
            "sk": "prečítané modelom, text overený"},
    "machine": {"en": "machine-verified", "sk": "overené strojom"},
    "unverified": {"en": "unverified", "sk": "neoverené"},
    "llm_unverified": {"en": "read by the model, text not verified",
                       "sk": "prečítané modelom, text neoverený"},
    "verified": {"en": "verified by a person", "sk": "overené človekom"},
    "corrected": {"en": "corrected by a person", "sk": "opravené človekom"},
}  # fmt: skip
_RANK = {"verified": 0, "corrected": 0, "machine": 1, "unverified": 2}


def is_llm(extracted_by: str | None) -> bool:
    return str(extracted_by or "").startswith(LLM_CLINICAL_FAMILY)


def method_of(extracted_by: str | None) -> str | None:
    """``llm-clinical`` | ``rule`` (rule parsers, re-reads) | None (manual / legacy)."""
    if is_llm(extracted_by):
        return LLM_METHOD
    return "rule" if str(extracted_by or "").startswith("rule") else None


def verification_label(verification: str | None, extracted_by: str | None) -> dict | None:
    """``{"en": …, "sk": …}`` for a verification state; LLM rows get their own."""
    if not verification:
        return None
    llm = is_llm(extracted_by)
    if verification == "machine":
        return dict(_LABELS["llm" if llm else "machine"])
    if verification == "unverified":
        return dict(_LABELS["llm_unverified" if llm else "unverified"])
    return dict(_LABELS[verification]) if verification in _LABELS else None


def label_row(row: dict, *, verification_key: str = "verification") -> dict:
    """Add ``method`` and ``verification_label`` (from ``extracted_by``)."""
    eb = row.get("extracted_by")
    row["method"] = method_of(eb)
    row["verification_label"] = verification_label(row.get(verification_key), eb)
    return row


def norm(value) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip().lower()


def group_llm(
    rows: list[dict],
    key: Callable[[dict], Hashable],
    *,
    document_key: str = "source_document_id",
    page_key: str = "page_number",
    id_key: str = "id",
) -> list[dict]:
    """Group identical LLM rows across documents (same ``key``) into one item
    with ``sources: [{document_id, page, id, verification}]``; the item keeps
    the strongest verification of its sources. Other rows are not grouped.
    Every row gets ``method`` and ``verification_label``. Order: the first
    occurrence of each group."""
    out: list[dict] = []
    groups: dict[Hashable, dict] = {}
    for row in rows:
        if not is_llm(row.get("extracted_by")):
            out.append(label_row(row))
            continue
        source = {
            "document_id": row.get(document_key), "page": row.get(page_key),
            "id": row.get(id_key), "verification": row.get("verification"),
        }  # fmt: skip
        k = key(row)
        item = groups.get(k)
        if item is None:
            item = groups[k] = {**row, "sources": [source]}
            out.append(item)
            continue
        item["sources"].append(source)
        if _RANK.get(row.get("verification"), 3) < _RANK.get(item.get("verification"), 3):
            item["verification"] = row.get("verification")
    for item in groups.values():
        item["per_document"] = False
        label_row(item)
    return out
