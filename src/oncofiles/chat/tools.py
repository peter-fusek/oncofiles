"""The chat core's read-only tools (#1020): allowlist, argument checks, data wrapping.

``CHAT_TOOLS`` is a fixed allowlist. Every name in it is BOTH a user-surface
tool (``tool_surface.USER_TOOLS``) AND registered read-only
(``readOnlyHint=true``); ``tests/test_1020_chat_core.py`` verifies that against
the live registry and that no write tool, no operator tool and no ``query_db``
can ever be added. The tool functions are called in-process with
``patient_slug=None``: the patient is the one ``chat.scope.chat_scope`` fixed,
and the resolver refuses any other one.

Tool output reaches the model only through ``ToolResult.text``: compacted
(links and notes dropped), raw genotype content excluded by the
``curated.llm_input`` rules (when in doubt, excluded) and cut to
``RESULT_MAX_BYTES``. Citations come only from these results
(``citation_candidates``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from oncofiles.curated.llm_input import extraction_exclusion
from oncofiles.fact_labels import verification_label

logger = logging.getLogger(__name__)

RESULT_MAX_BYTES = 4_000  # one tool result as the model sees it
MAX_CODES = 8
MAX_LIST_LIMIT = 20
EXCLUDED = "raw_genotype_excluded"
_RSID = re.compile(r"\brs\d{3,}\b", re.I)
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class _Ctx:
    """The minimal FastMCP-like context the tool functions read the DB from."""

    class _Req:
        def __init__(self, db) -> None:
            self.lifespan_context = {"db": db}

    def __init__(self, db) -> None:
        self.request_context = self._Req(db)


def _date(v) -> bool:
    return isinstance(v, str) and bool(_ISO_DAY.fullmatch(v))


@dataclass(frozen=True)
class Param:
    kind: str  # str | int | bool | list | date
    required: bool = False
    maximum: int | None = None


@dataclass(frozen=True)
class Spec:
    description: str
    params: dict[str, Param] = field(default_factory=dict)


SPECS: dict[str, Spec] = {
    "patient_overview": Spec(
        "Overview: diagnoses and markers, latest key values, recent visits, upcoming planned "
        "dates, documents by type. Start here for a general question."
    ),
    "fact_catalog": Spec("Every measured lab code of the patient with counts and first/last date."),
    "fact_series": Spec(
        "Values over time of lab codes (e.g. HGB, PLT, WBC, CEA) with source document, page, "
        "unit, reference range and verification; newest value in latest_any.",
        {
            "codes": Param("list", required=True),
            "date_from": Param("date"),
            "date_to": Param("date"),
            "include_unverified": Param("bool"),
        },
    ),
    "timeline": Spec(
        "What happened, newest first. kinds filters by: encounter, document, treatment, "
        "condition, procedure, medication, imaging, planned (next cycle, examination or "
        "visit), lab, tumor_marker.",
        {
            "date_from": Param("date"),
            "date_to": Param("date"),
            "kinds": Param("list"),
            "limit": Param("int", maximum=MAX_LIST_LIMIT),
        },
    ),
    "search_documents": Spec(
        "Search the patient's documents by text, institution, category (labs, report, imaging, "
        "pathology, genetics, surgery, discharge, prescription, referral, other) and dates.",
        {
            "text": Param("str"),
            "institution": Param("str"),
            "category": Param("str"),
            "date_from": Param("date"),
            "date_to": Param("date"),
            "limit": Param("int", maximum=MAX_LIST_LIMIT),
        },
    ),
    "document_detail": Spec(
        "One document: metadata, institution, doctor and the facts extracted from it.",
        {"document_id": Param("int", required=True)},
    ),
    "encounter_detail": Spec(
        "One visit (or lab draw, procedure, chemo day): its documents and doctor.",
        {"encounter_id": Param("int", required=True)},
    ),
    "people_and_places": Spec("Doctors and institutions in this patient's records."),
}

CHAT_TOOLS: frozenset[str] = frozenset(SPECS)


def _functions() -> dict[str, Callable[..., Awaitable[str]]]:
    from oncofiles.tools import clinical_facts as cf
    from oncofiles.tools import documents

    return {
        "patient_overview": cf.patient_overview,
        "fact_catalog": cf.fact_catalog,
        "fact_series": cf.fact_series,
        "timeline": cf.timeline,
        "search_documents": documents.search_documents,
        "document_detail": cf.document_detail,
        "encounter_detail": cf.encounter_detail,
        "people_and_places": cf.people_and_places,
    }


def tools_text() -> str:
    lines = []
    for name, spec in SPECS.items():
        args = ", ".join(
            f"{k}: {p.kind}{' (required)' if p.required else ''}" for k, p in spec.params.items()
        )
        lines.append(f"- {name}({args}): {spec.description}")
    return "\n".join(lines)


def clean_args(name: str, args: Any) -> tuple[dict, list[str]]:
    """(the arguments a call may pass, error codes). Unknown keys are dropped,
    ``patient_slug`` above all: the patient is never an argument."""
    spec = SPECS.get(name)
    if spec is None:
        return {}, ["unknown_tool"]
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {}, ["args_not_object"]
    out: dict[str, Any] = {}
    errors: list[str] = []
    for key, param in spec.params.items():
        value = args.get(key)
        if value is None:
            if param.required:
                errors.append(f"missing_{key}")
            continue
        if param.kind == "str":
            ok = isinstance(value, str) and 0 < len(value) <= 200
        elif param.kind == "date":
            ok = _date(value)
        elif param.kind == "bool":
            ok = isinstance(value, bool)
        elif param.kind == "int":
            ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
            if ok and param.maximum is not None:
                value = min(value, param.maximum)
        else:  # list of short strings
            ok = (
                isinstance(value, list)
                and 0 < len(value) <= MAX_CODES
                and all(isinstance(v, str) and 0 < len(v) <= 40 for v in value)
            )
        if ok:
            out[key] = value
        else:
            errors.append(f"bad_{key}")
    if name in ("search_documents", "timeline") and "limit" not in out:
        out["limit"] = 10 if name == "search_documents" else MAX_LIST_LIMIT
    return out, errors


# ── output: compact, exclude, cut ──

_DROP_KEYS = frozenset({
    "note", "llm_note", "gdrive_url", "source_url", "url", "file_id", "duplicates",
    "local_codes", "links", "extracted_by", "ai_tags", "flag_source",
})  # fmt: skip


def _doc_excluded(row: dict) -> bool:
    meta = row.get("structured_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = "unreadable"
    probe = {
        "filename": row.get("filename"),
        "mime_type": row.get("mime_type"),
        "category": row.get("category"),
        "structured_metadata": meta,
    }
    return extraction_exclusion(probe) is not None


def _compact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _DROP_KEYS or v is None or v == [] or v == {}:
                continue
            if k == "structured_metadata":
                summary = _summary(v)
                if summary:
                    out["summary"] = summary
                continue
            out[k] = _compact(v)
        return out
    if isinstance(obj, list):
        return [_compact(v) for v in obj]
    return obj


def _summary(meta: Any) -> str | None:
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return None
    if not isinstance(meta, dict):
        return None
    cur = meta.get("curated") if isinstance(meta.get("curated"), dict) else {}
    text = cur.get("summary") or meta.get("summary")
    return str(text)[:400] if text else None


def _exclude_documents(name: str, data: dict) -> dict:
    """Search hits that are raw genotype exports keep only id, date and category."""
    if name != "search_documents" or not isinstance(data.get("documents"), list):
        return data
    docs = []
    for row in data["documents"]:
        if isinstance(row, dict) and _doc_excluded(row):
            docs.append({"id": row.get("id"), "document_date": row.get("document_date"),
                         "category": row.get("category"), "excluded": EXCLUDED})  # fmt: skip
        else:
            docs.append(row)
    return {**data, "documents": docs}


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _longest_list(obj: Any, best: list | None = None) -> list | None:
    if isinstance(obj, list):
        if len(obj) > 2 and (best is None or len(obj) > len(best)):
            best = obj
        for v in obj:
            best = _longest_list(v, best)
    elif isinstance(obj, dict):
        for v in obj.values():
            best = _longest_list(v, best)
    return best


def fit(obj: Any, budget: int = RESULT_MAX_BYTES) -> str:
    """Compact JSON within ``budget`` bytes: the longest list loses its middle
    (oldest and newest items stay, a marker says how many were left out), and
    as a last resort the text is cut."""
    for _ in range(64):
        text = _dumps(obj)
        if len(text.encode()) <= budget:
            return text
        lst = _longest_list(obj)
        if lst is None:
            break
        keep = max(1, (len(lst) - 1) // 4)
        omitted = len(lst) - 2 * keep
        lst[:] = [*lst[:keep], f"…({omitted} items omitted)", *lst[-keep:]]
    raw = _dumps(obj).encode()[: budget - 20]
    return raw.decode("utf-8", "ignore") + "…(truncated)"


# ── execution ──


@dataclass
class ToolResult:
    name: str
    args: dict
    ok: bool
    data: Any
    text: str
    error: str | None = None


def _error(name: str, args: dict, code: str) -> ToolResult:
    data = {"error": code}
    return ToolResult(name, args, False, data, _dumps(data), code)


async def run_tool(db, name: str, args: Any) -> ToolResult:
    """Run one allowlisted read tool for the scoped patient. Never raises."""
    clean, errors = clean_args(name, args)
    if errors:
        return _error(name if name in SPECS else "unknown", clean, errors[0])
    fn = _functions()[name]
    ctx = _Ctx(db)
    try:
        if name == "document_detail":
            from oncofiles.chat.scope import chat_patient_id

            pid = chat_patient_id()
            row = await db.document_row(patient_id=pid, document_id=clean["document_id"])
            if row is None:
                return _error(name, clean, "not_found")
            if _doc_excluded(row):
                return _error(name, clean, EXCLUDED)
        raw = await fn(ctx, **clean, patient_slug=None)
        data = json.loads(raw)
    except ValueError:  # the resolver's not-accessible text, a bad value
        return _error(name, clean, "not_found")
    except Exception as exc:  # noqa: BLE001 - one tool failing is data, not a crash
        logger.warning("chat: tool %s failed (%s)", name, type(exc).__name__)
        return _error(name, clean, "tool_failed")
    if isinstance(data, dict) and data.get("error"):
        code = "not_found" if "not" in str(data.get("error")).lower() else "tool_error"
        return _error(name, clean, code)
    data = _compact(_exclude_documents(name, data))
    if len(_RSID.findall(_dumps(data))) >= 3:  # genotype markers in any field: excluded
        return _error(name, clean, EXCLUDED)
    return ToolResult(name, clean, True, data, fit(data))


# ── citations (only from tool results) ──


@dataclass
class Candidate:
    document_id: int
    page: int | None
    label: str | None
    date: str | None
    verification: str | None
    verification_label: dict | None
    evidence: str | None
    values: list[str]


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _candidate(d: dict, doc_id: int | None) -> Candidate | None:
    src = d.get("source") if isinstance(d.get("source"), dict) else {}
    did = _first(d, "source_document_id") or src.get("document_id") or doc_id
    if not isinstance(did, int) or isinstance(did, bool):
        return None
    verification = d.get("verification")
    label = _first(d, "label", "code", "name", "title", "name_en", "category")
    date = _first(d, "at", "effective_at", "document_date") or src.get("date")
    vlabel = d.get("verification_label")
    if not isinstance(vlabel, dict):
        vlabel = verification_label(verification, None)
    values = [str(v) for v in (d.get("value"), date) if v is not None and not isinstance(v, dict)]
    evidence = d.get("evidence")
    return Candidate(
        document_id=did,
        page=_first(d, "page", "page_number") or src.get("page"),
        label=str(label)[:80] if label is not None else None,
        date=str(date)[:25] if date is not None else None,
        verification=verification,
        verification_label=vlabel,
        evidence=str(evidence)[:160] if evidence else None,
        values=values,
    )


def citation_candidates(results: list[ToolResult]) -> list[Candidate]:
    """Every row of this turn's tool results that names a source document."""
    out: list[Candidate] = []

    def walk(obj: Any, doc_id: int | None) -> None:
        if isinstance(obj, list):
            for v in obj:
                walk(v, doc_id)
            return
        if not isinstance(obj, dict):
            return
        inner = doc_id
        if isinstance(obj.get("document"), dict) and isinstance(obj["document"].get("id"), int):
            inner = obj["document"]["id"]
            cand = _candidate(obj["document"], inner)
            if cand:
                out.append(cand)
        elif "filename" in obj and isinstance(obj.get("id"), int):  # a document row
            cand = _candidate(obj, obj["id"])
            if cand:
                out.append(cand)
        elif any(k in obj for k in ("value", "verification", "at")):
            cand = _candidate(obj, inner)
            if cand:
                out.append(cand)
        for k, v in obj.items():
            if k != "document":
                walk(v, inner)

    for r in results:
        if r.ok:
            walk(r.data, None)
    return out
