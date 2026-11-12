"""Schema lint: the data-model conventions of #789.

Rules: ``docs/architecture/data_model_conventions.md``. The schema is built from
``migrations/`` exactly as the app builds it (``Database.migrate``) and checked:

1. names are snake_case; every table has an ``id INTEGER`` primary key;
2. every ``patient_id`` is NOT NULL, has no DEFAULT and an FK to
   ``patients(patient_id)``;
3. every ``*_id`` column is an FK (to ``<table>(id)``, or to
   ``patients(patient_id)``), half of a documented polymorphic pair, or an
   external id by name;
4. every enum-like column is classified in ``oncofiles.vocab`` (domain,
   reference code or free text by design), and no table keeps a text copy of
   an entity it already references by FK;
5. instants end in ``_at`` (TEXT, ISO-8601 default), dates in ``_date``;
   ``last_updated`` / ``version_id`` are projection names, not columns;
6. JSON columns end in ``_json``;
7. every table with ``verification`` carries the full provenance set.

A new table or column that breaks a rule fails here. Legacy violations sit in
allowlists that MAY ONLY SHRINK: ``test_legacy_allowlists_only_shrink`` fails
when an allowlisted item no longer violates its rule (fixed or dropped), so the
entry must go. ``test_doc_lists_every_legacy_exception`` keeps the conventions
doc in step with the allowlists.
"""

from __future__ import annotations

import asyncio
import functools
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from oncofiles import vocab
from oncofiles.database import Database

DOC = Path(__file__).resolve().parent.parent / "docs/architecture/data_model_conventions.md"


# ── schema model ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    notnull: bool
    default: str | None
    pk: int


@dataclass
class Table:
    name: str
    columns: dict[str, Column]
    fks: dict[str, tuple[str, str]] = field(default_factory=dict)  # column -> (table, column)

    @property
    def pk(self) -> list[Column]:
        return sorted((c for c in self.columns.values() if c.pk), key=lambda c: c.pk)


# The migration runner's own bookkeeping (created by Database.migrate, not by a
# migration) and SQLite's internals are not part of the data model.
INFRA_TABLES = {"schema_migrations"}


async def _load() -> dict[str, Table]:
    db = Database(":memory:")
    await db.connect()
    try:
        await db.migrate()
        async with db.db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ) as cur:
            rows = [(r[0], r[1] or "") for r in await cur.fetchall()]
        virtual = [n for n, sql in rows if sql.upper().startswith("CREATE VIRTUAL TABLE")]
        out: dict[str, Table] = {}
        for name, _ in rows:
            if (
                name.startswith("sqlite_")
                or name in INFRA_TABLES
                or any(name == v or name.startswith(f"{v}_") for v in virtual)  # FTS + shadows
            ):
                continue
            async with db.db.execute(f"PRAGMA table_info('{name}')") as cur:
                cols = {
                    r["name"]: Column(
                        r["name"], (r["type"] or "").upper(), bool(r["notnull"]),
                        r["dflt_value"], r["pk"],
                    )
                    for r in await cur.fetchall()
                }  # fmt: skip
            async with db.db.execute(f"PRAGMA foreign_key_list('{name}')") as cur:
                fks = {r["from"]: (r["table"], r["to"]) for r in await cur.fetchall()}
            out[name] = Table(name, cols, fks)
        return out
    finally:
        await db.close()


@functools.cache
def schema() -> dict[str, Table]:
    return asyncio.run(_load())


def _cols(s: dict[str, Table]):
    for t in s.values():
        for c in t.columns.values():
            yield t, c


# ── rule 1: names, primary keys ──────────────────────────────────────────

SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")

# table -> reason. May only shrink.
LEGACY_PK: dict[str, str] = {
    "patients": "PERMANENT: patient_id (TEXT UUID) is the tenant key every table references",
    "fact_codes": "the code itself is the PK (every clinical_records.code points at it)",
    "curated_settings": "one row per patient, PK patient_id (and no FK)",
    "patient_selection": "one row per caregiver, PK owner_email",
    "mcp_oauth_clients": "OAuth client registry, PK client_id as presented by the client",
    "mcp_oauth_tokens": "auth table, tracked separately",
    "newsletter_subscribers": "PK id is a TEXT UUID",
    "session_revocations": "PK is the session token's tid",
}


def pk_violations(s: dict[str, Table]) -> dict[str, str]:
    out = {}
    for t in s.values():
        pk = t.pk
        if [(c.name, c.type) for c in pk] != [("id", "INTEGER")]:
            out[t.name] = ", ".join(f"{c.name} {c.type}" for c in pk) or "no primary key"
    return out


# ── rule 2: patient_id ───────────────────────────────────────────────────

PATIENTS_FK = ("patients", "patient_id")

# table -> reason. May only shrink. Planned fix (b): table rebuilds in a
# maintenance window with a snapshot (SQLite cannot ADD an FK to a column).
LEGACY_PATIENT_FK: dict[str, str] = {
    "documents": "Drive era (migration 001), patient_id added later",
    "document_references": "vaccination shortcuts (#457)",
    "conversation_entries": "session diary, patient_id added later",
    "treatment_events": "deprecated treatment log (#711 replacements have the FK)",
    "curated_runs": "curated run log (#573)",
    "curated_settings": "nightly switch per patient (#573)",
    "fact_rereads": "re-read cache (#679)",
    "prompt_log": "AI call telemetry; also holds a non-UUID system sentinel",
    "activity_log": "tool-call audit log",
    "agent_state": "Oncoteam key/value state (sunset)",
    "research_entries": "Oncoteam research library (sunset)",
    "calendar_entries": "Google Calendar mirror",
    "email_entries": "Gmail mirror",
    "oauth_tokens": "Google OAuth tokens per patient",
    "onboarding_events": "onboarding funnel events (#468)",
    "patient_context": "patient context JSON",
    "sync_history": "sync run log; '' for system-wide rows",
}

# table -> reason. May only shrink (these two are by design).
LEGACY_PATIENT_NULLABLE: dict[str, str] = {
    "codings": "BY DESIGN: NULL when the owner is shared reference data (organisation, device)",
    "identifiers": "BY DESIGN: NULL when the owner is a shared organisation or department",
    "curated_settings": "TEXT PRIMARY KEY without NOT NULL (SQLite then allows NULL)",
}

# table -> reason. May only shrink. A DEFAULT turns a forgotten patient_id into
# a real-looking value instead of an error.
LEGACY_PATIENT_DEFAULT: dict[str, str] = {
    "activity_log": "DEFAULT 'erika' (single-patient era)",
    "conversation_entries": "DEFAULT 'erika' (single-patient era)",
    "documents": "DEFAULT 'erika' (single-patient era)",
    "prompt_log": "DEFAULT 'erika' (single-patient era)",
    "research_entries": "DEFAULT 'erika' (single-patient era)",
    "treatment_events": "DEFAULT 'erika' (single-patient era)",
    "calendar_entries": "DEFAULT 'default'",
    "email_entries": "DEFAULT 'default'",
    "oauth_tokens": "DEFAULT 'default'",
    "agent_state": "DEFAULT ''",
    "patient_context": "DEFAULT ''",
    "sync_history": "DEFAULT '' (system-wide rows)",
}


def _patient_tables(s: dict[str, Table]):
    return [t for t in s.values() if "patient_id" in t.columns and t.name != "patients"]


def patient_fk_violations(s: dict[str, Table]) -> dict[str, str]:
    return {
        t.name: f"FK {t.fks.get('patient_id')}"
        for t in _patient_tables(s)
        if t.fks.get("patient_id") != PATIENTS_FK
    }


def patient_nullable_violations(s: dict[str, Table]) -> dict[str, str]:
    return {t.name: "nullable" for t in _patient_tables(s) if not t.columns["patient_id"].notnull}


def patient_default_violations(s: dict[str, Table]) -> dict[str, str]:
    return {
        t.name: f"DEFAULT {t.columns['patient_id'].default}"
        for t in _patient_tables(s)
        if t.columns["patient_id"].default is not None
    }


# ── rule 3: *_id columns ─────────────────────────────────────────────────

# column name -> what it identifies outside our tables (any table).
EXTERNAL_ID_NAMES: dict[str, str] = {
    "fhir_id": "public UUID of a row (rule 1 identity column)",
    "session_id": "MCP / chat session id sent by the client",
    "agent_id": "name of the calling agent",
    "run_id": "curated runner's run id, also in the Drive journal",
    "file_id": "Files API id or 'gdrive:<id>'",
    "gdrive_id": "Google Drive file id",
    "gdrive_folder_id": "Google Drive folder id",
    "gdrive_shortcut_id": "Google Drive shortcut id",
    "target_folder_id": "Google Drive folder id",
    "google_event_id": "Google Calendar event id",
    "gmail_message_id": "Gmail message id",
    "thread_id": "Gmail thread id",
    "external_id": "id in an external source (PubMed PMID, NCT number)",
}
# (table, id column) -> its type column. Codes of the type column are an
# allowlist in vocab (and in the DB helpers, which check ownership).
POLYMORPHIC: dict[tuple[str, str], str] = {
    ("codings", "owner_id"): "owner_type",
    ("identifiers", "owner_id"): "owner_type",
    ("links", "from_id"): "from_type",
    ("links", "to_id"): "to_type",
    ("provenance", "target_id"): "target_type",
}
# (table, column) -> reason. May only shrink. Planned fix (b): add the FK in the
# same table rebuild as the patient_id FK.
LEGACY_ID_WITHOUT_FK: dict[tuple[str, str], str] = {
    ("documents", "split_source_doc_id"): "points at documents.id without an FK",
    ("documents", "group_id"): "TEXT UUID shared by a document group; no groups table",
    ("fact_rereads", "document_id"): "points at documents.id without an FK",
    ("prompt_log", "document_id"): "points at documents.id without an FK",
    ("calendar_entries", "treatment_event_id"): "points at treatment_events.id without an FK",
    ("mcp_oauth_tokens", "client_id"): "points at mcp_oauth_clients.client_id without an FK",
}
PROJECTION_NAMES = {"last_updated", "version_id"}  # rule 5, checked there


def id_violations(s: dict[str, Table]) -> dict[tuple[str, str], str]:
    out = {}
    for t, c in _cols(s):
        if (
            not c.name.endswith("_id")
            or c.pk
            or c.name == "patient_id"
            or c.name in PROJECTION_NAMES
            or c.name in t.fks
            or (t.name, c.name) in POLYMORPHIC
            or c.name in EXTERNAL_ID_NAMES
        ):
            continue
        out[(t.name, c.name)] = f"{c.type} without FK"
    return out


def fk_target_violations(s: dict[str, Table]) -> dict[tuple[str, str], str]:
    return {
        (t.name, col): f"references {ref[0]}({ref[1]})"
        for t in s.values()
        for col, ref in t.fks.items()
        if ref != PATIENTS_FK and ref[1] != "id"
    }


# ── rule 4: enum-like columns, FK + text copy ────────────────────────────

ENUM_LIKE = re.compile(
    r"(^|_)(kind|type|status|state|category|source|role|route|intent|trigger|verification"
    r"|flag|relation|relationship|action|activity|setting|provider|tier|profile|participant"
    r"|engine|precision|comparator|laterality|equivalence|outcome|basis|lang|system|prefix"
    r"|mapped_by|produced_by|auth_method|certainty|code)$"
)


def enum_like(s: dict[str, Table]) -> set[tuple[str, str]]:
    return {(t.name, c.name) for t, c in _cols(s) if ENUM_LIKE.search(c.name) and "TEXT" in c.type}


def _classified() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for label, mapping in (
        ("domain", vocab.COLUMN_DOMAINS),
        ("reference", vocab.REFERENCE_CODE_COLUMNS),
        ("free_text", vocab.FREE_TEXT_BY_DESIGN),
    ):
        for key in mapping:
            out[key] = f"{out[key]}+{label}" if key in out else label
    return out


# referenced table -> text column names that would copy the entity
TEXT_COPY_SYNONYMS: dict[str, set[str]] = {
    "organizations": {"institution", "institution_code", "hospital"},
    "departments": {"dept", "ward"},
    "practitioners": {"doctor", "physician"},
    "documents": {"filename", "document_name"},
    "patients": {"patient_name", "patient_slug"},
}
# (table, text column) -> reason. May only shrink. Planned fix (a).
LEGACY_TEXT_COPY: dict[tuple[str, str], str] = {
    ("documents", "institution"): "organisation CODE next to organization_id; can drift",
}


def _singular(table: str) -> str:
    return table[:-1] if table.endswith("s") else table


def text_copy_violations(s: dict[str, Table]) -> dict[tuple[str, str], str]:
    out = {}
    for t in s.values():
        for fk_col, (ref_table, _) in t.fks.items():
            if ref_table == t.name:  # a self-reference (previous version, primary condition)
                continue
            stems = {fk_col.removesuffix("_id"), _singular(ref_table)}
            names = {f"{x}{sfx}" for x in stems for sfx in ("", "_code", "_name")}
            names |= TEXT_COPY_SYNONYMS.get(ref_table, set())
            for col in sorted(names & set(t.columns)):
                if "TEXT" in t.columns[col].type:
                    out[(t.name, col)] = f"text copy of {fk_col} -> {ref_table}"
    return out


# ── rule 5: time naming ──────────────────────────────────────────────────

ISO_DEFAULT = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"  # as _default() returns it
# a time word at the end of the name, or a bare participle ('updated'); a counter
# like sync_history.from_gdrive_updated is not a time
TIME_LIKE = re.compile(
    r"(^|_)(date|time|timestamp|start|end|expiry|expires|since|until)$"
    r"|^(created|updated|deleted|modified)$"
)
_TIME_DEFAULT = re.compile(r"strftime|datetime|date\(|current_timestamp", re.I)


def _default(c: Column) -> str | None:
    """The DEFAULT expression without outer parentheses or spaces."""
    d = (c.default or "").replace(" ", "")
    while d.startswith("(") and d.endswith(")") and d.count("(") == d.count(")"):
        inner = d[1:-1]
        if inner.count("(") != inner.count(")"):
            break
        d = inner
    return d or None


_HARD = (
    "clinical_records", "cancer_stages", "conditions", "devices", "medication_events",
    "procedures", "reports", "specimens", "treatment_courses", "treatment_cycles",
)  # fmt: skip
# (table, column) -> reason. May only shrink. Planned fix (c): views first, columns later.
LEGACY_TIME_NAME: dict[tuple[str, str], str] = {
    ("calendar_entries", "start_time"): "Google event start; rule: start_at",
    ("calendar_entries", "end_time"): "Google event end; rule: end_at",
    ("clinical_records", "effective_start"): "FHIR effectivePeriod; rule: effective_start_at",
    ("clinical_records", "effective_end"): "FHIR effectivePeriod; rule: effective_end_at",
    ("medication_events", "effective_start"): "FHIR effectivePeriod; rule: effective_start_at",
    ("medication_events", "effective_end"): "FHIR effectivePeriod; rule: effective_end_at",
    ("procedures", "performed_end"): "end of performedPeriod; rule: performed_end_at",
    ("documents", "gdrive_modified_time"): "Drive modifiedTime; rule: gdrive_modified_at",
    ("email_entries", "date"): "Gmail Date header; rule: sent_at",
    ("oauth_tokens", "token_expiry"): "Google token expiry; rule: token_expires_at",
}
# (table, column) -> reason. May only shrink. Planned fix (c).
LEGACY_INSTANT_FORMAT: dict[tuple[str, str], str] = {
    ("mcp_oauth_tokens", "expires_at"): "INTEGER epoch seconds (FastMCP token object)",
    ("session_revocations", "revoked_at"): "INTEGER epoch seconds",
    ("session_revocations", "expires_at"): "INTEGER epoch seconds",
    ("patient_selection", "updated_at"): "DEFAULT datetime('now'): space, no 'T'/'Z'",
}
# (table, column) -> reason. May only shrink. Planned fix (c): the FHIR names
# stay in the projection; the columns become updated_at / version.
LEGACY_PROJECTION_NAME: dict[tuple[str, str], str] = {
    **{(t, "last_updated"): "FHIR meta.lastUpdated as a column (rule: updated_at)" for t in _HARD},
    **{(t, "version_id"): "FHIR meta.versionId as a column (rule: version)" for t in _HARD},
}


def time_name_violations(s: dict[str, Table]) -> dict[tuple[str, str], str]:
    out = {}
    for t, c in _cols(s):
        name = c.name
        if name.endswith(("_at", "_date", "_by")) or name in PROJECTION_NAMES:
            continue
        if TIME_LIKE.search(name) or _TIME_DEFAULT.search(c.default or ""):
            out[(t.name, name)] = "time value not named *_at / *_date"
    return out


def instant_format_violations(s: dict[str, Table]) -> dict[tuple[str, str], str]:
    out = {}
    for t, c in _cols(s):
        if not c.name.endswith("_at"):
            continue
        if c.type != "TEXT":
            out[(t.name, c.name)] = f"type {c.type or 'none'}"
        elif c.default is not None and _default(c) != ISO_DEFAULT:
            out[(t.name, c.name)] = f"DEFAULT {c.default}"
    return out


def projection_name_violations(s: dict[str, Table]) -> dict[tuple[str, str], str]:
    return {(t.name, c.name): "projection name" for t, c in _cols(s) if c.name in PROJECTION_NAMES}


# ── rule 6: JSON naming ──────────────────────────────────────────────────

JSON_LIKE = re.compile(
    r"(^|_)(json|metadata|meta|tags|ids|aliases|conversions|scopes|labels|attendees"
    r"|recipients)$"
)
# (table, column) -> reason. May only shrink. Planned fix (c).
LEGACY_JSON_NAME: dict[tuple[str, str], str] = {
    ("documents", "structured_metadata"): "AI metadata object",
    ("documents", "ai_tags"): "AI tag array",
    ("email_entries", "structured_metadata"): "AI metadata object",
    ("email_entries", "recipients"): "address array",
    ("email_entries", "labels"): "Gmail label array",
    ("email_entries", "linked_document_ids"): "document id array (also 1NF, rule 8)",
    ("calendar_entries", "attendees"): "attendee array",
    ("conversation_entries", "tags"): "tag array",
    ("conversation_entries", "document_ids"): "document id array (also 1NF, rule 8)",
    ("treatment_events", "metadata"): "event metadata object",
    ("newsletter_subscribers", "metadata"): "signup metadata object",
    ("activity_log", "tags"): "tag array",
    ("research_entries", "tags"): "tag array",
    ("clinical_analyses", "tags"): "tag array",
    ("clinical_analyses", "record_ids"): "clinical_records id array (also 1NF, rule 8)",
    ("clinical_record_notes", "tags"): "tag array",
    ("fact_codes", "aliases"): "alias array",
    ("fact_codes", "unit_conversions"): "unit -> factor object",
    ("oauth_tokens", "granted_scopes"): "scope array",
    ("agent_state", "value"): "agent value object (DEFAULT '{}')",
}


def json_name_violations(s: dict[str, Table]) -> dict[tuple[str, str], str]:
    out = {}
    for t, c in _cols(s):
        if c.name.endswith("_json") or "TEXT" not in c.type:
            continue
        if JSON_LIKE.search(c.name) or _default(c) in ("'[]'", "'{}'"):
            out[(t.name, c.name)] = "JSON column not named *_json"
    return out


# ── rule 7: provenance on facts ──────────────────────────────────────────

PROVENANCE_COLUMNS = {"source_document_id", "page_number", "evidence", "extracted_by"}


def provenance_violations(s: dict[str, Table]) -> dict[str, str]:
    return {
        t.name: f"missing {sorted(PROVENANCE_COLUMNS - set(t.columns))}"
        for t in s.values()
        if "verification" in t.columns and not set(t.columns) >= PROVENANCE_COLUMNS
    }


# ── vocab: code format ───────────────────────────────────────────────────

CODE_FORMAT = re.compile(r"^[a-z][a-z0-9_]*$")
# domain -> reason. May only shrink. Domains with a ``standard`` keep its spelling.
LEGACY_CODE_FORMAT: dict[str, str] = {
    "curated_run_trigger": "'<trigger>-preview' codes are kebab-case",
    "device_kind": "'llm-extractor' is kebab-case",
    "diary_participant": "client names ('claude.ai', 'claude-code') as codes",
}


def code_format_violations() -> dict[str, str]:
    out = {}
    for d in vocab.registry().values():
        bad = sorted(c for c in d.code_set if not CODE_FORMAT.match(c))
        if bad and not d.standard:
            out[d.name] = ", ".join(bad)
    return out


# ── the checks ───────────────────────────────────────────────────────────

# rule -> (violations of the real schema, legacy allowlist)
LEGACY = {
    "pk": (lambda: pk_violations(schema()), LEGACY_PK),
    "patient_fk": (lambda: patient_fk_violations(schema()), LEGACY_PATIENT_FK),
    "patient_nullable": (lambda: patient_nullable_violations(schema()), LEGACY_PATIENT_NULLABLE),
    "patient_default": (lambda: patient_default_violations(schema()), LEGACY_PATIENT_DEFAULT),
    "id_without_fk": (lambda: id_violations(schema()), LEGACY_ID_WITHOUT_FK),
    "text_copy": (lambda: text_copy_violations(schema()), LEGACY_TEXT_COPY),
    "time_name": (lambda: time_name_violations(schema()), LEGACY_TIME_NAME),
    "instant_format": (lambda: instant_format_violations(schema()), LEGACY_INSTANT_FORMAT),
    "projection_name": (lambda: projection_name_violations(schema()), LEGACY_PROJECTION_NAME),
    "json_name": (lambda: json_name_violations(schema()), LEGACY_JSON_NAME),
    "code_format": (code_format_violations, LEGACY_CODE_FORMAT),
}


def test_schema_loaded():
    s = schema()
    assert len(s) >= 45, sorted(s)
    assert {"patients", "documents", "clinical_records", "provenance"} <= set(s)
    assert not any("_fts" in name for name in s), "FTS tables are excluded"
    assert ("documents", "category") in enum_like(s)


@pytest.mark.parametrize("rule", sorted(LEGACY))
def test_no_new_violations(rule):
    violations, allowlist = LEGACY[rule]
    found = violations()
    new = {k: v for k, v in found.items() if k not in allowlist}
    assert not new, (
        f"rule {rule!r}: new violations {new}. Follow docs/architecture/"
        "data_model_conventions.md; never add to the legacy allowlist for new schema."
    )


@pytest.mark.parametrize("rule", sorted(LEGACY))
def test_legacy_allowlists_only_shrink(rule):
    violations, allowlist = LEGACY[rule]
    stale = set(allowlist) - set(violations())
    assert not stale, (
        f"rule {rule!r}: {sorted(stale)} no longer violate it. Fixed? Remove them from the "
        "allowlist (and from the Legacy exceptions in data_model_conventions.md)."
    )


def test_names_are_snake_case():
    bad = [t.name for t in schema().values() if not SNAKE.match(t.name)]
    bad += [f"{t.name}.{c.name}" for t, c in _cols(schema()) if not SNAKE.match(c.name)]
    assert not bad


def test_fk_targets_are_ids_or_the_tenant_key():
    assert not fk_target_violations(schema())


def test_facts_carry_provenance():
    assert not provenance_violations(schema())


def test_external_id_names_are_in_use():
    used = {c.name for _, c in _cols(schema())}
    assert not set(EXTERNAL_ID_NAMES) - used


def test_polymorphic_pairs_are_typed_by_a_vocab_domain():
    s = schema()
    from oncofiles.database._facts import _LINK_TABLES
    from oncofiles.database._hard_facts import PATIENT_OWNER_TABLES, SHARED_OWNER_TABLES

    tables_of = {**PATIENT_OWNER_TABLES, **SHARED_OWNER_TABLES, "fact_code": "fact_codes"}
    tables_of |= {code: table for code, (table, _) in _LINK_TABLES.items()}
    for (table, id_col), type_col in POLYMORPHIC.items():
        assert {id_col, type_col} <= set(s[table].columns)
        d = vocab.domain_for(table, type_col)
        assert d is not None, f"{table}.{type_col} has no vocab domain"
        unknown = {code for code in d.code_set if tables_of.get(code) not in s}
        assert not unknown, f"{table}.{type_col}: codes without a table {unknown}"


# ── enum classification ──────────────────────────────────────────────────


def test_every_enum_like_column_is_classified():
    missing = sorted(enum_like(schema()) - set(_classified()))
    assert not missing, (
        f"enum-like columns without a vocab entry: {missing}. Register a domain in "
        "oncofiles.vocab (COLUMN_DOMAINS), or list it in REFERENCE_CODE_COLUMNS / "
        "FREE_TEXT_BY_DESIGN with a reason."
    )


def test_vocab_classifies_each_column_once_and_only_existing_columns():
    s = schema()
    twice = {k: v for k, v in _classified().items() if "+" in v}
    assert not twice
    gone = sorted((t, c) for t, c in _classified() if t not in s or c not in s[t].columns)
    assert not gone, f"vocab lists columns that do not exist: {gone}"


def test_vocab_domains_load_and_are_used():
    reg = vocab.registry()
    assert set(vocab.COLUMN_DOMAINS.values()) <= set(reg)
    assert set(reg) <= set(vocab.COLUMN_DOMAINS.values()), "unused domain"
    for d in reg.values():
        codes = [c.code for c in d.codes]
        assert codes and len(codes) == len(set(codes)), d.name
        assert d.deprecated <= d.code_set, d.name


def test_vocab_labels_are_complete():
    assert vocab.label_gaps() == {}


def test_vocab_reuses_existing_constants():
    """Spot checks: a domain is the existing allowlist, not a copy of it."""
    from oncofiles.curated.clinical_store import CYCLE_STATUSES
    from oncofiles.database._hard_facts import PROVENANCE_ACTIVITIES
    from oncofiles.models import DocumentCategory

    assert vocab.codes("document_category") == {c.value for c in DocumentCategory}
    assert vocab.codes("provenance_activity") == PROVENANCE_ACTIVITIES
    assert vocab.codes("treatment_cycle_status") == set(CYCLE_STATUSES)
    assert vocab.label("verification", "verified", "en") == "verified by a person"
    assert [r for r in vocab.rows() if r[:2] == ("verification", "machine")]


# ── the lint catches new schema (synthetic table) ────────────────────────


def _synthetic() -> dict[str, Table]:
    def col(name, type_="TEXT", notnull=False, default=None, pk=0):
        return Column(name, type_, notnull, default, pk)

    bad = Table(
        "bad_things",
        {
            c.name: c
            for c in (
                col("uuid", pk=1),
                col("patient_id", default="'erika'"),
                col("document_ref_id", "INTEGER"),
                col("organization_id", "INTEGER"),
                col("institution"),
                col("seen_time"),
                col("expires_at", "INTEGER"),
                col("last_updated"),
                col("payload", default="'{}'"),
                col("verification"),
            )
        },
        {"organization_id": ("organizations", "id")},
    )
    return {"bad_things": bad}


def test_lint_flags_a_new_table_breaking_every_rule():
    s = _synthetic()
    assert pk_violations(s) == {"bad_things": "uuid TEXT"}
    assert set(patient_fk_violations(s)) == {"bad_things"}
    assert set(patient_nullable_violations(s)) == {"bad_things"}
    assert set(patient_default_violations(s)) == {"bad_things"}
    assert set(id_violations(s)) == {("bad_things", "document_ref_id")}
    assert set(text_copy_violations(s)) == {("bad_things", "institution")}
    assert set(time_name_violations(s)) == {("bad_things", "seen_time")}
    assert set(instant_format_violations(s)) == {("bad_things", "expires_at")}
    assert set(projection_name_violations(s)) == {("bad_things", "last_updated")}
    assert set(json_name_violations(s)) == {("bad_things", "payload")}
    assert set(provenance_violations(s)) == {"bad_things"}
    assert ("bad_things", "verification") in enum_like(s) - set(_classified())


# ── the doc names every legacy exception ─────────────────────────────────


def test_doc_lists_every_legacy_exception():
    text = DOC.read_text()
    missing = []
    for rule, (_, allowlist) in LEGACY.items():
        for key in allowlist:
            needle = f"{key[0]}.{key[1]}" if isinstance(key, tuple) else key
            if f"`{needle}`" not in text:
                missing.append((rule, needle))
    assert not missing, f"add to Legacy exceptions in {DOC.name}: {missing}"
