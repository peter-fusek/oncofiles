"""The user-facing lifecycle of a document, in one place.

    uploaded → extracted (text read) → named → filed → ready to use

The dashboard (``GET /api/documents``), ``/status`` and the MCP status matrix
all derive what a person sees from here, so "named" and "ready" mean the same
thing everywhere.

Two kinds of rows share the ``documents`` table:

* **curated** rows mirror a file filed in a curated Drive folder (#562/#603):
  ``file_id`` starts with ``gdrive:`` and ``structured_metadata`` carries the
  ``curated`` object the indexer writes. Their filenames follow the README
  contract (``curated.naming``, hyphen-separated).
* **legacy** rows come from the old sync / upload path and follow the
  underscore standard checked by ``filename_parser.is_standard_format``.

Before this module every status surface used the legacy check for every row,
so a curated patient showed "named 0" for hundreds of correctly named files.

``ready`` is deliberately smaller than the legacy ``fully_complete`` of the
status matrix: an AI summary (written only by the cloud path, which is off by
default) and a known institution (``Unknown`` is a legitimate outcome since
#854) are NOT required.

Pure functions only: no DB, no I/O, no model call. Output is flags and codes,
never a filename or a document value.
"""

from __future__ import annotations

import json
import re

# Steps a person can follow, in the order they happen.
STEPS = ("extracted", "named", "filed")

# ``stage`` values; a waiting stage names the first step still missing.
STAGE_READY = "ready"
STAGE_ATTENTION = "attention"
WAITING_STAGE = {
    "extracted": "waiting_text",
    "named": "waiting_name",
    "filed": "waiting_filing",
}
# A file still waiting in a curated inbox: no ``documents`` row yet, shown from
# the probe's in-memory snapshot (``dashboard_inbox``, #921 follow-up).
STAGE_IN_INBOX = "in_inbox"
STAGES = (STAGE_READY, STAGE_ATTENTION, STAGE_IN_INBOX, *WAITING_STAGE.values())

# ``attention_reason`` codes, in the order they are checked (first match wins).
REASON_REVIEW_NEEDED = "review_needed"
REASON_MISSING_IN_DRIVE = "missing_in_drive"
REASON_OUTSIDE_FOLDER = "outside_folder"
REASON_MISSING_DATE = "missing_date"
ATTENTION_REASONS = (
    REASON_REVIEW_NEEDED,
    REASON_MISSING_IN_DRIVE,
    REASON_OUTSIDE_FOLDER,
    REASON_MISSING_DATE,
)


def _metadata(structured_metadata) -> dict:
    """``documents.structured_metadata`` (JSON text, a dict or None) as a dict."""
    if isinstance(structured_metadata, dict):
        return structured_metadata
    if not structured_metadata or not isinstance(structured_metadata, str | bytes):
        return {}
    try:
        value = json.loads(structured_metadata)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def is_curated_row(file_id: str | None, structured_metadata=None) -> bool:
    """True for a row the curated indexer mirrors from a curated Drive folder.

    The ``gdrive:`` prefix alone is not enough: a Drive-only row of the legacy
    sync / upload path shares it (#777). Same rule as
    ``Database.curated_index_state``'s ``foreign`` flag: such a row has
    non-empty metadata without the ``curated`` object (and without
    ``source: curated``). A ``gdrive:`` row with no metadata yet is the
    indexer's own, caught between its insert and its commit marker.
    """
    from oncofiles.curated.indexer import is_curated_file_id

    if not is_curated_file_id(file_id):
        return False
    meta = _metadata(structured_metadata)
    if not meta:
        return True
    return "curated" in meta or meta.get("source") == "curated"


def is_named(
    filename: str | None,
    *,
    file_id: str | None,
    structured_metadata=None,
    patient_id: str,
    mime_type: str | None = None,
) -> bool:
    """Does the filename follow the standard that applies to this row?

    Curated row: the README contract (``curated.naming.is_standard``); the
    row's ``mime_type`` lets a Google-native file (a Google Doc has no
    extension through the Drive API) count as named (#919).
    Any other row: the legacy underscore standard for this patient.
    """
    if not filename:
        return False
    if is_curated_row(file_id, structured_metadata):
        from oncofiles.curated.naming import is_standard

        return is_standard(filename, mime=mime_type)
    from oncofiles.filename_parser import is_standard_format

    return is_standard_format(filename, patient_id=patient_id)


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def document_month(file_id: str | None, structured_metadata=None) -> str | None:
    """``YYYY-MM`` of a curated row dated by month only (#919), else None.

    The curated indexer stores such a document with ``document_date`` NULL
    (never a day printed nowhere) and ``doc_date_precision: "month"`` plus
    ``doc_month`` in its metadata. A row that has this month HAS a date: it
    is never ``missing_date``, and the dashboard shows ``03/2027``.
    """
    if not is_curated_row(file_id, structured_metadata):
        return None
    meta = _metadata(structured_metadata)
    month = meta.get("doc_month")
    if meta.get("doc_date_precision") == "month" and isinstance(month, str):
        return month if _MONTH_RE.match(month) else None
    return None


def is_extracted(*, has_text: bool, extractable: bool) -> bool:
    """The "text read" step: stored page text, or a file type that has no such step.

    ``extractable`` is the caller's existing notion (``tools.hygiene``:
    ``sync.EXTRACTABLE_MIME_TYPES``); a spreadsheet or a text note is never
    OCR'd, so it must not wait for text forever.
    """
    return bool(has_text) or not extractable


def _curated(meta: dict) -> dict:
    curated = meta.get("curated")
    return curated if isinstance(curated, dict) else {}


def _review_needed(meta: dict) -> bool:
    """The sidecar front-matter key ``review_needed`` as the indexer stores it.

    ``tools.curated.index_patient`` spreads the kept front-matter keys into
    the top level of ``structured_metadata``; the ``curated`` object is read
    too, in case a later index version moves them there.

    An active #900 maintainer override (``curated.review_override`` is true:
    a non-empty ``review_override`` without ``review_override_status:
    hard_checks_failed``, decided by ``curated.runner.review_override``) means
    no review (#919): the runner files such a document only after every hard
    check passed, and it leaves ``review_needed: true`` in the sidecar on
    purpose (the sidecar moves byte for byte). Only indexed (= filed) rows
    reach this function; the inbox is never indexed.
    """
    curated = _curated(meta)
    if curated.get("review_override") is True:
        return False
    for holder in (meta, curated):
        value = holder.get("review_needed")
        if value is True or (isinstance(value, str) and value.strip().lower() == "true"):
            return True
    return False


def review_reason_codes(structured_metadata) -> list[str]:
    """The review reason codes the curated indexer kept (#919); [] when none.

    Codes only (``curated.review_codes``), re-validated on read, so a stored
    value can never carry text to the dashboard.
    """
    from oncofiles.curated.review_codes import safe_codes

    return safe_codes(_curated(_metadata(structured_metadata)).get("review_reasons"))


def stale_review(file_id: str | None, structured_metadata) -> bool:
    """A curated row held in review with no recognised reason (#919 item 4).

    True when the row would show ``review_needed`` (the same check as
    ``attention_reason``, so an active #900 override is never stale) and its
    kept codes are empty or only ``other``: the dashboard can name no action.
    The curated runner re-checks such documents (``runner._recheck_stale_reviews``).
    """
    if not is_curated_row(file_id, structured_metadata):
        return False
    meta = _metadata(structured_metadata)
    if not _review_needed(meta):
        return False
    from oncofiles.curated.review_codes import OTHER

    return all(code == OTHER for code in review_reason_codes(meta))


def attention_reason(
    *,
    file_id: str | None = None,
    structured_metadata=None,
    sync_state: str | None = None,
    outside_folder: bool = False,
    has_text: bool = False,
    has_date: bool = True,
) -> str | None:
    """Why a person should look at this document, as a code, or None.

    First match in this order:

    * ``review_needed``: a curated row whose sidecar says ``review_needed: true``
    * ``missing_in_drive``: the Drive file is gone (``sync_state = deleted_remote``)
    * ``outside_folder``: the Drive file lives outside the patient's folder
    * ``missing_date``: the text was read, but no document date came out of it
    """
    if is_curated_row(file_id, structured_metadata) and _review_needed(
        _metadata(structured_metadata)
    ):
        return REASON_REVIEW_NEEDED
    if sync_state == "deleted_remote":
        return REASON_MISSING_IN_DRIVE
    if outside_folder:
        return REASON_OUTSIDE_FOLDER
    if has_text and not has_date:
        return REASON_MISSING_DATE
    return None


def stage_of(*, extracted: bool, named: bool, filed: bool, attention) -> dict:
    """``{"steps": {extracted, named, filed}, "ready": bool, "stage": str}``.

    ``attention`` (a reason code, or any truthy value) wins over everything;
    otherwise ``stage`` names the first missing step in the order extracted,
    named, filed. ``ready`` = all three steps done and no attention.
    """
    steps = {"extracted": bool(extracted), "named": bool(named), "filed": bool(filed)}
    if attention:
        stage = STAGE_ATTENTION
    else:
        stage = next((WAITING_STAGE[s] for s in STEPS if not steps[s]), STAGE_READY)
    return {"steps": steps, "ready": stage == STAGE_READY, "stage": stage}


def describe(
    *,
    filename: str | None,
    file_id: str | None,
    structured_metadata=None,
    patient_id: str,
    has_text: bool,
    extractable: bool,
    gdrive_id: str | None,
    sync_state: str | None = None,
    outside_folder: bool = False,
    has_date: bool = True,
    mime_type: str | None = None,
) -> dict:
    """Everything a status row needs for one document, from its raw columns.

    ``{"curated", "steps", "ready", "stage", "attention_reason", "review_reasons"}``;
    ``review_reasons`` (codes) is filled only when the reason is ``review_needed``.
    """
    from oncofiles.curated.indexer import is_curated_file_id

    # Parse the metadata once, and only for a row that can be curated at all
    # (a legacy row's AI metadata is large and never read here).
    structured_metadata = _metadata(structured_metadata) if is_curated_file_id(file_id) else None
    # #919: a month-precision curated document is dated (document_date NULL by design)
    has_date = has_date or document_month(file_id, structured_metadata) is not None
    reason = attention_reason(
        file_id=file_id,
        structured_metadata=structured_metadata,
        sync_state=sync_state,
        outside_folder=outside_folder,
        has_text=has_text,
        has_date=has_date,
    )
    stage = stage_of(
        extracted=is_extracted(has_text=has_text, extractable=extractable),
        named=is_named(
            filename,
            file_id=file_id,
            structured_metadata=structured_metadata,
            patient_id=patient_id,
            mime_type=mime_type,
        ),
        filed=gdrive_id is not None,
        attention=reason,
    )
    return {
        "curated": is_curated_row(file_id, structured_metadata),
        **stage,
        "attention_reason": reason,
        "review_reasons": (
            review_reason_codes(structured_metadata) if reason == REASON_REVIEW_NEEDED else []
        ),
    }
