"""A caregiver confirms a filed curated document "as is" (#919, dashboard).

A filed document whose sidecar says ``review_needed: true`` with soft reasons
only (no printed date found, low model confidence, a vision note) shows
"Needs your review" on the dashboard. The caregiver compares it with the
original and confirms it: this module adds ONE front-matter line to the
document's sidecar, the #900 override

    review_override: "confirmed in dashboard (YYYY-MM-DD)"

which ``runner.review_override`` (and so the indexer's
``curated.review_override`` and ``document_stage``) recognises as active.
Every other byte of the sidecar stays as it is (front matter and body); the
override value never carries an e-mail or a name. Only the sidecar (a
``.md`` text state file) is ever written, through ``drive_ops.update_text``
(read back, never retried); an original is never touched, and a text-source
note has no sidecar, so it cannot be confirmed here.

The caller (``tools.curated.confirm_review_for``) holds ``patient_lock`` and
``contract_writes()``; this module does the checks on the sidecar's CURRENT
text and the single write. Pure apart from the two ``ops`` calls.
"""

from __future__ import annotations

from datetime import date

from oncofiles.curated.indexer import parse_sidecar
from oncofiles.curated.review_codes import OTHER, codes_of
from oncofiles.curated.runner import (
    OVERRIDE_KEY,
    OVERRIDE_STATUS_KEY,
    _front_value,
    _set_front,
    _split_sidecar,
    review_override,
)

# The review reason codes a caregiver may set aside with "Confirm as is".
# Anything else (another person, a birth date, a split, an unknown type, a
# missing institution or vocabulary entry, a technical failure, …) must be
# fixed in Drive: the route answers 409 ``hard_check``. May only shrink
# without a maintainer decision.
CONFIRMABLE_CODES = frozenset(
    {"no_date", "low_confidence", "rotated_region", "orientation_unsure", "chart_page", OTHER}
)


class ConfirmRefusedError(Exception):
    """The sidecar cannot be confirmed; ``code`` says why. Nothing was written."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AlreadyResolvedError(Exception):
    """The sidecar is no longer in review (or already overridden). Nothing was written."""


def override_value(today: date) -> str:
    """The override reason written by the dashboard: a fixed text and the date only."""
    return f"confirmed in dashboard ({today.isoformat()})"


def hard_codes(codes) -> list[str]:
    """The codes of ``codes`` that a caregiver may NOT set aside, in order."""
    return [c for c in codes or [] if c not in CONFIRMABLE_CODES]


def plan(sidecar_text: str, today: date) -> tuple[str, list[str]]:
    """``(new sidecar text, codes set aside)`` for a sidecar that may be confirmed.

    Raises ``AlreadyResolvedError`` when the sidecar is no longer in review or
    carries an active override, and ``ConfirmRefusedError`` (``no_front_matter``,
    ``override_blocked``, ``override_present``, ``hard_check``) otherwise.
    The new text is the old one with exactly one front-matter line appended.
    """
    parts = _split_sidecar(sidecar_text)
    if parts is None:
        raise ConfirmRefusedError("no_front_matter")
    front_raw, rest = parts
    if review_override(sidecar_text) is not None:
        raise AlreadyResolvedError()
    if _front_value(front_raw, OVERRIDE_STATUS_KEY):
        # an earlier override the runner found blocked by a hard check
        raise ConfirmRefusedError("override_blocked")
    if _front_value(front_raw, OVERRIDE_KEY) is not None:
        # an inactive value ("false", "") would be REPLACED, not added: a person decides
        raise ConfirmRefusedError("override_present")
    front, _pages = parse_sidecar(sidecar_text)
    needed = front.get("review_needed")
    if not (needed is True or (isinstance(needed, str) and needed.strip().lower() == "true")):
        raise AlreadyResolvedError()
    codes = codes_of(front.get("review_reasons"))
    if hard_codes(codes):
        raise ConfirmRefusedError("hard_check")
    new_front = _set_front(front_raw, OVERRIDE_KEY, override_value(today))
    new_text = f"---\n{new_front}\n---{rest}"
    # the value written must be the one the indexer and the runner recognise
    if review_override(new_text) != override_value(today):
        raise ConfirmRefusedError("override_not_recognised")
    return new_text, codes


def confirm(ops, sidecar_id: str, today: date) -> dict:
    """Read the sidecar, check it, write the override line once (read back by ``ops``).

    ``ops`` is a ``drive_ops.DriveOps`` inside ``contract_writes()``. Returns
    ``{"result": "confirmed", "codes": [...]}``; raises as ``plan`` does, and
    any Drive error propagates (the caller fails closed).
    """
    try:
        text = ops.download(sidecar_id).decode("utf-8")
    except UnicodeDecodeError:
        # re-encoding a replaced text would change bytes of the sidecar
        raise ConfirmRefusedError("sidecar_unreadable") from None
    new_text, codes = plan(text, today)
    ops.update_text(sidecar_id, new_text)
    return {"result": "confirmed", "codes": codes}
