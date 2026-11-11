"""DB rows → FHIR R4 JSON. Pure functions over one patient's ``PatientData``.

Verification mapping (design §3.1):

* our QA state (``machine | unverified | verified | corrected``) is a
  ``meta.tag`` in ``urn:oncofiles:verification`` — never a clinical status;
* machine- or LLM-derived resources carry ``meta.security = AIAST`` (AI
  Transparency IG, ballot);
* ``Observation.status`` comes from ``result_status`` (what the report printed
  or implied) and otherwise defaults to ``final``. ``unverified`` is NEVER
  mapped to ``preliminary``: a lab result is not preliminary because our reader
  was unsure;
* every fact gets a Provenance: agent ``assembler`` (the extractor Device),
  ``verifier`` when verified, entity ``source`` = the DocumentReference with
  page and quotation extensions.

Codes: WHO ICD-10 (``http://hl7.org/fhir/sid/icd-10``), ATC
(``http://www.whocc.no/atc``), LOINC, UCUM, SNOMED CT. ICD-10-CM and RxNorm are
dropped from every output (US-only).
"""

from __future__ import annotations

import json
from typing import Any

from oncofiles.fhir import terminology as t
from oncofiles.fhir.ids import derived_id, row_id
from oncofiles.fhir.store import PatientData

GDRIVE_FILE_URL = "https://drive.google.com/file/d/{}/view"

# record sources whose rows were asserted by an AI / machine (AIAST)
_MACHINE_SOURCES = frozenset(
    {"ai-extract", "curated-extract", "mcp-claude", "mcp-chatgpt", "oncoteam"}
)
_MACHINE_PROVENANCE = frozenset({"extract", "reread"})

# medication_events.kind → FHIR resource type (dispense is not projected yet)
MEDICATION_KINDS = {
    "request": "MedicationRequest",
    "administration": "MedicationAdministration",
    "statement": "MedicationStatement",
}
_INPATIENT_KINDS = frozenset({"admission", "hospitalization", "hospitalisation", "inpatient"})
_HUMAN = {"display": "Oncofiles user"}  # never an e-mail address in an export


def _clean(obj: Any) -> Any:
    """Drop None, empty strings, empty lists and empty dicts (FHIR forbids empty elements)."""
    if isinstance(obj, dict):
        out = {k: _clean(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(obj, list):
        out = [_clean(v) for v in obj]
        return [v for v in out if v not in (None, "", [], {})]
    return obj


def _meta_json(row: dict) -> dict:
    try:
        meta = json.loads(row.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def coding(system: str | None, code: Any, display: str | None = None, **extra) -> dict | None:
    system = t.canonical_system(system)
    if code in (None, "") or not t.allowed_system(system):
        return None
    return _clean({"system": system, "code": str(code), "display": display, **extra})


def concept(codings: list[dict | None], text: str | None = None) -> dict:
    seen: set[tuple] = set()
    out = []
    for c in codings:
        if not c:
            continue
        key = (c.get("system"), c.get("code"))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return _clean({"coding": out, "text": text})


def _db_codings(rows: list[dict]) -> list[dict | None]:
    return [
        coding(
            r.get("system"),
            r.get("code"),
            r.get("display"),
            version=r.get("system_version"),
            userSelected=True if r.get("is_original") else None,
        )
        for r in rows
    ]


def _quantity(value: Any, unit: str | None, ucum: str | None = None, comparator=None) -> dict:
    code = ucum or t.ucum_code(unit)
    return _clean(
        {
            "value": value,
            "comparator": comparator,
            "unit": unit or code,
            "system": t.UCUM if code else None,
            "code": code,
        }
    )


def _absent(code: str = "unknown") -> dict:
    return {"extension": [{"url": t.DATA_ABSENT_REASON_EXT, "valueCode": code}]}


class Projector:
    """Projects one patient's rows. ``resources()`` returns every resource once."""

    def __init__(self, data: PatientData):
        self.data = data
        self.patient_fhir_id = derived_id("patients", data.patient_id)
        self._row_ref: dict[tuple[str, int], dict] = {}
        self._index_refs()
        self.extractor_devices: dict[str, dict] = {}

    # ── references ────────────────────────────────────────────────────

    def _index_refs(self) -> None:
        d = self.data
        for r in d.observations:
            self._row_ref[("clinical_records", r["id"])] = self._ref(
                "Observation", r, "clinical_records"
            )
        for r in d.documents:
            self._row_ref[("documents", r["id"])] = self._ref("DocumentReference", r, "documents")
        for table, rtype in (
            ("conditions", "Condition"),
            ("treatment_courses", "CarePlan"),
            ("treatment_cycles", "CarePlan"),
            ("procedures", "Procedure"),
        ):
            for r in d.facts.get(table, []):
                self._row_ref[(table, r["id"])] = self._ref(rtype, r, table)
        for r in d.facts.get("medication_events", []):
            rtype = MEDICATION_KINDS.get(r["kind"])
            if rtype:
                self._row_ref[("medication_events", r["id"])] = self._ref(
                    rtype, r, "medication_events"
                )
        for table, rows, rtype in (
            ("encounters", d.encounters, "Encounter"),
            ("practitioners", d.practitioners, "Practitioner"),
            ("organizations", d.organizations, "Organization"),
            ("departments", d.departments, "Organization"),
            ("devices", d.devices, "Device"),
        ):
            for r in rows:
                self._row_ref[(table, r["id"])] = self._ref(rtype, r, table)

    @staticmethod
    def _ref(rtype: str, row: dict, table: str) -> dict:
        return {"reference": f"{rtype}/{row_id(table, row)}"}

    def ref(self, table: str, rid: Any) -> dict | None:
        """Reference to a row of this patient, or None (never a dangling reference)."""
        if rid is None:
            return None
        found = self._row_ref.get((table, int(rid)))
        return dict(found) if found else None

    @property
    def patient_ref(self) -> dict:
        return {"reference": f"Patient/{self.patient_fhir_id}"}

    def _org_ref(self, row: dict) -> dict | None:
        return self.ref("departments", row.get("department_id")) or self.ref(
            "organizations", row.get("organization_id")
        )

    # ── meta ──────────────────────────────────────────────────────────

    def _is_machine(self, table: str, row: dict) -> bool:
        if row.get("extracted_by"):
            return True
        if (row.get("source") or "") in _MACHINE_SOURCES:
            return True
        provs = self.data.provenance.get((table, row["id"]), [])
        return any(p.get("activity") in _MACHINE_PROVENANCE for p in provs)

    def _meta(self, table: str, row: dict, *, fact: bool = True) -> dict:
        meta: dict = {
            "versionId": str(row.get("version_id") or row.get("version") or 1),
            "lastUpdated": t.fhir_instant(
                row.get("last_updated") or row.get("updated_at") or row.get("created_at")
            ),
        }
        if fact:
            verification = row.get("verification") or "unverified"
            meta["tag"] = [{"system": t.VERIFICATION_SYSTEM, "code": verification}]
            if self._is_machine(table, row):
                meta["security"] = [dict(t.AIAST)]
        return meta

    def _codings_of(self, table: str, rid: Any) -> list[dict | None]:
        return _db_codings(self.data.codings.get((table, str(rid)), []))

    # ── Patient ───────────────────────────────────────────────────────

    def patient(self) -> dict:
        """Minimal Patient: id, gender and birthDate from the soft context. No name."""
        ctx = self.data.context
        sex = str(ctx.get("sex") or "").strip().lower()
        birth = t.fhir_date(ctx.get("date_of_birth"))
        return _clean(
            {
                "resourceType": "Patient",
                "id": self.patient_fhir_id,
                "active": True,
                "gender": sex if sex in ("male", "female", "other", "unknown") else None,
                "birthDate": birth if birth and len(birth) == 10 else None,
            }
        )

    # ── Observation ───────────────────────────────────────────────────

    def observation(self, row: dict) -> dict:
        code = row.get("code")
        category = t.OBSERVATION_CATEGORY_OVERRIDE.get(
            code or "", t.OBSERVATION_RECORD_TYPES.get(row.get("record_type"), "laboratory")
        )
        codings = [coding(t.LOINC, row.get("fc_loinc"), row.get("fc_name_en"))]
        codings += self._codings_of("clinical_records", row["id"])
        if code:
            codings += _db_codings(self.data.fact_code_codings.get(code, []))
            codings.append(coding(t.FACT_CODE_SYSTEM, code, row.get("fc_name_en")))
        res: dict = {
            "resourceType": "Observation",
            "id": row_id("clinical_records", row),
            "meta": self._meta("clinical_records", row),
            "status": row["result_status"]
            if row.get("result_status") in t.OBSERVATION_STATUSES
            else "final",
            "category": [
                {
                    "coding": [
                        {
                            "system": t.OBS_CATEGORY,
                            "code": category,
                            "display": category.replace("-", " ").title(),
                        }
                    ]
                }  # fmt: skip
            ],
            # code.text is the label as printed on the document (originalText)
            "code": concept(codings, row.get("param") or row.get("fc_name_en") or code),
            "subject": self.patient_ref,
            "encounter": self.ref("encounters", row.get("encounter_id")),
            "issued": t.fhir_instant(row.get("issued_at")),
            "performer": [self._org_ref(row)],
            "derivedFrom": [self.ref("documents", row.get("source_document_id"))],
        }
        res.update(self._effective(row))
        res.update(self._obs_value(row))
        rr = self._reference_range(row)
        if rr:
            res["referenceRange"] = [rr]
        interp = t.interpretation_code(row)
        if interp:
            res["interpretation"] = [
                {"coding": [{"system": t.OBS_INTERPRETATION, "code": interp,
                             "display": t.INTERPRETATION_DISPLAY[interp]}],
                 "text": row.get("flag") or interp}
            ]  # fmt: skip
        return _clean(res)

    @staticmethod
    def _effective(row: dict) -> dict:
        meta = _meta_json(row)
        start = t.fhir_datetime(meta.get("date_range_start") or row.get("effective_start"))
        end = t.fhir_datetime(meta.get("date_range_end") or row.get("effective_end"))
        if start and end:
            return {"effectivePeriod": {"start": start, "end": end}}
        at = t.fhir_datetime(row.get("occurred_at")) or start
        return {"effectiveDateTime": at} if at else {}

    @staticmethod
    def _canonical(row: dict) -> tuple[Any, str | None, float | None]:
        """(value, unit, factor printed→canonical) for valueQuantity."""
        canon, printed = row.get("value_canonical"), row.get("value_num")
        if canon is not None:
            unit = row.get("unit_canonical") or row.get("fc_unit_canonical") or row.get("unit")
            factor = canon / printed if printed not in (None, 0) and canon != 0 else 1.0
            return canon, unit, factor
        return printed, row.get("unit_printed") or row.get("unit"), 1.0

    def _obs_value(self, row: dict) -> dict:
        value, unit, _ = self._canonical(row)
        if value is not None:
            ucum = row.get("unit_ucum") if row.get("value_canonical") is not None else None
            return {
                "valueQuantity": _quantity(
                    value, unit, ucum or t.ucum_code(unit), row.get("value_comparator")
                )
            }
        if row.get("value_text"):
            return {"valueString": str(row["value_text"])}
        return {
            "dataAbsentReason": {
                "coding": [
                    {"system": "http://terminology.hl7.org/CodeSystem/data-absent-reason",
                     "code": "unknown", "display": "Unknown"}
                ]
            }
        }  # fmt: skip

    def _reference_range(self, row: dict) -> dict | None:
        _, unit, factor = self._canonical(row)
        lo, hi = row.get("ref_range_low"), row.get("ref_range_high")

        def scaled(v):
            return None if v is None else round(v * (factor or 1.0), 6)

        rr = _clean(
            {
                "low": _quantity(scaled(lo), unit) if lo is not None else None,
                "high": _quantity(scaled(hi), unit) if hi is not None else None,
                "text": row.get("ref_text"),
            }
        )
        return rr or None

    # ── DocumentReference ─────────────────────────────────────────────

    @staticmethod
    def document_url(row: dict) -> str | None:
        if row.get("source_url"):
            return row["source_url"]
        gid = row.get("gdrive_id")
        if not gid and str(row.get("file_id") or "").startswith("gdrive:"):
            gid = str(row["file_id"])[len("gdrive:") :]
        return GDRIVE_FILE_URL.format(gid) if gid else None

    def document_reference(self, row: dict) -> dict:
        cat = row.get("category") or "other"
        loinc = t.DOC_TYPE_LOINC.get(cat)
        local = coding(t.DOC_CATEGORY_SYSTEM, cat)
        return _clean(
            {
                "resourceType": "DocumentReference",
                "id": row_id("documents", row),
                "meta": self._meta("documents", row, fact=False),
                "status": "current",
                "type": concept(
                    [coding(t.LOINC, loinc[0], loinc[1]) if loinc else None, local],
                    loinc[1] if loinc else cat,
                ),
                "category": [concept([local], cat)],
                "subject": self.patient_ref,
                "date": t.fhir_instant(row.get("created_at")),
                "author": [
                    self.ref("practitioners", row.get("practitioner_id")),
                    self._org_ref(row),
                ],
                "description": row.get("title") or row.get("filename"),
                "content": [
                    {
                        "attachment": _clean(
                            {
                                "contentType": row.get("mime_type"),
                                "url": self.document_url(row),
                                "title": row.get("filename"),
                                "size": row.get("size_bytes") or None,
                                "creation": t.fhir_datetime(row.get("document_date")),
                            }
                        )
                    }
                ],
                "context": {
                    "encounter": [self.ref("encounters", row.get("encounter_id"))],
                    "period": {"start": t.fhir_datetime(row.get("document_date"))},
                },
            }
        )

    # ── Condition ─────────────────────────────────────────────────────

    def condition(self, row: dict) -> dict:
        certainty = row.get("clinical_certainty")
        clinical = row.get("clinical_status")
        if certainty == "entered-in-error":
            clinical = None  # con-5
        stages = [
            s for s in self.data.facts.get("cancer_stages", []) if s["condition_id"] == row["id"]
        ]
        abated = clinical in (None, "inactive", "remission", "resolved")  # con-4
        body = " ".join(x for x in (row.get("body_site"), row.get("laterality")) if x)
        res = {
            "resourceType": "Condition",
            "id": row_id("conditions", row),
            "meta": self._meta("conditions", row),
            "clinicalStatus": concept([coding(t.CONDITION_CLINICAL, clinical)])
            if clinical in t.CONDITION_CLINICAL_CODES
            else None,
            "verificationStatus": concept([coding(t.CONDITION_VER_STATUS, certainty)])
            if certainty in t.CONDITION_VER_CODES
            else None,
            "category": [concept([coding(t.CONDITION_CATEGORY, row.get("category"))])]
            if row.get("category")
            else None,
            "code": concept(self._codings_of("conditions", row["id"]), row.get("printed_text")),
            "bodySite": [{"text": body}] if body else None,
            "subject": self.patient_ref,
            "encounter": self.ref("encounters", row.get("encounter_id")),
            "onsetDateTime": t.fhir_datetime(row.get("onset_at")),
            "abatementDateTime": t.fhir_datetime(row.get("abatement_at")) if abated else None,
            "recordedDate": t.fhir_datetime(row.get("recorded_at")),
            "asserter": self.ref("practitioners", row.get("asserter_practitioner_id")),
            "stage": [
                {
                    "summary": {"text": s["printed"]},
                    "type": {
                        "text": " ".join(
                            x for x in (s.get("system"), s.get("edition"), s.get("prefix")) if x
                        )
                        or None
                    },
                }
                for s in stages
            ],
            "extension": [
                {
                    "url": t.CONDITION_ASSERTED_DATE_EXT,
                    "valueDateTime": t.fhir_datetime(row.get("asserted_at")),
                }
                if t.fhir_datetime(row.get("asserted_at"))
                else None,
                {
                    "url": t.MCODE_RELATED_PRIMARY_EXT,
                    "valueReference": self.ref("conditions", row.get("primary_condition_id")),
                }
                if self.ref("conditions", row.get("primary_condition_id"))
                else None,
            ],
        }
        return _clean(res)

    # ── Procedure ─────────────────────────────────────────────────────

    def procedure(self, row: dict) -> dict:
        start = t.fhir_datetime(row.get("performed_at"))
        end = t.fhir_datetime(row.get("performed_end"))
        performer = self.ref("practitioners", row.get("performer_practitioner_id"))
        org = self._org_ref(row)
        body = " ".join(x for x in (row.get("body_site"), row.get("laterality")) if x)
        res = {
            "resourceType": "Procedure",
            "id": row_id("procedures", row),
            "meta": self._meta("procedures", row),
            "status": "completed" if start else "unknown",
            "category": concept(
                [coding(t.PROCEDURE_KIND_SYSTEM, row.get("kind"))], row.get("kind")
            ),
            "code": concept(self._codings_of("procedures", row["id"]), row.get("printed_text")),
            "subject": self.patient_ref,
            "encounter": self.ref("encounters", row.get("encounter_id")),
            "performer": [{"actor": performer, "onBehalfOf": org}]
            if performer
            else ([{"actor": org}] if org else None),
            "bodySite": [{"text": body}] if body else None,
            "outcome": {"text": row["outcome"]} if row.get("outcome") else None,
            "reasonReference": [self.ref("conditions", row.get("condition_id"))],
        }
        if start and end:
            res["performedPeriod"] = {"start": start, "end": end}
        elif start:
            res["performedDateTime"] = start
        return _clean(res)

    # ── Medication* ───────────────────────────────────────────────────

    def _medication(self, row: dict) -> dict:
        return concept(
            self._codings_of("medication_events", row["id"]),
            row.get("ingredient_printed") or row.get("inn"),
        )

    def _plan_ref(self, row: dict) -> dict | None:
        return self.ref("treatment_cycles", row.get("cycle_id")) or self.ref(
            "treatment_courses", row.get("course_id")
        )

    @staticmethod
    def _dose(row: dict) -> dict | None:
        if row.get("dose_value") is None:
            return None
        unit = row.get("dose_unit_ucum")
        return _quantity(row["dose_value"], unit, unit)

    def _dosage_text(self, row: dict) -> str | None:
        parts = [row.get("dose_printed"), row.get("timing_text")]
        if row.get("normalization_basis"):
            parts.append(f"({row['normalization_basis']})")
        return " ".join(p for p in parts if p) or None

    def medication_event(self, row: dict) -> dict | None:
        rtype = MEDICATION_KINDS.get(row.get("kind"))
        if rtype is None:
            return None
        base = {
            "resourceType": rtype,
            "id": row_id("medication_events", row),
            "meta": self._meta("medication_events", row),
            "medicationCodeableConcept": self._medication(row),
            "subject": self.patient_ref,
        }
        start = t.fhir_datetime(row.get("effective_start"))
        end = t.fhir_datetime(row.get("effective_end"))
        effective = (
            {"effectivePeriod": {"start": start, "end": end}}
            if start and end
            else ({"effectiveDateTime": start} if start else {})
        )
        status = row.get("status")
        route = {"text": row["route"]} if row.get("route") else None
        if rtype == "MedicationRequest":
            dose = self._dose(row)
            base.update(
                {
                    "status": status if status in t.MEDREQ_STATUSES else "unknown",
                    "intent": "order",
                    "encounter": self.ref("encounters", row.get("encounter_id")),
                    "authoredOn": t.fhir_datetime(row.get("authored_at")),
                    "requester": self.ref("practitioners", row.get("prescriber_practitioner_id")),
                    "basedOn": [self._plan_ref(row)],
                    "dosageInstruction": [
                        {
                            "text": self._dosage_text(row),
                            "route": route,
                            "doseAndRate": [{"doseQuantity": dose}] if dose else None,
                        }
                    ],
                }
            )
        elif rtype == "MedicationAdministration":
            base.update(
                {
                    "status": status if status in t.MEDADMIN_STATUSES else "completed",
                    "context": self.ref("encounters", row.get("encounter_id")),
                    "request": self.ref("medication_events", row.get("request_id")),
                    "supportingInformation": [self._plan_ref(row)],
                    "dosage": {
                        "text": self._dosage_text(row),
                        "route": route,
                        "dose": self._dose(row),
                    },
                    **effective,
                }
            )
            if not effective:  # effective[x] is 1..1
                base["_effectiveDateTime"] = _absent()
        else:  # MedicationStatement
            base.update(
                {
                    "status": status if status in t.MEDSTATEMENT_STATUSES else "unknown",
                    "context": self.ref("encounters", row.get("encounter_id")),
                    "basedOn": [self._plan_ref(row)],
                    "dosage": [{"text": self._dosage_text(row), "route": route}],
                    **effective,
                }
            )
        return _clean(base)

    # ── CarePlan (course and cycles) ──────────────────────────────────

    def care_plan_course(self, row: dict) -> dict:
        ext = []
        if row.get("line_of_therapy") is not None:
            ext.append({"url": t.EXT_LINE_OF_THERAPY, "valueInteger": int(row["line_of_therapy"])})
        if row.get("intent"):
            ext.append({"url": t.EXT_TREATMENT_INTENT, "valueString": row["intent"]})
        return _clean(
            {
                "resourceType": "CarePlan",
                "id": row_id("treatment_courses", row),
                "meta": self._meta("treatment_courses", row),
                "extension": ext,
                "status": row["status"] if row.get("status") in t.CAREPLAN_STATUSES else "unknown",
                "intent": "plan",
                "category": [{"text": "Treatment regimen"}],
                "title": row.get("regimen_printed"),
                "description": row.get("regimen_normalised"),
                "subject": self.patient_ref,
                "period": {
                    "start": t.fhir_datetime(row.get("start_at")),
                    "end": t.fhir_datetime(row.get("end_at")),
                },
                "addresses": [self.ref("conditions", row.get("condition_id"))],
                "note": [{"text": f"Termination reason: {row['termination_reason']}"}]
                if row.get("termination_reason")
                else None,
            }
        )

    def care_plan_cycle(self, row: dict) -> dict:
        actual = t.fhir_datetime(row.get("actual_at"))
        planned = t.fhir_datetime(row.get("planned_at"))
        number = row.get("cycle_number_printed")
        return _clean(
            {
                "resourceType": "CarePlan",
                "id": row_id("treatment_cycles", row),
                "meta": self._meta("treatment_cycles", row),
                "status": "completed" if actual else ("active" if planned else "unknown"),
                "intent": "plan",
                "category": [{"text": "Treatment cycle"}],
                "title": f"Cycle {number}" if number else "Cycle",
                "partOf": [self.ref("treatment_courses", row.get("course_id"))],
                "subject": self.patient_ref,
                "encounter": self.ref("encounters", row.get("encounter_id")),
                "period": {"start": actual or planned},
                "note": [{"text": f"Dose modification: {row['dose_modification_reason']}"}]
                if row.get("dose_modification_reason")
                else None,
            }
        )

    # ── Encounter, Practitioner, Organization, Device ─────────────────

    def encounter(self, row: dict) -> dict:
        kind = (row.get("kind") or "visit").lower()
        cls = "IMP" if kind in _INPATIENT_KINDS else "AMB"
        return _clean(
            {
                "resourceType": "Encounter",
                "id": row_id("encounters", row),
                "meta": self._meta("encounters", row, fact=False),
                "status": "unknown",
                "class": {
                    "system": t.V3_ACT_CODE,
                    "code": cls,
                    "display": "inpatient encounter" if cls == "IMP" else "ambulatory",
                },
                "type": [{"text": row.get("title") or kind}],
                "subject": self.patient_ref,
                "participant": [
                    {"individual": self.ref("practitioners", row.get("practitioner_id"))}
                ]
                if row.get("practitioner_id")
                else None,
                "period": {
                    "start": t.fhir_datetime(row.get("start_at")),
                    "end": t.fhir_datetime(row.get("end_at")),
                },
                "serviceProvider": self._org_ref(row),
            }
        )

    def _identifiers(self, owner: str, rid: int) -> list[dict]:
        return [
            {"system": i["system"], "value": i["value"]}
            for i in self.data.identifiers.get((owner, rid), [])
        ]

    def practitioner(self, row: dict) -> dict:
        return _clean(
            {
                "resourceType": "Practitioner",
                "id": row_id("practitioners", row),
                "identifier": self._identifiers("practitioner", row["id"]),
                "name": [{"text": row.get("display_name") or row.get("surname_token")}],
            }
        )

    def organization(self, row: dict) -> dict:
        return _clean(
            {
                "resourceType": "Organization",
                "id": row_id("organizations", row),
                "identifier": self._identifiers("organization", row["id"]),
                "active": True,
                "name": row.get("name") or row.get("code"),
                "alias": [row["code"]] if row.get("code") and row.get("name") else None,
            }
        )

    def department(self, row: dict) -> dict:
        return _clean(
            {
                "resourceType": "Organization",
                "id": row_id("departments", row),
                "identifier": self._identifiers("department", row["id"]),
                "active": True,
                "type": [{"text": "department"}],
                "name": row.get("name") or row.get("code"),
                "partOf": self.ref("organizations", row.get("organization_id")),
            }
        )

    def device(self, row: dict) -> dict:
        return _clean(
            {
                "resourceType": "Device",
                "id": row_id("devices", row),
                "deviceName": [{"name": row.get("name"), "type": "model-name"}],
                "type": {"text": row.get("kind")},
                "version": [{"value": row["version"]}] if row.get("version") else None,
                "note": [{"text": row["note"]}] if row.get("note") else None,
            }
        )

    def extractor_device(self, name: str) -> dict:
        """A Device for an ``extracted_by`` string that has no ``devices`` row yet."""
        dev = self.extractor_devices.get(name)
        if dev is None:
            dev = {
                "resourceType": "Device",
                "id": derived_id("extractor", name),
                "deviceName": [{"name": name, "type": "model-name"}],
                "type": {"text": "extractor"},
            }
            self.extractor_devices[name] = dev
        return dev

    # ── Provenance ────────────────────────────────────────────────────

    def _entity(self, doc_id, page, quote, offset=None, length=None) -> dict | None:
        what = self.ref("documents", doc_id)
        if what is None:
            return None
        ext = []
        if page is not None:
            ext.append({"url": t.EXT_SOURCE_PAGE, "valueInteger": int(page)})
        if quote:
            ext.append({"url": t.EXT_SOURCE_QUOTATION, "valueString": str(quote)})
        if offset is not None:
            sub = [{"url": "reference", "valueReference": dict(what)},
                   {"url": "offset", "valueInteger": int(offset)}]  # fmt: skip
            if length is not None:
                sub.append({"url": "length", "valueInteger": int(length)})
            ext.append({"url": t.DERIVATION_REFERENCE_EXT, "extension": sub})
        return _clean({"extension": ext, "role": "source", "what": what})

    @staticmethod
    def _agent(role: str, who: dict) -> dict:
        return {
            "type": {
                "coding": [{"system": t.PROVENANCE_PARTICIPANT, "code": role,
                            "display": role.title()}]
            },
            "who": who,
        }  # fmt: skip

    def provenances(self, table: str, row: dict, target: dict) -> list[dict]:
        """Provenance resources for one fact: stored rows, else one derived from the row."""
        stored = self.data.provenance.get((table, row["id"]), [])
        out = []
        for p in stored:
            role = p.get("agent_role") or "assembler"
            if role not in ("assembler", "verifier", "author", "enterer"):
                role = "assembler"
            if p.get("agent_device_id") is not None and self.ref("devices", p["agent_device_id"]):
                who = self.ref("devices", p["agent_device_id"])
            elif (
                p.get("agent_device_id") is None
                and not p.get("agent_user")
                and row.get("extracted_by")
            ):
                who = {"reference": f"Device/{self.extractor_device(row['extracted_by'])['id']}"}
            else:
                who = dict(_HUMAN)
            entities = [
                self._entity(
                    p.get("source_document_id"), p.get("page_number"), p.get("quote"),
                    p.get("char_offset"), p.get("char_length"),
                )
            ]  # fmt: skip
            if p.get("activity") == "correct" and (p.get("target_version") or 0) > 1:
                ref = target["reference"]
                entities.append(
                    {"role": "revision",
                     "what": {"reference": f"{ref}/_history/{int(p['target_version']) - 1}"}}
                )  # fmt: skip
            out.append(
                _clean(
                    {
                        "resourceType": "Provenance",
                        "id": p["fhir_id"],
                        "target": [dict(target)],
                        "recorded": t.fhir_instant(p.get("recorded_at"))
                        or t.fhir_instant(p.get("created_at")),
                        "activity": concept(
                            [coding(t.PROVENANCE_ACTIVITY_SYSTEM, p.get("activity"))]
                        ),
                        "agent": [self._agent(role, who)],
                        "entity": entities,
                        "reason": [{"text": p["reason"]}] if p.get("reason") else None,
                    }
                )
            )
        if out:
            return out
        if not (row.get("source_document_id") or row.get("extracted_by")):
            return []
        agents = []
        if row.get("extracted_by"):
            dev = self.extractor_device(row["extracted_by"])
            agents.append(self._agent("assembler", {"reference": f"Device/{dev['id']}"}))
        if row.get("verification") in ("verified", "corrected"):
            agents.append(self._agent("verifier", dict(_HUMAN)))
        if not agents:
            agents.append(self._agent("author", dict(_HUMAN)))
        return [
            _clean(
                {
                    "resourceType": "Provenance",
                    "id": derived_id("provenance-derived", f"{table}:{row['id']}"),
                    "target": [dict(target)],
                    "recorded": t.fhir_instant(row.get("created_at"))
                    or t.fhir_instant(row.get("last_updated") or row.get("updated_at")),
                    "activity": concept([coding(t.PROVENANCE_ACTIVITY_SYSTEM, "extract")])
                    if row.get("extracted_by")
                    else None,
                    "agent": agents,
                    "entity": [
                        self._entity(
                            row.get("source_document_id"),
                            row.get("page_number"),
                            row.get("evidence"),
                        )
                    ],  # fmt: skip
                }
            )
        ]

    # ── everything ────────────────────────────────────────────────────

    def resources(self) -> tuple[list[dict], dict[str, list[dict]]]:
        """All resources (Provenance included) and target reference → Provenance list."""
        d = self.data
        out: list[dict] = [self.patient()]
        facts: list[tuple[str, dict, dict]] = []
        for r in d.observations:
            facts.append(("clinical_records", r, self.observation(r)))
        for r in d.facts.get("conditions", []):
            facts.append(("conditions", r, self.condition(r)))
        for r in d.facts.get("procedures", []):
            facts.append(("procedures", r, self.procedure(r)))
        for r in d.facts.get("medication_events", []):
            res = self.medication_event(r)
            if res:
                facts.append(("medication_events", r, res))
        for r in d.facts.get("treatment_courses", []):
            facts.append(("treatment_courses", r, self.care_plan_course(r)))
        for r in d.facts.get("treatment_cycles", []):
            facts.append(("treatment_cycles", r, self.care_plan_cycle(r)))
        out += [res for _, _, res in facts]
        out += [self.document_reference(r) for r in d.documents]
        out += [self.encounter(r) for r in d.encounters]
        out += [self.practitioner(r) for r in d.practitioners]
        out += [self.organization(r) for r in d.organizations]
        out += [self.department(r) for r in d.departments]
        out += [self.device(r) for r in d.devices]
        by_target: dict[str, list[dict]] = {}
        provs: list[dict] = []
        for table, row, res in facts:
            ref = {"reference": f"{res['resourceType']}/{res['id']}"}
            for p in self.provenances(table, row, ref):
                provs.append(p)
                by_target.setdefault(ref["reference"], []).append(p)
        out += list(self.extractor_devices.values())
        out += provs
        return out, by_target
