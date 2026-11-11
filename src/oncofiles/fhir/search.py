"""Search, read, $everything and the IPS summary over one patient's projection.

Everything here works on a ``PatientIndex`` built from ``store.load_patient``
(one patient, patient_id filtered in SQL), so no search can reach another
patient's rows. Responses are bounded: ``_count`` (max 200) and a byte budget of
~150 kB per page; ``page_token`` continues a search.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from oncofiles.fhir import FHIR_VERSION
from oncofiles.fhir import terminology as t
from oncofiles.fhir.ids import derived_id
from oncofiles.fhir.projection import Projector
from oncofiles.fhir.store import PatientData

PAGE_BYTE_BUDGET = 150_000
DEFAULT_COUNT = 50
MAX_COUNT = 200

_TOKEN, _DATE, _REF = "token", "date", "reference"
_COMMON = {"_id": _TOKEN, "_tag": _TOKEN}

# type → supported search parameters (design §4.1). ``date`` is accepted on every
# dated type and maps to that type's clinically relevant date.
SEARCH_PARAMS: dict[str, dict[str, str]] = {
    "Patient": {},
    "Observation": {"category": _TOKEN, "code": _TOKEN, "date": _DATE, "status": _TOKEN},
    "Condition": {
        "category": _TOKEN,
        "code": _TOKEN,
        "clinical-status": _TOKEN,
        "verification-status": _TOKEN,
        "onset-date": _DATE,
        "date": _DATE,
    },  # fmt: skip
    "Procedure": {"category": _TOKEN, "code": _TOKEN, "date": _DATE, "status": _TOKEN},
    "MedicationRequest": {"code": _TOKEN, "status": _TOKEN, "authoredon": _DATE, "date": _DATE},
    "MedicationAdministration": {
        "code": _TOKEN,
        "status": _TOKEN,
        "effective-time": _DATE,
        "date": _DATE,
    },  # fmt: skip
    "MedicationStatement": {"code": _TOKEN, "status": _TOKEN, "effective": _DATE, "date": _DATE},
    "CarePlan": {"category": _TOKEN, "status": _TOKEN, "date": _DATE},
    "DocumentReference": {"type": _TOKEN, "category": _TOKEN, "status": _TOKEN, "date": _DATE},
    "Encounter": {"class": _TOKEN, "status": _TOKEN, "date": _DATE},
    "Practitioner": {},
    "Organization": {},
    "Device": {},
    "Provenance": {"target": _REF},
}
SEARCH_TYPES = tuple(SEARCH_PARAMS)
REVINCLUDE = "Provenance:target"
# Types the design lists but this phase does not project yet (follow-ups).
NOT_YET = ("DiagnosticReport", "Specimen", "PractitionerRole", "MedicationDispense")

_CODE_ELEMENT = {
    "Observation": "code",
    "Condition": "code",
    "Procedure": "code",
    "MedicationRequest": "medicationCodeableConcept",
    "MedicationAdministration": "medicationCodeableConcept",
    "MedicationStatement": "medicationCodeableConcept",
}


class SearchError(ValueError):
    """A bad request: unknown type, parameter or token. Rendered as OperationOutcome."""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def operation_outcome(message: str, code: str = "invalid", severity: str = "error") -> dict:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": severity, "code": code, "diagnostics": message}],
    }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── index ────────────────────────────────────────────────────────────────────


class PatientIndex:
    """Every resource of one patient, by ``Type/id``, plus Provenance by target."""

    def __init__(self, data: PatientData):
        self.data = data
        self.projector = Projector(data)
        self.resources, self.provenance_by_target = self.projector.resources()
        self.by_key = {f"{r['resourceType']}/{r['id']}": r for r in self.resources}

    @property
    def patient(self) -> dict:
        return self.resources[0]

    def of_type(self, rtype: str) -> list[dict]:
        return [r for r in self.resources if r["resourceType"] == rtype]

    def get(self, rtype: str, rid: str) -> dict | None:
        # TODO(#728): also resolve fact ids minted by fact_identity once it lands.
        return self.by_key.get(f"{rtype}/{rid}")


# ── matching ─────────────────────────────────────────────────────────────────


def _codeable_concepts(res: dict, param: str) -> list[dict]:
    rtype = res["resourceType"]
    if param == "code":
        el = _CODE_ELEMENT.get(rtype)
        return [res[el]] if el and el in res else []
    if param == "type":
        return [res["type"]] if isinstance(res.get("type"), dict) else []
    if param == "category":
        cat = res.get("category")
        return cat if isinstance(cat, list) else ([cat] if isinstance(cat, dict) else [])
    if param == "clinical-status":
        return [res["clinicalStatus"]] if "clinicalStatus" in res else []
    if param == "verification-status":
        return [res["verificationStatus"]] if "verificationStatus" in res else []
    if param == "class":
        return [{"coding": [res["class"]]}] if "class" in res else []
    if param == "_tag":
        return [{"coding": res.get("meta", {}).get("tag", [])}]
    return []


def _token_match(concepts: list[dict], value: str, text_mode: bool) -> bool:
    if text_mode:
        needle = value.lower()
        for cc in concepts:
            texts = [cc.get("text") or ""] + [c.get("display") or "" for c in cc.get("coding", [])]
            if any(needle in x.lower() for x in texts):
                return True
        return False
    if "|" in value:
        system, code = value.split("|", 1)
    else:
        system, code = None, value
    for cc in concepts:
        for c in cc.get("coding", []):
            if system is not None and system != "" and c.get("system") != system:
                continue
            if system == "" and c.get("system"):
                continue
            if code == "" or c.get("code") == code:
                return True
    return False


def _status_of(res: dict) -> str | None:
    return res.get("status")


def _interval(res: dict) -> tuple[str, str] | None:
    """(low, high) ISO dates of a resource's main date, padded for partial dates."""
    rtype = res["resourceType"]
    lo = hi = None
    if rtype == "Observation":
        lo, hi = _from_choice(res, "effective")
    elif rtype == "Condition":
        lo = res.get("onsetDateTime") or res.get("recordedDate")
    elif rtype == "Procedure":
        lo, hi = _from_choice(res, "performed")
    elif rtype == "MedicationRequest":
        lo = res.get("authoredOn")
    elif rtype in ("MedicationAdministration", "MedicationStatement"):
        lo, hi = _from_choice(res, "effective")
    elif rtype in ("CarePlan", "Encounter"):
        p = res.get("period") or {}
        lo, hi = p.get("start"), p.get("end")
    elif rtype == "DocumentReference":
        lo = (res.get("context", {}).get("period") or {}).get("start") or res.get("date")
    if not lo and not hi:
        return None
    lo, hi = lo or hi, hi or lo
    return _pad(lo, low=True), _pad(hi, low=False)


def _from_choice(res: dict, base: str) -> tuple[str | None, str | None]:
    if f"{base}DateTime" in res:
        return res[f"{base}DateTime"], None
    p = res.get(f"{base}Period") or {}
    return p.get("start"), p.get("end")


def _pad(value: str, *, low: bool) -> str:
    day = value[:10]
    if len(day) == 4:
        return day + ("-01-01" if low else "-12-31")
    if len(day) == 7:
        return day + ("-01" if low else "-31")
    return day


_PREFIXES = ("eq", "ne", "gt", "lt", "ge", "le")


def _date_match(res: dict, value: str) -> bool:
    prefix = value[:2] if value[:2] in _PREFIXES else "eq"
    raw = value[2:] if value[:2] in _PREFIXES else value
    if not t.fhir_datetime(raw):
        raise SearchError(f"invalid date value {value!r} (use e.g. ge2027-01-01)")
    iv = _interval(res)
    if iv is None:
        return False
    lo, hi = iv
    plo, phi = _pad(raw, low=True), _pad(raw, low=False)
    return {
        "eq": lo >= plo and hi <= phi,
        "ne": not (lo >= plo and hi <= phi),
        "gt": hi > phi,
        "lt": lo < plo,
        "ge": hi >= plo,
        "le": lo <= phi,
    }[prefix]


def _one_value_matches(res: dict, name: str, modifier: str | None, kind: str, value: str) -> bool:
    if name == "_id":
        return res["id"] == value
    if kind == _DATE:
        return _date_match(res, value)
    if kind == _REF:  # Provenance?target=Observation/<id>
        return any(r.get("reference") == value for r in res.get("target", []))
    if name == "status":
        return _status_of(res) == value
    return _token_match(_codeable_concepts(res, name), value, modifier == "text")


def _parse_params(rtype: str, params: dict[str, Any] | None) -> tuple[list, dict]:
    """→ ([(name, modifier, kind, [and-values → [or-values]])], control params)."""
    allowed = {**_COMMON, **SEARCH_PARAMS[rtype]}
    filters, control = [], {}
    for key, raw in (params or {}).items():
        name, _, modifier = str(key).partition(":")
        if name in ("_count", "_revinclude", "_elements", "page_token"):
            control[name] = raw
            continue
        if name not in allowed:
            raise SearchError(
                f"search parameter {name!r} is not supported for {rtype}. "
                f"Supported: {sorted(allowed)} + _count, _elements, _revinclude=Provenance:target",
                code="not-supported",
            )
        if modifier and modifier != "text":
            raise SearchError(
                f"modifier :{modifier} is not supported (only :text)", "not-supported"
            )
        if modifier == "text" and allowed[name] != _TOKEN:
            raise SearchError(f":text applies to token parameters, not {name!r}", "not-supported")
        values = raw if isinstance(raw, list) else [raw]
        ands = [[v.strip() for v in str(v).split(",") if v.strip()] for v in values]
        filters.append((name, modifier or None, allowed[name], [a for a in ands if a]))
    return filters, control


def _matches(res: dict, filters: list) -> bool:
    for name, modifier, kind, ands in filters:
        for ors in ands:
            if not any(_one_value_matches(res, name, modifier, kind, v) for v in ors):
                return False
    return True


def _sort_key(res: dict) -> tuple:
    iv = _interval(res)
    return (0 if iv else 1, "" if not iv else _neg(iv[0]), res["id"])


def _neg(day: str) -> str:
    """Sort newest first with an ascending sort: invert digits."""
    return "".join(chr(ord("9") - ord(c) + ord("0")) if c.isdigit() else c for c in day)


# ── paging ───────────────────────────────────────────────────────────────────


def _query_digest(patient_id: str, op: str, payload: Any) -> str:
    raw = json.dumps([patient_id, op, payload], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _encode_token(offset: int, digest: str) -> str:
    raw = json.dumps({"o": offset, "q": digest}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_token(token: str | None, digest: str) -> int:
    if not token:
        return 0
    try:
        pad = "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(token + pad))
        offset = int(data["o"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SearchError("invalid page_token") from exc
    if data.get("q") != digest or offset < 0:
        raise SearchError("page_token does not belong to this query (repeat the same parameters)")
    return offset


def _elements(res: dict, elements: list[str] | None) -> dict:
    if not elements:
        return res
    keep = {"resourceType", "id", "meta", *elements}
    out = {k: v for k, v in res.items() if k in keep}
    meta = dict(out.get("meta") or {})
    meta["tag"] = [*meta.get("tag", []),
                   {"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationValue",
                    "code": "SUBSETTED"}]  # fmt: skip
    out["meta"] = meta
    return out


def _page(
    matches: list[dict],
    *,
    index: PatientIndex,
    offset: int,
    count: int,
    revinclude: bool,
    elements: list[str] | None,
    self_url: str,
    digest: str,
) -> dict:
    entries: list[dict] = []
    used, taken = 0, 0
    for res in matches[offset : offset + count]:
        group = [{"fullUrl": f"urn:uuid:{res['id']}", "resource": _elements(res, elements),
                  "search": {"mode": "match"}}]  # fmt: skip
        if revinclude:
            key = f"{res['resourceType']}/{res['id']}"
            for p in index.provenance_by_target.get(key, []):
                group.append({"fullUrl": f"urn:uuid:{p['id']}", "resource": p,
                              "search": {"mode": "include"}})  # fmt: skip
        size = len(json.dumps(group, ensure_ascii=False, separators=(",", ":")))
        if taken and used + size > PAGE_BYTE_BUDGET:
            break
        entries.extend(group)
        used += size
        taken += 1
    links = [{"relation": "self", "url": self_url}]
    nxt = offset + taken
    if nxt < len(matches):
        sep = "&" if "?" in self_url else "?"
        links.append(
            {"relation": "next", "url": f"{self_url}{sep}page_token={_encode_token(nxt, digest)}"}
        )
    bundle = {
        "resourceType": "Bundle",
        "id": str(uuid.uuid4()),
        "meta": {"lastUpdated": _now()},
        "type": "searchset",
        "total": len(matches),
        "link": links,
    }
    if entries:  # FHIR forbids an empty array
        bundle["entry"] = entries
    return bundle


def _clamp(count: int | None) -> int:
    try:
        n = int(count if count is not None else DEFAULT_COUNT)
    except (TypeError, ValueError) as exc:
        raise SearchError("_count must be an integer") from exc
    return max(1, min(n, MAX_COUNT))


def _query_string(params: dict | None) -> str:
    parts = []
    for k, v in sorted((params or {}).items()):
        for item in v if isinstance(v, list) else [v]:
            parts.append(f"{k}={item}")
    return "&".join(parts)


# ── operations ───────────────────────────────────────────────────────────────


def search(
    index: PatientIndex,
    rtype: str,
    params: dict[str, Any] | None = None,
    count: int | None = None,
    page_token: str | None = None,
) -> dict:
    if rtype not in SEARCH_PARAMS:
        hint = " (planned, not projected yet)" if rtype in NOT_YET else ""
        raise SearchError(
            f"resource type {rtype!r} is not supported{hint}. Supported: {list(SEARCH_TYPES)}",
            code="not-supported",
        )
    params = dict(params or {})
    filters, control = _parse_params(rtype, params)
    count = _clamp(control.get("_count", count))
    page_token = control.get("page_token") or page_token
    rev = control.get("_revinclude")
    if rev not in (None, "", REVINCLUDE):
        raise SearchError(f"only _revinclude={REVINCLUDE} is supported", "not-supported")
    elements = control.get("_elements")
    if isinstance(elements, str):
        elements = [e.strip() for e in elements.split(",") if e.strip()]
    query = {k: v for k, v in params.items() if k not in ("page_token", "_count")}
    digest = _query_digest(index.data.patient_id, rtype, query)
    offset = _decode_token(page_token, digest)
    matches = sorted((r for r in index.of_type(rtype) if _matches(r, filters)), key=_sort_key)
    qs = _query_string({**query, "_count": count})
    return _page(
        matches,
        index=index,
        offset=offset,
        count=count,
        revinclude=bool(rev),
        elements=elements,
        self_url=f"{rtype}?{qs}",
        digest=digest,
    )


def read(index: PatientIndex, rtype: str, rid: str) -> dict:
    if rtype not in SEARCH_PARAMS:
        raise SearchError(f"resource type {rtype!r} is not supported", "not-supported")
    res = index.get(rtype, rid)
    if res is None:
        raise SearchError(f"{rtype}/{rid} not found", code="not-found")
    return res


def everything(
    index: PatientIndex,
    start: str | None = None,
    end: str | None = None,
    types: list[str] | str | None = None,
    count: int | None = None,
    page_token: str | None = None,
) -> dict:
    """``Patient/$everything``: the Patient first, then every resource, newest first."""
    if isinstance(types, str):
        types = [x.strip() for x in types.split(",") if x.strip()]
    for x in types or []:
        if x not in SEARCH_PARAMS:
            raise SearchError(f"_type {x!r} is not supported", "not-supported")
    for label, value in (("start", start), ("end", end)):
        if value and not t.fhir_datetime(value):
            raise SearchError(f"invalid {label} date {value!r}")
    lo = _pad(start, low=True) if start else None
    hi = _pad(end, low=False) if end else None

    def keep(res: dict) -> bool:
        if types and res["resourceType"] not in types:
            return False
        iv = _interval(res)
        if iv is None:
            return True  # undated resources (Patient, Organization, ...) always included
        return not ((lo and iv[1] < lo) or (hi and iv[0] > hi))

    rest = sorted((r for r in index.resources[1:] if keep(r)), key=_sort_key)
    matches = ([index.patient] if keep(index.patient) else []) + rest
    count = _clamp(count)
    query = {"start": start, "end": end, "_type": types}
    digest = _query_digest(index.data.patient_id, "$everything", query)
    offset = _decode_token(page_token, digest)
    qs = _query_string({k: (",".join(v) if isinstance(v, list) else v)
                        for k, v in {**query, "_count": count}.items() if v})  # fmt: skip
    return _page(
        matches,
        index=index,
        offset=offset,
        count=count,
        revinclude=False,
        elements=None,
        self_url=f"Patient/{index.patient['id']}/$everything" + (f"?{qs}" if qs else ""),
        digest=digest,
    )


# ── IPS-shaped patient summary ───────────────────────────────────────────────

IPS_SECTIONS = (
    ("problems", "11450-4", "Problem list - Reported", "Problems"),
    ("medications", "10160-0", "History of Medication use Narrative", "Medication Summary"),
    ("allergies", "48765-2", "Allergies and adverse reactions Document",
     "Allergies and Intolerances"),
    ("results", "30954-2", "Relevant diagnostic tests/laboratory data Narrative", "Results"),
)  # fmt: skip


def _verification(res: dict) -> str | None:
    for tag in res.get("meta", {}).get("tag", []):
        if tag.get("system") == t.VERIFICATION_SYSTEM:
            return tag.get("code")
    return None


def _label(res: dict) -> str:
    for key in ("code", "medicationCodeableConcept"):
        cc = res.get(key)
        if isinstance(cc, dict):
            text = cc.get("text") or next(
                (c.get("display") or c.get("code") for c in cc.get("coding", [])), ""
            )
            break
    else:
        text = res.get("title") or res["resourceType"]
    when = None
    iv = _interval(res)
    if iv:
        when = iv[0]
    value = ""
    q = res.get("valueQuantity")
    if q:
        value = f" {q.get('comparator', '')}{q.get('value')} {q.get('unit', '')}".rstrip()
    elif res.get("valueString"):
        value = f" {res['valueString']}"
    v = _verification(res)
    tail = f" [{v}]" if v and v not in ("verified", "corrected") else ""
    return f"{when + ': ' if when else ''}{text}{value}{tail}"


def _narrative(items: list[dict], empty: str) -> dict:
    if items:
        body = "<ul>" + "".join(f"<li>{html.escape(_label(r))}</li>" for r in items) + "</ul>"
    else:
        body = f"<p>{html.escape(empty)}</p>"
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml">{body}</div>'}


def _refs_in(obj: Any) -> list[str]:
    out = []
    if isinstance(obj, dict):
        ref = obj.get("reference")
        if isinstance(ref, str):
            out.append(ref)
        for v in obj.values():
            out.extend(_refs_in(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_refs_in(v))
    return out


def _rewrite_refs(obj: Any, present: set[str]) -> Any:
    """Relative ``Type/id`` → ``urn:uuid:id`` inside a document; drop references to
    resources left out of it (e.g. an excluded unverified fact)."""
    if isinstance(obj, dict):
        ref = obj.get("reference")
        if isinstance(ref, str) and "/" in ref and not ref.startswith("urn:"):
            parts = ref.split("/")
            if len(parts) == 2:
                if ref not in present:
                    return None
                obj = {**obj, "reference": f"urn:uuid:{parts[1]}"}
        out = {}
        for k, v in obj.items():
            nv = _rewrite_refs(v, present)
            if nv not in (None, [], {}):
                out[k] = nv
        return out
    if isinstance(obj, list):
        out = [_rewrite_refs(v, present) for v in obj]
        return [v for v in out if v not in (None, [], {})]
    return obj


def _projection_device() -> dict:
    try:
        from importlib.metadata import version

        ver = version("oncofiles")
    except Exception:
        ver = None
    dev = {
        "resourceType": "Device",
        "id": derived_id("device", "oncofiles-fhir-projection"),
        "deviceName": [{"name": "Oncofiles FHIR projection", "type": "model-name"}],
        "type": {"text": "software"},
    }
    if ver:
        dev["version"] = [{"value": ver}]
    return dev


def patient_summary(index: PatientIndex, *, include_unverified: bool = False) -> dict:
    """IPS-shaped document Bundle (Composition + Patient + section entries + their
    references and Provenance). Unverified facts are excluded unless asked."""

    def ok(res: dict) -> bool:
        return include_unverified or _verification(res) != "unverified"

    problems = [r for r in index.of_type("Condition") if ok(r)]
    med_types = ("MedicationRequest", "MedicationStatement")
    meds = [r for r in index.resources if r["resourceType"] in med_types and ok(r)]
    latest: dict[str, dict] = {}
    for r in sorted(
        (o for o in index.of_type("Observation") if ok(o)), key=_sort_key
    ):  # newest first
        cat = {c.get("code") for cc in r.get("category", []) for c in cc.get("coding", [])}
        if "laboratory" not in cat:
            continue
        key = json.dumps(r.get("code", {}).get("coding") or r.get("code", {}).get("text"))
        latest.setdefault(key, r)
    results = list(latest.values())
    sections = {"problems": problems, "medications": meds, "allergies": [], "results": results}

    device = _projection_device()
    included: dict[str, dict] = {f"Patient/{index.patient['id']}": index.patient}
    included[f"Device/{device['id']}"] = device
    for items in sections.values():
        for r in items:
            included[f"{r['resourceType']}/{r['id']}"] = r
    for key in [k for k in included if not k.startswith(("Patient/", "Device/"))]:
        for p in index.provenance_by_target.get(key, []):
            included[f"Provenance/{p['id']}"] = p
    # closure: referenced documents, organisations, practitioners, devices, plans ...
    queue = list(included.values())
    while queue:
        res = queue.pop()
        for ref in _refs_in(res):
            if ref in included or ref.count("/") != 1:
                continue
            target = index.by_key.get(ref)
            if target is None or not ok(target):
                continue
            included[ref] = target
            queue.append(target)

    comp_id = str(uuid.uuid4())
    now = _now()
    composition = {
        "resourceType": "Composition",
        "id": comp_id,
        "status": "final",
        "type": {
            "coding": [
                {"system": t.LOINC, "code": "60591-5", "display": "Patient summary Document"}
            ]
        },  # fmt: skip
        "subject": {"reference": f"Patient/{index.patient['id']}"},
        "date": now,
        "author": [{"reference": f"Device/{device['id']}"}],
        "title": "Patient Summary",
        "section": [],
    }
    for key, code, display, title in IPS_SECTIONS:
        items = sections[key]
        sec = {
            "title": title,
            "code": {"coding": [{"system": t.LOINC, "code": code, "display": display}]},
            "text": _narrative(items, f"No {title.lower()} information available."),
        }
        if items:
            sec["entry"] = [{"reference": f"{r['resourceType']}/{r['id']}"} for r in items]
        else:
            sec["emptyReason"] = {
                "coding": [{"system": t.LIST_EMPTY_REASON, "code": "unavailable",
                            "display": "Unavailable"}]
            }  # fmt: skip
        composition["section"].append(sec)

    present = set(included) | {f"Composition/{comp_id}"}
    ordered = [composition, index.patient] + [
        r for k, r in included.items() if not k.startswith("Patient/")
    ]
    entries = [
        {"fullUrl": f"urn:uuid:{r['id']}", "resource": _rewrite_refs(r, present)} for r in ordered
    ]
    return {
        "resourceType": "Bundle",
        "id": str(uuid.uuid4()),
        "meta": {"lastUpdated": now},
        "identifier": {"system": "urn:ietf:rfc:3986", "value": f"urn:uuid:{uuid.uuid4()}"},
        "type": "document",
        "timestamp": now,
        "entry": entries,
    }


# ── CapabilityStatement ──────────────────────────────────────────────────────


def capability_statement() -> dict:
    resources = []
    for rtype, params in SEARCH_PARAMS.items():
        sp = [{"name": n, "type": k} for n, k in {**_COMMON, **params}.items()]
        entry = {
            "type": rtype,
            "interaction": [{"code": "read"}, {"code": "search-type"}],
            "searchParam": sp,
        }
        if rtype not in ("Provenance", "Patient", "Device"):
            entry["searchRevInclude"] = [REVINCLUDE]
        resources.append(entry)
    systems = [t.LOINC, t.UCUM, t.SNOMED, t.ICD10_WHO, t.ICD_O_3, t.ATC, t.MKCH10, t.NCZI_LAB,
               t.VERIFICATION_SYSTEM, t.FACT_CODE_SYSTEM, t.DOC_CATEGORY_SYSTEM]  # fmt: skip
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": _now()[:10],
        "publisher": "Oncofiles",
        "kind": "instance",
        "software": {"name": "Oncofiles FHIR projection (read-only)"},
        "implementation": {
            "description": "Read-only FHIR R4 projection over MCP tools fhir_search, "
            "fhir_read, patient_everything, export_patient_summary. One patient per call."
        },
        "fhirVersion": FHIR_VERSION,
        "format": ["json"],
        "rest": [
            {
                "mode": "server",
                "documentation": (
                    "Code systems used: " + ", ".join(systems) + ". Never ICD-10-CM or RxNorm. "
                    "Verification (our QA, not a clinical status) is meta.tag in "
                    f"{t.VERIFICATION_SYSTEM} (machine | unverified | verified | corrected); "
                    "machine/LLM-derived resources carry meta.security AIAST. Search: tokens "
                    "'system|code', '|code', 'code' or ':text'; dates with prefixes "
                    "eq/ne/gt/lt/ge/le at day precision; comma = OR, a list of values = AND. "
                    f"Paging: _count (max {MAX_COUNT}) and a ~{PAGE_BYTE_BUDGET // 1000} kB "
                    "budget per page; continue with page_token from the 'next' link. "
                    f"Not projected yet: {', '.join(NOT_YET)}."
                ),
                "resource": resources,
                "operation": [
                    {
                        "name": "everything",
                        "definition": "http://hl7.org/fhir/OperationDefinition/Patient-everything",
                    },
                    {
                        "name": "summary",
                        "definition": "http://hl7.org/fhir/uv/ips/OperationDefinition/summary",
                    },
                ],  # fmt: skip
            }
        ],
    }
