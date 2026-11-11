"""Code systems, small mapping tables, UCUM units and FHIR date helpers.

Only system URIs and a handful of individual codes live here, never a code list
(licensing rules in the design §2: no SNOMED RF2, no full ICD/MKCH lists, no
TNM/AJCC content). LOINC codes are used under the LOINC licence (notice in the
repo).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

# ── system URIs ──────────────────────────────────────────────────────────────

LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
SNOMED = "http://snomed.info/sct"
ICD10_WHO = "http://hl7.org/fhir/sid/icd-10"  # WHO ICD-10, never ICD-10-CM
ICD_O_3 = "http://terminology.hl7.org/CodeSystem/icd-o-3"
ATC = "http://www.whocc.no/atc"
MKCH10 = "urn:oid:1.3.158.00165387.100.10.25"  # SK national diagnosis list (≈ WHO ICD-10)
NCZI_LAB = "urn:oid:1.3.158.00165387.100.10.52"  # SK national lab-item list

OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
OBS_INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
CONDITION_CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"
CONDITION_VER_STATUS = "http://terminology.hl7.org/CodeSystem/condition-ver-status"
CONDITION_CATEGORY = "http://terminology.hl7.org/CodeSystem/condition-category"
PROVENANCE_PARTICIPANT = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
V3_OBSERVATION_VALUE = "http://terminology.hl7.org/CodeSystem/v3-ObservationValue"
V3_ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
LIST_EMPTY_REASON = "http://terminology.hl7.org/CodeSystem/list-empty-reason"
DATA_ABSENT_REASON_EXT = "http://hl7.org/fhir/StructureDefinition/data-absent-reason"
DERIVATION_REFERENCE_EXT = "http://hl7.org/fhir/StructureDefinition/derivation-reference"
CONDITION_ASSERTED_DATE_EXT = "http://hl7.org/fhir/StructureDefinition/condition-assertedDate"
MCODE_RELATED_PRIMARY_EXT = (
    "http://hl7.org/fhir/us/mcode/StructureDefinition/mcode-related-primary-cancer-condition"
)

# our own vocabularies (URNs, so nobody mistakes them for a published system)
VERIFICATION_SYSTEM = "urn:oncofiles:verification"
FACT_CODE_SYSTEM = "urn:oncofiles:fact-code"
DOC_CATEGORY_SYSTEM = "urn:oncofiles:document-category"
PROCEDURE_KIND_SYSTEM = "urn:oncofiles:procedure-kind"
PROVENANCE_ACTIVITY_SYSTEM = "urn:oncofiles:provenance-activity"
EXT_BASE = "https://oncofiles.com/fhir/StructureDefinition/"
EXT_SOURCE_PAGE = EXT_BASE + "source-page"
EXT_SOURCE_QUOTATION = EXT_BASE + "source-quotation"
EXT_LINE_OF_THERAPY = EXT_BASE + "line-of-therapy"
EXT_TREATMENT_INTENT = EXT_BASE + "treatment-intent"

# AI Transparency IG (ballot): AI-asserted content carries this security label.
AIAST = {
    "system": V3_OBSERVATION_VALUE,
    "code": "AIAST",
    "display": "Artificial Intelligence asserted",
}

# US-only systems never leave Oncofiles (design §2 "Never" column).
FORBIDDEN_SYSTEMS = frozenset(
    {
        "http://hl7.org/fhir/sid/icd-10-cm",
        "http://www.cms.gov/Medicare/Coding/ICD10",
        "http://www.nlm.nih.gov/research/umls/rxnorm",
    }
)

# Free-text aliases a writer may have stored in ``codings.system`` → canonical URI.
_SYSTEM_ALIASES = {
    "loinc": LOINC,
    "ucum": UCUM,
    "snomed": SNOMED,
    "snomed-ct": SNOMED,
    "sct": SNOMED,
    "icd-10": ICD10_WHO,
    "icd10": ICD10_WHO,
    "who-icd-10": ICD10_WHO,
    "icd-o-3": ICD_O_3,
    "icdo3": ICD_O_3,
    "atc": ATC,
    "mkch-10": MKCH10,
    "mkch10": MKCH10,
    "icd-10-cm": "http://hl7.org/fhir/sid/icd-10-cm",
    "rxnorm": "http://www.nlm.nih.gov/research/umls/rxnorm",
}
_OID = re.compile(r"^[0-2](\.(0|[1-9]\d*|0\d+))+$")


def canonical_system(system: str | None) -> str | None:
    """Normalise a stored system to a URI (bare OIDs become ``urn:oid:…``)."""
    if not system:
        return None
    s = system.strip()
    if s.lower() in _SYSTEM_ALIASES:
        return _SYSTEM_ALIASES[s.lower()]
    if _OID.match(s):
        return f"urn:oid:{s}"
    return s


def allowed_system(system: str | None) -> bool:
    return bool(system) and system not in FORBIDDEN_SYSTEMS


# ── documents: category → LOINC document type (small table) ────────────────

DOC_TYPE_LOINC: dict[str, tuple[str, str]] = {
    "labs": ("11502-2", "Laboratory report"),
    "report": ("11488-4", "Consult note"),
    "imaging": ("18748-4", "Diagnostic imaging study"),
    "pathology": ("11526-1", "Pathology study"),
    "genetics": ("51969-4", "Genetic analysis report"),
    "surgery": ("11504-8", "Surgical operation note"),
    "surgical_report": ("11504-8", "Surgical operation note"),
    "prescription": ("57833-6", "Prescription for medication"),
    "referral": ("57133-1", "Referral note"),
    "discharge": ("18842-5", "Discharge summary"),
    "discharge_summary": ("18842-5", "Discharge summary"),
    "chemo_sheet": ("18776-5", "Plan of care note"),
    "vaccination": ("11369-6", "History of Immunization Narrative"),
}
# reference, advocate, other, dental, preventive: local category coding only.


# ── observations ─────────────────────────────────────────────────────────────

# clinical_records.record_type → FHIR observation-category code
OBSERVATION_RECORD_TYPES: dict[str, str] = {
    "lab": "laboratory",
    "tumor_marker": "laboratory",
    "biomarker": "laboratory",
    "genetic_variant": "laboratory",
    "vital": "vital-signs",
}
# codes that are not vital signs although stored as vitals
OBSERVATION_CATEGORY_OVERRIDE = {"ECOG": "survey"}

INTERPRETATION_DISPLAY = {
    "H": "High",
    "L": "Low",
    "HH": "Critical high",
    "LL": "Critical low",
    "A": "Abnormal",
    "AA": "Critical abnormal",
    "N": "Normal",
}
_FLAG_TO_INTERPRETATION = {"h": "H", "l": "L", "hh": "HH", "ll": "LL", "a": "A", "n": "N"}

OBSERVATION_STATUSES = frozenset(
    {"registered", "preliminary", "final", "amended", "corrected", "cancelled",
     "entered-in-error", "unknown"}
)  # fmt: skip


def interpretation_code(row: dict) -> str | None:
    code = row.get("interpretation_code")
    if code and code.upper() in INTERPRETATION_DISPLAY:
        return code.upper()
    flag = (row.get("flag") or "").strip().lower()
    return _FLAG_TO_INTERPRETATION.get(flag)


# Our canonical units (fact_codes.unit_canonical) → UCUM case-sensitive codes.
_UCUM: dict[str, str] = {
    "10^9/L": "10*9/L",
    "10^12/L": "10*12/L",
    "g/L": "g/L",
    "L/L": "L/L",
    "fL": "fL",
    "pg": "pg",
    "%": "%",
    "mmol/L": "mmol/L",
    "µmol/L": "umol/L",
    "umol/L": "umol/L",
    "µkat/L": "ukat/L",
    "ukat/L": "ukat/L",
    "µg/L": "ug/L",
    "ug/L": "ug/L",
    "mg/L": "mg/L",
    "kU/L": "k[U]/L",
    "U/L": "U/L",
    "s": "s",
    "ml/s": "mL/s",
    "mL/s": "mL/s",
    "/µl": "/uL",
    "/uL": "/uL",
    "kg": "kg",
    "cm": "cm",
    "m2": "m2",
    "mmHg": "mm[Hg]",
    "°C": "Cel",
    "mg": "mg",
    "mg/m2": "mg/m2",
    "mg/kg": "mg/kg",
    "ml": "mL",
    "mL": "mL",
}


def ucum_code(unit: str | None) -> str | None:
    """UCUM code for one of our units, or None when we do not know it."""
    if not unit:
        return None
    return _UCUM.get(unit.strip())


# ── conditions / medications / careplans: FHIR value sets ──────────────────

CONDITION_CLINICAL_CODES = frozenset(
    {"active", "recurrence", "relapse", "inactive", "remission", "resolved"}
)
CONDITION_VER_CODES = frozenset(
    {"unconfirmed", "provisional", "differential", "confirmed", "refuted", "entered-in-error"}
)
MEDREQ_STATUSES = frozenset(
    {"active", "on-hold", "cancelled", "completed", "entered-in-error", "stopped", "draft",
     "unknown"}
)  # fmt: skip
MEDADMIN_STATUSES = frozenset(
    {"in-progress", "not-done", "on-hold", "completed", "entered-in-error", "stopped", "unknown"}
)
MEDSTATEMENT_STATUSES = frozenset(
    {"active", "completed", "entered-in-error", "intended", "stopped", "on-hold", "unknown",
     "not-taken"}
)  # fmt: skip
CAREPLAN_STATUSES = frozenset(
    {"draft", "active", "on-hold", "revoked", "completed", "entered-in-error", "unknown"}
)
PROCEDURE_STATUSES = frozenset(
    {"preparation", "in-progress", "not-done", "on-hold", "stopped", "completed",
     "entered-in-error", "unknown"}
)  # fmt: skip


# ── dates ────────────────────────────────────────────────────────────────────

_PARTIAL_DATE = re.compile(r"^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?$")
_DATETIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


def _local_tz():
    """Naive times printed on Slovak documents are local (Europe/Bratislava)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Europe/Bratislava")
    except Exception:  # pragma: no cover - tzdata missing
        return UTC


def fhir_datetime(value: str | None) -> str | None:
    """A FHIR ``dateTime`` (partial dates allowed), or None when unparseable.

    A time without an offset gets the Europe/Bratislava offset of that day,
    because FHIR requires an offset whenever a time is present.
    """
    if not value:
        return None
    s = str(value).strip()
    if _PARTIAL_DATE.match(s):
        return s
    m = _DATETIME.match(s)
    if not m:
        return None
    day, hh, mm, ss, frac, tz = m.groups()
    try:
        dt = datetime.fromisoformat(f"{day}T{hh}:{mm}:{ss or '00'}")
    except ValueError:
        return None
    if tz:
        if tz == "Z":
            dt = dt.replace(tzinfo=UTC)
        else:
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            dt = dt.replace(
                tzinfo=timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
            )
    else:
        dt = dt.replace(tzinfo=_local_tz())
    out = dt.isoformat(timespec="seconds")
    return out.replace("+00:00", "Z") if dt.utcoffset() == timedelta(0) and tz == "Z" else out


def fhir_instant(value: str | None) -> str | None:
    """A FHIR ``instant`` (full date-time with offset), or None."""
    dt = fhir_datetime(value)
    return dt if dt and "T" in dt else None


def fhir_date(value: str | None) -> str | None:
    """A FHIR ``date`` (no time part), or None."""
    dt = fhir_datetime(value)
    return dt[:10] if dt and len(dt) >= 10 else dt
