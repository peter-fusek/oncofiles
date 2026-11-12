"""Human review of facts (#636): verify, unverify, correct.

Design: ``docs/architecture/hard_data_model.md`` §3.1 (provenance, versions,
verification vocabulary) and §4.3 (audited write tools, human as ``verifier``).

Rules enforced here:
* every helper takes a keyword-only ``patient_id`` and filters on it in SQL, so
  a foreign fact id is simply not found (no existence oracle);
* only active (not soft-deleted) facts can be reviewed;
* ``verify`` / ``unverify`` change only ``verification`` (a QA state, not the
  clinical value) and write a ``clinical_record_audit`` row; ``verify`` also
  writes a ``provenance`` row (activity ``verify``, agent role ``verifier``);
* ``correct`` never edits a value in place: the old row is soft-deleted and a
  new row is inserted with ``verification = 'corrected'``, ``version_id + 1``,
  the same ``fhir_id`` and the old row's source document / page / evidence,
  plus ``metadata.corrected_from``. Both rows get an audit row and the new row
  a provenance row (activity ``correct``; the old version is the ``revision``
  entity, recorded in ``reason`` until provenance has an entity column). A row
  stored before stable ids (``fhir_id`` NULL) gets, on both versions, the id
  readers already saw, so a corrected date never changes the fact id (#741);
* each action is written in one transaction (one commit at the end).

Re-extraction (``replace_document_facts``) keeps ``verified`` and ``corrected``
rows and never re-adds the extractor's version of such a fact, also when its
date was corrected (#741, matched by fact id or the original date); that
contract is locked in ``tests/test_fact_review.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

from ._hard_facts import PROVENANCE_ACTIVITIES, PROVENANCE_AGENT_ROLES

_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
REVIEW_SOURCE = "fact-review"
REVIEWABLE = ("machine", "unverified")

# Copied from the old row to its correction unchanged.
_CARRY_COLUMNS = (
    "patient_id", "record_type", "source_document_id", "param", "status", "ref_range_low",
    "ref_range_high", "ref_text", "code", "page_number", "evidence", "encounter_id",
    "practitioner_id", "organization_id", "department_id", "extracted_by", "session_id",
    "result_status", "value_comparator", "unit_printed", "unit_ucum", "interpretation_code",
    "effective_start", "effective_end", "effective_precision", "issued_at", "specimen_id",
    "report_id", "fhir_id",
)  # fmt: skip
# Range-dating keys (#726) that no longer describe a day a person set.
_RANGE_KEYS = ("date_precision", "date_range_start", "date_range_end", "date_range_open")


def _unit_key(u: str | None) -> str:
    return (u or "").lower().replace("µ", "u").replace("μ", "u").replace(" ", "")


def _flag(value: float | None, lo: float | None, hi: float | None) -> str | None:
    if value is None:
        return None
    if lo is not None and value < lo:
        return "L"
    if hi is not None and value > hi:
        return "H"
    return None


def _check_occurred_at(value: str) -> str:
    value = value.strip()
    try:
        date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ValueError("occurred_at must be an ISO date (YYYY-MM-DD) or datetime") from exc
    return value


def _meta(raw: str | None) -> dict:
    try:
        out = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


class FactVerificationMixin:
    """verify / unverify / correct one ``clinical_records`` fact."""

    # ── internals ─────────────────────────────────────────────────────

    async def _vr_one(self, sql: str, params: tuple) -> dict | None:
        async with self.db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def find_reviewable_fact(self, fact_ref: int | str, *, patient_id: str) -> dict | None:
        """The active fact ``fact_ref`` of ``patient_id``, or None.

        ``fact_ref`` is the integer row id or the fact's ``fhir_id``.
        TODO(#728): once stable fact ids land, rows whose ``fhir_id`` is still
        NULL get their id computed at read time (``fact_identity.ids_for_rows``);
        resolve those here too, not only ids stored in ``fhir_id``.
        """
        if not patient_id:
            raise ValueError("patient_id is required")
        if isinstance(fact_ref, bool):
            return None
        if isinstance(fact_ref, str):
            ref = fact_ref.strip()
            if ref.isdigit():
                fact_ref = int(ref)
            else:
                return await self._vr_one(
                    "SELECT * FROM clinical_records WHERE fhir_id = ? AND patient_id = ? "
                    "AND deleted_at IS NULL ORDER BY version_id DESC, id DESC LIMIT 1",
                    (ref, patient_id),
                )
        return await self._vr_one(
            "SELECT * FROM clinical_records WHERE id = ? AND patient_id = ? AND deleted_at IS NULL",
            (fact_ref, patient_id),
        )

    async def _vr_audit(
        self,
        record_id: int,
        action: str,
        *,
        before: dict | None,
        after: dict | None,
        changed_fields: str | None,
        reason: str | None,
        agent_user: str,
    ) -> None:
        await self.db.execute(
            "INSERT INTO clinical_record_audit (record_id, action, before_json, after_json, "
            "changed_fields, reason, source, caller_identity, changed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, action,
                json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                changed_fields, reason, REVIEW_SOURCE, agent_user, agent_user,
            ),
        )  # fmt: skip

    async def _vr_provenance(
        self,
        fact: dict,
        *,
        activity: str,
        agent_role: str,
        agent_user: str,
        reason: str | None,
    ) -> int:
        """Append one provenance row for ``fact`` (no commit: part of the caller's write)."""
        if activity not in PROVENANCE_ACTIVITIES or agent_role not in PROVENANCE_AGENT_ROLES:
            raise ValueError("provenance activity/agent_role not allowed")
        cur = await self.db.execute(
            "INSERT INTO provenance (fhir_id, patient_id, target_type, target_id, target_version, "
            "activity, agent_role, agent_user, source_document_id, page_number, quote, reason) "
            "VALUES (?, ?, 'fact', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), fact["patient_id"], fact["id"], fact.get("version_id"),
                activity, agent_role, agent_user, fact.get("source_document_id"),
                fact.get("page_number"), fact.get("evidence"), reason,
            ),
        )  # fmt: skip
        return cur.lastrowid

    # ── verify / unverify ─────────────────────────────────────────────

    async def verify_fact(
        self, fact_ref: int | str, *, patient_id: str, agent_user: str, note: str | None = None
    ) -> dict:
        """Mark a machine/unverified fact as verified by a person.

        Returns ``{"status": "not_found" | "already_verified" | "corrected" |
        "verified", "fact": row?, "provenance_id": int?}``.
        """
        if not agent_user:
            raise ValueError("agent_user is required")
        fact = await self.find_reviewable_fact(fact_ref, patient_id=patient_id)
        if fact is None:
            return {"status": "not_found"}
        if fact["verification"] == "verified":
            return {"status": "already_verified", "fact": fact}
        if fact["verification"] not in REVIEWABLE:
            # A corrected value was entered by a person; correct it again instead.
            return {"status": fact["verification"], "fact": fact}
        await self.db.execute(
            f"UPDATE clinical_records SET verification = 'verified', updated_at = {_NOW}, "
            f"updated_by = ?, last_updated = {_NOW} "
            "WHERE id = ? AND patient_id = ? AND deleted_at IS NULL",
            (agent_user, fact["id"], patient_id),
        )
        await self._vr_audit(
            fact["id"], "update",
            before={"verification": fact["verification"]}, after={"verification": "verified"},
            changed_fields="verification", reason=f"verify: {note}" if note else "verify",
            agent_user=agent_user,
        )  # fmt: skip
        prov_id = await self._vr_provenance(
            fact, activity="verify", agent_role="verifier", agent_user=agent_user, reason=note
        )
        await self.db.commit()
        fact = await self.find_reviewable_fact(fact["id"], patient_id=patient_id)
        return {"status": "verified", "fact": fact, "provenance_id": prov_id}

    async def unverify_fact(
        self, fact_ref: int | str, *, patient_id: str, agent_user: str, note: str | None = None
    ) -> dict:
        """Undo a verification: back to the state before the last ``verify``.

        The previous state comes from the newest verify audit row of the fact
        (``machine`` when there is none). Corrected facts are not unverified:
        a correction is a new version, correct it again instead.
        """
        if not agent_user:
            raise ValueError("agent_user is required")
        fact = await self.find_reviewable_fact(fact_ref, patient_id=patient_id)
        if fact is None:
            return {"status": "not_found"}
        if fact["verification"] != "verified":
            return {"status": "not_verified", "fact": fact}
        last = await self._vr_one(
            "SELECT before_json FROM clinical_record_audit WHERE record_id = ? "
            "AND changed_fields = 'verification' AND reason LIKE 'verify%' "
            "ORDER BY id DESC LIMIT 1",
            (fact["id"],),
        )
        previous = _meta(last["before_json"] if last else None).get("verification")
        if previous not in REVIEWABLE:
            previous = "machine"
        await self.db.execute(
            f"UPDATE clinical_records SET verification = ?, updated_at = {_NOW}, "
            f"updated_by = ?, last_updated = {_NOW} "
            "WHERE id = ? AND patient_id = ? AND deleted_at IS NULL",
            (previous, agent_user, fact["id"], patient_id),
        )
        await self._vr_audit(
            fact["id"], "update",
            before={"verification": "verified"}, after={"verification": previous},
            changed_fields="verification", reason=f"unverify: {note}" if note else "unverify",
            agent_user=agent_user,
        )  # fmt: skip
        await self.db.commit()
        fact = await self.find_reviewable_fact(fact["id"], patient_id=patient_id)
        return {"status": "unverified", "fact": fact}

    # ── correct ───────────────────────────────────────────────────────

    async def _vr_canonical(
        self, code: str | None, value: float | None, unit: str | None
    ) -> tuple[float | None, str | None]:
        """Canonical value/unit for a corrected numeric value (fact_codes conversions)."""
        if code is None or value is None:
            return None, None
        row = await self._vr_one(
            "SELECT unit_canonical, unit_conversions FROM fact_codes WHERE code = ?", (code,)
        )
        if row is None:
            return None, None
        canonical = row["unit_canonical"]
        uk, ck = _unit_key(unit), _unit_key(canonical)
        conv = {_unit_key(k): f for k, f in _meta(row["unit_conversions"]).items()}
        if not uk or uk == ck:
            return value, canonical
        if uk in conv:
            return round(value * float(conv[uk]), 6), canonical
        return None, None  # unknown unit: the value is kept, never under the canonical unit

    async def correct_fact(
        self,
        fact_ref: int | str,
        *,
        patient_id: str,
        agent_user: str,
        reason: str,
        value_num: float | None = None,
        value_text: str | None = None,
        unit: str | None = None,
        occurred_at: str | None = None,
    ) -> dict:
        """Replace a fact with a corrected version (never an in-place edit).

        Returns ``{"status": "not_found" | "corrected", "old": row?, "fact":
        new row?, "provenance_id": int?}``. ``ValueError`` for bad input.
        """
        if not agent_user:
            raise ValueError("agent_user is required")
        if not (reason or "").strip():
            raise ValueError("reason is required")
        if value_num is not None and value_text is not None:
            raise ValueError("give value_num or value_text, not both")
        if value_num is None and value_text is None and unit is None and occurred_at is None:
            raise ValueError("nothing to correct: give value_num, value_text, unit or occurred_at")
        if value_text is not None and not value_text.strip():
            raise ValueError("value_text must not be empty")
        if occurred_at is not None:
            occurred_at = _check_occurred_at(occurred_at)
        old = await self.find_reviewable_fact(fact_ref, patient_id=patient_id)
        if old is None:
            return {"status": "not_found"}
        all_rows_fhir_index = await self._vr_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'uq_cr_fhir_id'", ()
        )
        if old.get("fhir_id") and all_rows_fhir_index:
            # Migration 080's index is unique over ALL rows; the new version must
            # share the fhir_id, which needs #728's active-only index. Refuse
            # before writing anything rather than half-apply the correction.
            raise ValueError("this fact has a stable id; correcting it needs migration 082")

        new: dict[str, Any] = {c: old.get(c) for c in _CARRY_COLUMNS}
        if not new["fhir_id"] and not all_rows_fhir_index:
            # A row stored before stable ids has none: both versions get the id
            # readers already saw (computed as the insert path would), never one
            # computed later from the corrected date (#741).
            computed = await self.effective_fact_ids(
                patient_id=patient_id, document_ids=[old.get("source_document_id")]
            )
            new["fhir_id"] = computed.get(old["id"])
        changed: list[str] = []
        if value_num is not None:
            new["value_num"], new["value_text"] = float(value_num), None
            changed.append("value_num")
        elif value_text is not None:
            new["value_num"], new["value_text"] = None, value_text.strip()
            changed.append("value_text")
        else:
            new["value_num"], new["value_text"] = old.get("value_num"), old.get("value_text")
        new["unit"] = unit.strip() if unit is not None else old.get("unit")
        if unit is not None:
            changed.append("unit")
        new["occurred_at"] = occurred_at if occurred_at is not None else old.get("occurred_at")
        if occurred_at is not None:
            changed.append("occurred_at")
        if {"value_num", "value_text", "unit"} & set(changed):
            new["value_canonical"], new["unit_canonical"] = await self._vr_canonical(
                new["code"], new["value_num"], new["unit"]
            )
            new["flag"] = _flag(
                new["value_canonical"] if new["value_canonical"] is not None else new["value_num"],
                old.get("ref_range_low"),
                old.get("ref_range_high"),
            )
        else:
            new["value_canonical"] = old.get("value_canonical")
            new["unit_canonical"] = old.get("unit_canonical")
            new["flag"] = old.get("flag")

        meta = _meta(old.get("metadata_json"))
        meta.pop("unverified_reasons", None)
        if occurred_at is not None:
            for key in _RANGE_KEYS:
                meta.pop(key, None)
            meta["date_source"] = "corrected"
        meta["corrected_from"] = {
            "fact_id": old["id"],
            "version_id": old.get("version_id"),
            "verification": old["verification"],
            "fields": changed,
        }
        new.update(
            metadata_json=json.dumps(meta, ensure_ascii=False),
            verification="corrected",
            version_id=(old.get("version_id") or 1) + 1,
            confidence=None,
            source=REVIEW_SOURCE,
            caller_identity=agent_user,
            created_by=agent_user,
            updated_by=agent_user,
        )

        # Soft-delete first: fhir_id is unique among active rows (#728), and the
        # correction carries the same logical id as the version it replaces.
        await self.db.execute(
            f"UPDATE clinical_records SET deleted_at = {_NOW}, deleted_by = ?, "
            f"updated_at = {_NOW}, updated_by = ?, last_updated = {_NOW}, "
            "fhir_id = COALESCE(fhir_id, ?) "
            "WHERE id = ? AND patient_id = ? AND deleted_at IS NULL",
            (agent_user, agent_user, new["fhir_id"], old["id"], patient_id),
        )
        cols = list(new)
        cur = await self.db.execute(
            f"INSERT INTO clinical_records ({', '.join(cols)}, last_updated) "
            f"VALUES ({', '.join('?' for _ in cols)}, {_NOW})",
            tuple(new[c] for c in cols),
        )
        new_id = cur.lastrowid
        audit_reason = f"correct: {reason.strip()}"
        await self._vr_audit(
            old["id"], "delete",
            before={"verification": old["verification"], "version_id": old.get("version_id")},
            after={"superseded_by": new_id}, changed_fields="deleted_at,deleted_by",
            reason=audit_reason, agent_user=agent_user,
        )  # fmt: skip
        await self._vr_audit(
            new_id, "create",
            before=None, after={**new, "id": new_id}, changed_fields=",".join(changed),
            reason=audit_reason, agent_user=agent_user,
        )  # fmt: skip
        stored = await self._vr_one(
            "SELECT * FROM clinical_records WHERE id = ? AND patient_id = ?", (new_id, patient_id)
        )
        if stored is None:
            raise RuntimeError(f"correct_fact: row {new_id} vanished after INSERT")
        prov_id = await self._vr_provenance(
            stored,
            activity="correct",
            agent_role="verifier",
            agent_user=agent_user,
            reason=(
                f"entity=revision fact/{old['id']}/_history/{old.get('version_id') or 1}; "
                f"{reason.strip()}"
            ),
        )
        await self.db.commit()
        return {"status": "corrected", "old": old, "fact": stored, "provenance_id": prov_id}
