"""A small, hand-written FHIR R4 structural check.

Not a replacement for the official HL7 validator (Java; a CI follow-up). It
checks what our projection could plausibly get wrong: required elements and
their cardinality, choice types (``value[x]`` at most one), allowed top-level
elements, id / date / instant formats, Reference / Coding shape, empty
elements, forbidden code systems, and a few invariants (obs-3, con-5, cmp-1/2,
bdl-9/10, document fullUrls). ``problems(resource)`` returns a list of messages;
empty means structurally valid.
"""

from __future__ import annotations

import re
from typing import Any

from oncofiles.fhir import terminology as t

_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
_DATE = re.compile(r"^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?$")
_DATETIME = re.compile(
    r"^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01])(T([01]\d|2[0-3]):[0-5]\d:([0-5]\d|60)"
    r"(\.\d+)?(Z|[+-]((0\d|1[0-3]):[0-5]\d|14:00)))?)?)?$"
)
_INSTANT = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:([0-5]\d|60)"
    r"(\.\d+)?(Z|[+-]((0\d|1[0-3]):[0-5]\d|14:00))$"
)
_COMMON = {"resourceType", "id", "meta", "implicitRules", "language", "text", "contained",
           "extension", "modifierExtension"}  # fmt: skip

# type → (required elements, allowed elements, 1..1 choice bases, 0..1 choice bases)
SPEC: dict[str, dict[str, Any]] = {
    "Patient": {
        "required": [],
        "allowed": {"identifier", "active", "name", "telecom", "gender", "birthDate", "address",
                    "deceasedBoolean", "deceasedDateTime", "maritalStatus", "contact",
                    "communication", "generalPractitioner", "managingOrganization", "link"},
    },
    "Observation": {
        "required": ["status", "code"],
        "allowed": {"identifier", "basedOn", "partOf", "status", "category", "code", "subject",
                    "focus", "encounter", "effectiveDateTime", "effectivePeriod",
                    "effectiveInstant", "issued", "performer", "valueQuantity",
                    "valueCodeableConcept", "valueString", "valueBoolean", "valueInteger",
                    "dataAbsentReason", "interpretation", "note", "bodySite", "method",
                    "specimen", "device", "referenceRange", "hasMember", "derivedFrom",
                    "component"},
        "choice01": ["effective", "value"],
    },
    "Condition": {
        "required": ["subject"],
        "allowed": {"identifier", "clinicalStatus", "verificationStatus", "category", "severity",
                    "code", "bodySite", "subject", "encounter", "onsetDateTime", "onsetPeriod",
                    "onsetString", "abatementDateTime", "abatementPeriod", "abatementString",
                    "recordedDate", "recorder", "asserter", "stage", "evidence", "note"},
        "choice01": ["onset", "abatement"],
    },
    "Procedure": {
        "required": ["status", "subject"],
        "allowed": {"identifier", "basedOn", "partOf", "status", "statusReason", "category",
                    "code", "subject", "encounter", "performedDateTime", "performedPeriod",
                    "recorder", "asserter", "performer", "location", "reasonCode",
                    "reasonReference", "bodySite", "outcome", "report", "complication",
                    "followUp", "note", "usedCode"},
        "choice01": ["performed"],
    },
    "MedicationRequest": {
        "required": ["status", "intent", "subject"],
        "allowed": {"identifier", "status", "statusReason", "intent", "category", "priority",
                    "medicationCodeableConcept", "medicationReference", "subject", "encounter",
                    "authoredOn", "requester", "reasonCode", "reasonReference", "basedOn",
                    "note", "dosageInstruction", "dispenseRequest"},
        "choice11": ["medication"],
    },
    "MedicationAdministration": {
        "required": ["status", "subject"],
        "allowed": {"identifier", "partOf", "status", "statusReason", "category",
                    "medicationCodeableConcept", "medicationReference", "subject", "context",
                    "supportingInformation", "effectiveDateTime", "effectivePeriod",
                    "_effectiveDateTime", "performer", "reasonCode", "reasonReference",
                    "request", "device", "note", "dosage"},
        "choice11": ["medication", "effective"],
    },
    "MedicationStatement": {
        "required": ["status", "subject"],
        "allowed": {"identifier", "basedOn", "partOf", "status", "statusReason", "category",
                    "medicationCodeableConcept", "medicationReference", "subject", "context",
                    "effectiveDateTime", "effectivePeriod", "dateAsserted", "informationSource",
                    "derivedFrom", "reasonCode", "reasonReference", "note", "dosage"},
        "choice11": ["medication"],
        "choice01": ["effective"],
    },
    "CarePlan": {
        "required": ["status", "intent", "subject"],
        "allowed": {"identifier", "basedOn", "replaces", "partOf", "status", "intent",
                    "category", "title", "description", "subject", "encounter", "period",
                    "created", "author", "contributor", "careTeam", "addresses",
                    "supportingInfo", "goal", "activity", "note"},
    },
    "DocumentReference": {
        "required": ["status", "content"],
        "allowed": {"masterIdentifier", "identifier", "status", "docStatus", "type", "category",
                    "subject", "date", "author", "authenticator", "custodian", "relatesTo",
                    "description", "securityLabel", "content", "context"},
    },
    "Encounter": {
        "required": ["status", "class"],
        "allowed": {"identifier", "status", "statusHistory", "class", "classHistory", "type",
                    "serviceType", "priority", "subject", "episodeOfCare", "basedOn",
                    "participant", "appointment", "period", "length", "reasonCode",
                    "reasonReference", "diagnosis", "account", "hospitalization", "location",
                    "serviceProvider", "partOf"},
    },
    "Practitioner": {
        "required": [],
        "allowed": {"identifier", "active", "name", "telecom", "address", "gender",
                    "birthDate", "photo", "qualification", "communication"},
    },
    "Organization": {
        "required": [],
        "allowed": {"identifier", "active", "type", "name", "alias", "telecom", "address",
                    "partOf", "contact", "endpoint"},
    },
    "Device": {
        "required": [],
        "allowed": {"identifier", "definition", "udiCarrier", "status", "statusReason",
                    "distinctIdentifier", "manufacturer", "manufactureDate", "expirationDate",
                    "lotNumber", "serialNumber", "deviceName", "modelNumber", "partNumber",
                    "type", "specialization", "version", "property", "patient", "owner",
                    "contact", "location", "url", "note", "safety", "parent"},
    },
    "Provenance": {
        "required": ["target", "recorded", "agent"],
        "allowed": {"target", "occurredPeriod", "occurredDateTime", "recorded", "policy",
                    "location", "reason", "activity", "agent", "entity", "signature"},
    },
    "Composition": {
        "required": ["status", "type", "date", "author", "title"],
        "allowed": {"identifier", "status", "type", "category", "subject", "encounter", "date",
                    "author", "title", "confidentiality", "attester", "custodian",
                    "relatesTo", "event", "section"},
    },
    "Bundle": {
        "required": ["type"],
        "allowed": {"identifier", "type", "timestamp", "total", "link", "entry", "signature"},
    },
    "CapabilityStatement": {
        "required": ["status", "date", "kind", "fhirVersion", "format"],
        "allowed": {"url", "version", "name", "title", "status", "experimental", "date",
                    "publisher", "contact", "description", "useContext", "jurisdiction",
                    "purpose", "copyright", "kind", "instantiates", "imports", "software",
                    "implementation", "fhirVersion", "format", "patchFormat",
                    "implementationGuide", "rest", "messaging", "document"},
    },
    "OperationOutcome": {"required": ["issue"], "allowed": {"issue"}},
}  # fmt: skip

_DATE_FIELDS = {"birthDate"}
_DATETIME_FIELDS = {"effectiveDateTime", "onsetDateTime", "abatementDateTime", "recordedDate",
                    "performedDateTime", "authoredOn", "start", "end", "creation"}  # fmt: skip
_INSTANT_FIELDS = {"issued", "lastUpdated", "recorded", "timestamp"}
_LIST_FIELDS = {"performer", "derivedFrom", "referenceRange", "interpretation", "coding", "tag",
                "security", "extension", "stage", "bodySite", "agent", "entity", "target",
                "content", "author", "section", "entry", "link", "name", "alias", "deviceName",
                "version", "note", "basedOn", "addresses", "dosageInstruction",
                "reasonReference", "participant", "supportingInformation", "issue"}  # fmt: skip
# top-level elements whose cardinality differs per type: type → {element: is_list}
_TOP_CARDINALITY = {
    "category": {"Observation": True, "Condition": True, "CarePlan": True,
                 "DocumentReference": True, "Procedure": False},
    "partOf": {"CarePlan": True, "Organization": False, "Encounter": False},
    "identifier": {"Bundle": False, "Composition": False},
}  # fmt: skip


def _walk(obj: Any, path: str, out: list[str]) -> None:
    if isinstance(obj, dict):
        if not obj:
            out.append(f"{path}: empty object")
        for k, v in obj.items():
            p = f"{path}.{k}"
            if v in (None, "", [], {}):
                out.append(f"{p}: empty value")
                continue
            if k in _DATE_FIELDS and not _DATE.match(str(v)):
                out.append(f"{p}: invalid date {v!r}")
            if k in _DATETIME_FIELDS and isinstance(v, str) and not _DATETIME.match(v):
                out.append(f"{p}: invalid dateTime {v!r}")
            if k in _INSTANT_FIELDS and isinstance(v, str) and not _INSTANT.match(v):
                out.append(f"{p}: invalid instant {v!r}")
            # 'name' on Organization / Device.deviceName is a string
            scalar_ok = k in ("name", "version") and isinstance(v, str)
            if k in _LIST_FIELDS and not isinstance(v, list) and not scalar_ok:
                out.append(f"{p}: must be an array")
            if k == "reference" and isinstance(v, str):
                ok = v.startswith("urn:uuid:") or re.match(
                    r"^[A-Z][A-Za-z]+/[A-Za-z0-9\-.]{1,64}(/_history/[A-Za-z0-9\-.]{1,64})?$", v
                )
                if not ok:
                    out.append(f"{p}: malformed reference {v!r}")
            if k == "system" and isinstance(v, str):
                if v in t.FORBIDDEN_SYSTEMS:
                    out.append(f"{p}: forbidden code system {v}")
                if "icd-10-cm" in v.lower() or "rxnorm" in v.lower():
                    out.append(f"{p}: US-only code system {v}")
            if k == "coding" and isinstance(v, list):
                for i, c in enumerate(v):
                    if not isinstance(c, dict) or ("code" in c and not str(c["code"]).strip()):
                        out.append(f"{p}[{i}]: malformed coding")
            if k == "resource" and isinstance(v, dict):
                out.extend(problems(v, f"{p}"))
                continue
            _walk(v, p, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if v in (None, "", [], {}):
                out.append(f"{path}[{i}]: empty item")
            _walk(v, f"{path}[{i}]", out)


def problems(res: dict, path: str | None = None) -> list[str]:
    """Structural problems of one resource (recursing into Bundle entries)."""
    out: list[str] = []
    rtype = res.get("resourceType")
    path = path or str(rtype)
    spec = SPEC.get(rtype or "")
    if spec is None:
        return [f"{path}: unknown resourceType {rtype!r}"]
    if "id" in res and not _ID.match(str(res["id"])):
        out.append(f"{path}.id: invalid id {res['id']!r}")
    if rtype not in ("Bundle", "CapabilityStatement", "OperationOutcome") and "id" not in res:
        out.append(f"{path}: id missing")
    for el in spec["required"]:
        if el not in res:
            out.append(f"{path}: required element {el!r} missing")
    allowed = spec["allowed"] | _COMMON
    for k in res:
        if k not in allowed:
            out.append(f"{path}: element {k!r} is not allowed on {rtype}")
    for base in spec.get("choice11", []):
        n = sum(1 for k in res if k.startswith(base) and k[len(base) :][:1].isupper())
        n += sum(1 for k in res if k.startswith(f"_{base}"))
        if n != 1:
            out.append(f"{path}: {base}[x] must occur exactly once (found {n})")
    for base in spec.get("choice01", []):
        n = sum(1 for k in res if k.startswith(base) and k[len(base) :][:1].isupper())
        if n > 1:
            out.append(f"{path}: {base}[x] occurs {n} times")
    if rtype == "Observation":
        if "dataAbsentReason" in res and any(k.startswith("value") for k in res):
            out.append(f"{path}: obs-6 dataAbsentReason with a value")
        for rr in res.get("referenceRange", []):
            if not any(k in rr for k in ("low", "high", "text")):
                out.append(f"{path}: obs-3 referenceRange needs low, high or text")
    if rtype == "Condition":
        ver = (res.get("verificationStatus") or {}).get("coding", [{}])[0].get("code")
        if ver == "entered-in-error" and "clinicalStatus" in res:
            out.append(f"{path}: con-5 clinicalStatus with entered-in-error")
    if rtype == "Provenance":
        for a in res.get("agent", []):
            if "who" not in a:
                out.append(f"{path}: Provenance.agent.who is required")
        for e in res.get("entity", []):
            if "role" not in e or "what" not in e:
                out.append(f"{path}: Provenance.entity needs role and what")
    if rtype == "DocumentReference":
        for c in res.get("content", []):
            if "attachment" not in c:
                out.append(f"{path}: content.attachment is required")
    if rtype == "Composition":
        for i, s in enumerate(res.get("section", [])):
            if not any(k in s for k in ("text", "entry", "section")):
                out.append(f"{path}.section[{i}]: cmp-1")
            if "emptyReason" in s and s.get("entry"):
                out.append(f"{path}.section[{i}]: cmp-2 emptyReason with entries")
    if rtype == "Bundle":
        if res.get("type") == "document":
            for el in ("identifier", "timestamp"):
                if el not in res:
                    out.append(f"{path}: bdl-9/10 document needs {el}")
            entries = res.get("entry", [])
            if not entries or entries[0].get("resource", {}).get("resourceType") != "Composition":
                out.append(f"{path}: a document Bundle starts with a Composition")
            for e in entries:
                if "fullUrl" not in e:
                    out.append(f"{path}: bdl-7 every document entry needs a fullUrl")
        if "total" in res and res.get("type") not in ("searchset", "history"):
            out.append(f"{path}: bdl-1 total only on searchset/history")
    if rtype == "CapabilityStatement" and res.get("fhirVersion") != "4.0.1":
        out.append(f"{path}: fhirVersion must be 4.0.1")
    for el, by_type in _TOP_CARDINALITY.items():
        if el in res and rtype in by_type and isinstance(res[el], list) != by_type[rtype]:
            out.append(f"{path}.{el}: wrong cardinality for {rtype}")
    if (
        "identifier" in res
        and rtype not in _TOP_CARDINALITY["identifier"]
        and not isinstance(res["identifier"], list)
    ):
        out.append(f"{path}.identifier: must be an array")
    body = {k: v for k, v in res.items() if k != "resourceType"}
    _walk(body, path, out)
    return out
