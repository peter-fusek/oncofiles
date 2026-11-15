"""Store verified LLM lab facts of ONE document (#714, phase 2b for labs).

Labs whose layout has no rule parser get no lab facts from the index: no new
layout parsers are allowed, and the LLM lab extractor (``llm_extract``) ran in
shadow mode only. This module is the write path for its verified output,
**off by default** (``LLM_LAB_WRITE_ENABLED``); the only caller is the MCP tool
``curated_lab_extract`` (lock test ``tests/test_llm_lab_write.py``).

Rules (infra conditions on #714):

* ``extracted_by = llm-lab-v<N>/<model>`` (``N`` follows the extractor's
  ``EXTRACTOR_VERSION``), source ``curated-llm-lab``; family
  ``database._facts.LLM_LAB_FAMILY``. Every row carries page, evidence and
  verification and gets one ``provenance`` row (activity extract, agent role
  assembler, the extractor device).
* Verification is ``machine`` or ``unverified`` only, never ``verified``: a
  fact with any reason is unverified, and a fact read from the reasoning
  channel is unverified with ``llm_reasoning_channel`` whatever else holds.
  Transcription reasons stay as the verifier set them (never removed).
* ``verifier.require_dob_for_write`` before anything is written.
* Dedupe: never stored when the same document already holds a coded fact of
  the same code and day that this family did not write (rule facts first of
  all, but also manual or other extractors' rows) — ``skipped_rule_exists``.
  A rule fact dated under a printed range blocks every day of its window; an
  undated LLM fact is blocked by any fact of its code in the document.
  Uncoded facts (label not in ``fact_codes``) are not stored
  (``skipped_uncoded``): there is no code to dedupe or chart them by; nor are
  codes outside the lab categories (``skipped_not_lab``: biomarker / gene
  codes belong to the clinical extractor).
* Stable ids: ``fact_identity`` with ``family = llm-lab``, so an LLM fact id
  never equals a rule fact id (the active ``fhir_id`` index is unique).
* Re-extraction goes through ``replace_document_facts(family=llm-lab)``: it
  soft-deletes only this family's rows of the document, with an audit row,
  and never verified / corrected ones; the rule re-extraction
  (``family=None``) never sees this family's rows.
"""

from __future__ import annotations

import json
from collections import Counter

from oncofiles.curated import verifier
from oncofiles.curated.facts_rules import range_window
from oncofiles.curated.llm_extract import EXTRACTOR_VERSION
from oncofiles.database._facts import LLM_LAB_FAMILY, PROTECTED_VERIFICATION
from oncofiles.models import Device, ProvenanceRecord

# llm-extract-v10 → llm-lab-v10: a prompt/schema change of the extractor is a new writer version
WRITER_VERSION = f"{LLM_LAB_FAMILY}-v{EXTRACTOR_VERSION.rsplit('-v', 1)[1]}"
WRITE_LABELS = ("machine", "unverified")  # never verified / corrected
# lab kinds only: biomarker / gene codes belong to the clinical extractor (#726)
LAB_CATEGORIES = frozenset({"lab", "tumor_marker", "vital"})


def extracted_by_for(model: str | None) -> str:
    return f"{WRITER_VERSION}/{model}" if model else WRITER_VERSION


async def device_id_for(db, model: str | None) -> int:
    device = await db.upsert_device(
        Device(
            kind="llm-extractor",
            name="oncofiles.curated.llm_extract",
            version=f"{WRITER_VERSION}+{EXTRACTOR_VERSION}+verifier-{verifier.VERIFIER_VERSION}",
            model=model or None,
            note="local model on stored sidecar text; generic verifier decides machine",
        )
    )
    return device.id


def _label(fact: dict) -> dict:
    """A copy of a verified fact with the write rules applied (labels, reasons)."""
    f = dict(fact)
    meta = dict(f.get("metadata") or {})
    reasons = list(meta.get("unverified_reasons") or [])
    if (
        verifier.REASONING_CHANNEL_NEVER_MACHINE
        and verifier.from_reasoning_channel(f)
        and verifier.REASONING_CHANNEL_REASON not in reasons
    ):
        reasons.append(verifier.REASONING_CHANNEL_REASON)
    if reasons:
        meta["unverified_reasons"] = reasons
    meta["writer"] = WRITER_VERSION
    meta["extractor"] = EXTRACTOR_VERSION
    f["metadata"] = meta
    label = f.get("verification")
    f["verification"] = "machine" if label == "machine" and not reasons else "unverified"
    return f


def _blocked(fact: dict, others: list[dict]) -> bool:
    """Another writer already holds this code and day in the document."""
    code = fact.get("code")
    day = (fact.get("occurred_at") or "")[:10]
    for o in others:
        if o.get("code") != code:
            continue
        if not day:
            return True  # undated: any value of this code in the document wins
        if (o.get("occurred_at") or "")[:10] == day:
            return True
        try:
            meta = json.loads(o.get("metadata_json") or "{}")
        except ValueError:
            meta = {}
        window = range_window(meta if isinstance(meta, dict) else {})
        if window and window[0] <= day <= window[1]:
            return True
    return False


async def plan_document(db, *, patient_id: str, document_id: int, facts: list[dict]) -> dict:
    """What a write of ``facts`` would store: the labelled facts and the
    skipped counts. Reads only; shared by the dry run and the write."""
    others = await db.coded_facts_outside_family(
        patient_id=patient_id, document_id=document_id, family=LLM_LAB_FAMILY
    )
    to_store: list[dict] = []
    skipped: Counter = Counter()
    for f in facts:
        if not f.get("code"):
            skipped["skipped_uncoded"] += 1
        elif f.get("category") not in LAB_CATEGORIES:
            skipped["skipped_not_lab"] += 1
        elif _blocked(f, others):
            skipped["skipped_rule_exists"] += 1
        else:
            to_store.append(_label(f))
    return {
        "facts": to_store,
        "skipped": dict(sorted(skipped.items())),
        "would_store": dict(sorted(Counter(f["verification"] for f in to_store).items())),
    }


async def store_document(
    db,
    *,
    patient_id: str,
    document_id: int,
    facts: list[dict],
    birth_dates,
    model: str | None = None,
) -> dict:
    """Replace this family's facts of one document with ``facts`` (verified
    output of ``llm_extract.extract_document``). Counts only, no values.

    ``birth_dates`` are the ones the facts were verified against: without any,
    the birth-date guard was inactive and nothing is written."""
    verifier.require_dob_for_write(birth_dates)
    plan = await plan_document(db, patient_id=patient_id, document_id=document_id, facts=facts)
    extracted_by = extracted_by_for(model)
    device_id = await device_id_for(db, model)
    res = await db.replace_document_facts(
        patient_id=patient_id, document_id=document_id, facts=plan["facts"],
        extracted_by=extracted_by, family=LLM_LAB_FAMILY,
    )  # fmt: skip
    added: Counter = Counter()
    for row_id, f in res.pop("inserted"):
        meta = f.get("metadata") or {}
        await db.add_provenance(
            ProvenanceRecord(
                target_type="fact", target_id=row_id, target_version=1, activity="extract",
                agent_role="assembler", agent_device_id=device_id,
                source_document_id=document_id, page_number=f.get("page_number"),
                quote=f.get("evidence"),
                reason=",".join(meta.get("unverified_reasons") or []) or None,
            ),
            patient_id=patient_id,
        )  # fmt: skip
        added[f["verification"]] += 1
    stored = {
        "added": res["added"], "removed": res["removed"], "kept_verified": res["kept_verified"],
        "skipped_by_identity": res["skipped_by_identity"], **dict(sorted(added.items())),
    }  # fmt: skip
    return {"stored": stored, "skipped": plan["skipped"], "extracted_by": extracted_by}


async def retract_document(
    db, *, patient_id: str, document_id: int, reason: str, dry_run: bool = True
) -> dict:
    """User-invoked rollback (``curated_lab_retract``) of this family's facts
    of ONE document: soft-deletes exactly what a re-extraction would remove
    (every ``llm-lab*`` row that is not verified / corrected), with an audit
    row each, and adds nothing. Rule facts and every other writer's rows stay.
    Counts only. No birth-date guard: a retract writes no fact and no date."""
    rows = await db.extracted_records_of_document(
        patient_id=patient_id, document_id=document_id, family=LLM_LAB_FAMILY
    )
    kept = sum(1 for r in rows if r.get("verification") in PROTECTED_VERIFICATION)
    remove = [r for r in rows if r.get("verification") not in PROTECTED_VERIFICATION]
    if dry_run:
        removed = len(remove)
    else:
        removed = 0
        for r in remove:
            if await db.soft_delete_extracted_record(
                patient_id=patient_id, record_id=r["id"], reason=reason
            ):
                removed += 1
    counts = {k: v for k, v in (("removed", removed), ("kept", kept)) if v}
    return {"stored": {"lab_facts": counts} if counts else {}}
