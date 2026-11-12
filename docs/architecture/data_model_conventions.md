# Data model conventions

Status: normative for every **new** table and column (#789, phase 1). The data model
grew in generations (Drive era, Turso era, curated pipeline, hard-data model #711),
so older tables break some rules. They are listed under
[Legacy exceptions](#legacy-exceptions-found-by-the-lint) and fixed in small,
chunked PRs; the list only shrinks.

Enforcement:

- **`tests/test_schema_conventions.py`** (CI, every PR) builds the schema from
  `migrations/` the way the app does (`Database.migrate`) and checks rules 1–7.
  A new table or column that breaks a rule fails the build. Each legacy allowlist
  **may only shrink**: when a legacy item is fixed, the test fails until its entry
  is removed (from the test and from this doc). A synthetic bad table proves every
  check fires.
- **`src/oncofiles/vocab.py`** is the one registry of closed vocabularies (rule 4).
  The lint fails when an enum-like column has no entry there.

Never add a new item to a legacy allowlist to make new schema pass. Fix the schema.

---

## Rules

### 1. Tables and keys

Tables are `snake_case`, plural. The primary key is a surrogate `id INTEGER`. A public
or natural identity is a **separate** `UNIQUE` column: `fhir_id`, `natural_key`,
`gdrive_id`, `code`.

```sql
-- good
CREATE TABLE specimens (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    fhir_id  TEXT NOT NULL UNIQUE,
    ...
);
-- bad: the natural key is the primary key
CREATE TABLE lab_panels (code TEXT PRIMARY KEY, ...);
```

The one permanent exception is `patients`: `patients.patient_id` (TEXT UUID) is the
tenant key and the isolation boundary every query filters on. It is never migrated
to an integer.

### 2. Patient scope

Every patient-scoped table has `patient_id TEXT NOT NULL REFERENCES patients(patient_id)`,
**without a DEFAULT**, and every helper filters on it in SQL (keyword-only
`patient_id=`, see CLAUDE.md "DB int-id helpers"). A DEFAULT turns a forgotten
`patient_id` into a real-looking value instead of an error.

```sql
-- good
patient_id TEXT NOT NULL REFERENCES patients(patient_id)
-- bad: no FK, and a missing value silently becomes a patient
patient_id TEXT NOT NULL DEFAULT 'erika'
```

Shared reference data (`organizations`, `departments`, `fact_codes`, `devices`) has no
`patient_id`.

### 3. References

An FK column is `<entity>_id` and references `<entities>(id)`; only `patient_id`
references `patients(patient_id)`. A role prefix is allowed and encouraged when a
table has two references to the same entity: `source_document_id`,
`prescriber_practitioner_id`, `primary_condition_id`.

A column ending in `_id` is one of:

1. an FK (declared with `REFERENCES`);
2. half of a **polymorphic pair** `<x>_type` + `<x>_id`, where the type column has a
   vocab domain and the DB helper checks that the row exists and belongs to the same
   patient: `codings.owner_*`, `identifiers.owner_*`, `links.from_*` / `links.to_*`,
   `provenance.target_*`. Add a new pair only with an allowlist and a test;
3. an **external id** whose name says so: `fhir_id`, `gdrive_id`, `google_event_id`,
   `gmail_message_id`, `session_id`, `run_id`, `external_id` … (list:
   `EXTERNAL_ID_NAMES` in the lint).

```sql
-- good
source_document_id INTEGER REFERENCES documents(id)
-- bad: points at documents.id, but nothing guarantees the row exists
document_id INTEGER
```

A counter is not an id: name it `version`, never `version_id`.

### 4. Reference data and closed vocabularies

Two kinds of reference data, two shapes:

- **An entity with attributes** (organisation, department, doctor, device) is a row
  in its own table and is referenced by an **FK id**. Display text (name, code)
  comes from a join or a view, never from a text column copied next to the FK.
- **A closed vocabulary** (category, kind, status, verification, role, route …) is
  a **TEXT code**: lowercase `snake_case`, stable, never renamed. Each code is
  registered once in `vocab.py` with its domain, `label_sk` and `label_en`. Where
  an allowlist, enum or `Literal` already exists, the domain loads it from there
  instead of copying it. Validation on write and the read-only `ref_codes` view
  (for display and FHIR mapping) follow in a later phase.

Codes as keys are normal practice in reference data management: FHIR `system|code`,
ICD-10, LOINC, ATC all work this way, and so do ours (`fact_codes.code`,
`clinical_records.code`), as long as the code is **stable and controlled**. Codes
from an external standard keep the standard's spelling (FHIR's `entered-in-error`,
HL7's `H`/`L`, UCUM's `mg/m2`); `vocab.Domain.standard` names the standard.

What is wrong:

- **free text pretending to be a code**: a `status` column that takes whatever a
  caller sends, never validated (`clinical_records.status` today);
- **a duplicate of an FK**: `documents.institution` stores an organisation code
  (`"NOU"`) next to `documents.organization_id`. The same fact twice drifts.

```sql
-- good: the entity by id, the vocabulary by code
organization_id INTEGER REFERENCES organizations(id),
kind            TEXT NOT NULL            -- vocab domain 'procedure_kind'
-- bad: a text copy of the referenced entity
organization_id INTEGER REFERENCES organizations(id),
institution     TEXT                     -- 'NOU', also reachable via organization_id
```

Every enum-like column (found by name: `kind`, `*_type`, `status`, `category`,
`source`, `role`, `route`, `verification`, `*_code` …) is in exactly one `vocab.py`
list:

| List | Meaning |
|---|---|
| `COLUMN_DOMAINS` | a closed vocabulary; the column maps to a domain |
| `REFERENCE_CODE_COLUMNS` | a code used as a key into a reference table or an external code system not enumerated here (`fact_codes.code`, `codings.code`/`system`, IANA media types) |
| `FREE_TEXT_BY_DESIGN` | free text on purpose, with the reason (printed values, caller provenance tags, sunset tables, columns with no writer yet) |

### 5. Time

| Kind | Name | Value |
|---|---|---|
| instant | `<x>_at` | TEXT, UTC ISO-8601 `YYYY-MM-DDTHH:MM:SSZ`; default `strftime('%Y-%m-%dT%H:%M:%SZ', 'now')` |
| calendar date | `<x>_date` | TEXT, ISO date `YYYY-MM-DD` |
| range | `<x>_start_at` / `<x>_end_at` + `<x>_precision` | precision from the vocab domain `date_precision` |

Audit columns: `created_at`, `updated_at`, `deleted_at` (soft delete, with
`deleted_by`), `version` (an integer counter). `last_updated` and `version_id` are
FHIR `meta` names: they exist **only in the FHIR projection**, never as columns.

```sql
-- good
collected_at TEXT, effective_start_at TEXT, effective_end_at TEXT, effective_precision TEXT
-- bad
collected TEXT, start_time TEXT, expires_at INTEGER, last_updated TEXT
```

### 6. JSON, booleans, printed vs normalised values

- A JSON column ends in `_json` (`metadata_json`, `report_json`), whatever it holds.
- A boolean is `is_<x>` / `has_<x>`: an `INTEGER NOT NULL` flag, 0 or 1.
- A value read from a page keeps its printed form and its normalised form side by
  side:

  | Suffix | Meaning | Example |
  |---|---|---|
  | `<x>_printed` | exactly as on the page | `unit_printed = 'mmol/l'` |
  | `<x>_canonical` | our normalised unit or value | `unit_canonical = 'mmol/L'` |
  | `<x>_ucum` / `<x>_code` | the international code | `unit_ucum = 'mmol/L'`, `interpretation_code = 'H'` |

`clinical_records` value columns, defined once here:

| Column | Meaning |
|---|---|
| `value_num` | the number as printed, in the printed unit |
| `value_text` | a printed non-numeric result (`'wild-type'`, `'G12S'`), or several dimensions |
| `unit_printed` | the unit as printed (rule-6 name; #711) |
| `unit` | legacy copy of `unit_printed`; the lab rule parsers write only this one, readers use `unit_printed or unit` |
| `value_canonical` | `value_num` converted to `unit_canonical` (`fact_codes.unit_conversions`) |
| `unit_canonical` | our unit for the code (`fact_codes.unit_canonical`) |
| `unit_ucum` | UCUM code of the unit; while it is empty, the FHIR projection derives it (`fhir.terminology.ucum_code`) |
| `flag` | abnormal flag `H`/`L` (printed arrow, or the value outside the printed or generic range); vocab `observation_interpretation` |
| `interpretation_code` | HL7 v3 interpretation; the projection falls back to `flag` |

### 7. Provenance

Every fact carries `source_document_id`, `page_number`, `evidence` (the verbatim
quote), `extracted_by` (`<extractor version>/<model>`) and `verification` (vocab
`verification`: machine | unverified | verified | corrected — our QA state, never a
clinical status). The lint checks that every table with `verification` has all five.
Each action on a fact also gets a `provenance` row (see `hard_data_model.md`).

### 8. Normal form and caches

Entity data is in third normal form: one fact in one place. A denormalised copy is
allowed only as a **documented cache with a rebuild path**, listed here:

| Cache | Source of truth | Rebuild |
|---|---|---|
| `documents` rows with `file_id = 'gdrive:…'`, `document_pages` with `model = 'curated-sidecar'` | the curated Drive folder (sidecars) | `curated_index` (incremental by sidecar `modified`) |
| machine facts in `clinical_records` (rule parsers) | the stored sidecar text | re-extraction (`replace_document_facts`; verified / corrected rows are kept) |
| `documents_fts`, `conversation_entries_fts` | their base tables | triggers from migrations 001 / 003 |

Known duplicates that are **not** documented caches (to be removed): see
[Known, not linted](#known-not-linted).

---

## Checklist for a new table or column

1. `id INTEGER PRIMARY KEY`, natural identity as a separate `UNIQUE` column.
2. Patient-scoped: `patient_id TEXT NOT NULL REFERENCES patients(patient_id)`, no
   DEFAULT; helpers take keyword-only `patient_id=` and filter in SQL.
3. Every `*_id` has `REFERENCES`, or is an external id by name, or a documented
   polymorphic pair.
4. A code column: register its domain in `vocab.py` (`COLUMN_DOMAINS`, labels sk/en),
   reusing an existing allowlist if there is one. Free text: `FREE_TEXT_BY_DESIGN`
   with a reason. A reference to an entity: an FK id, never its name or code.
5. Times `*_at` (ISO-8601 TEXT) / `*_date`; JSON `*_json`; booleans `is_*` / `has_*`.
6. A fact table: the five provenance columns.
7. Migrations stay additive, no bulk data fixes at startup (CLAUDE.md).

---

## Legacy exceptions (found by the lint)

Every item below is in an allowlist in `tests/test_schema_conventions.py`; the test
keeps this list complete. Fixes go in small chunked PRs, never as startup bulk
migrations. SQLite cannot add an FK or change a column type in place, so (b) needs
table rebuilds in a maintenance window, after a verified snapshot.

**Migration plan** (order from #789):

- **(a)** `documents.institution` becomes derived from `organization_id` (view or
  read-time join), then the column is dropped from writes.
- **(b)** `patient_id` FKs (and the missing `*_id` FKs, NOT NULL, no DEFAULT) via
  table rebuilds, curated/fact tables first, one table per PR.
- **(c)** Timestamp and JSON naming: views with the conventional names first, then
  readers move to them, columns last.

### Rule 1: primary key is not `id INTEGER` (8)

- `patients` — **permanent**: the tenant key `patient_id` (TEXT UUID).
- `fact_codes` — the code itself is the PK; every `clinical_records.code` points at
  it. Fix: none planned (stable reference codes, rule 4).
- `curated_settings` — PK `patient_id` (also no FK). Fix: (b).
- `patient_selection` — PK `owner_email`. Fix: with (b), low priority.
- `mcp_oauth_clients` — PK `client_id` as presented by the client. Fix: none planned.
- `mcp_oauth_tokens` — PK: token key (auth table, format tracked separately).
- `newsletter_subscribers` — PK `id` is a TEXT UUID. Fix: none planned.
- `session_revocations` — PK is the session token's `tid`. Fix: none planned.

### Rule 2: `patient_id` without FK to `patients` (17) — fix (b)

`documents`, `document_references`, `conversation_entries`, `treatment_events`,
`curated_runs`, `curated_settings`, `fact_rereads`, `prompt_log` (also stores a
non-UUID system sentinel), `activity_log`, `agent_state`, `research_entries`,
`calendar_entries`, `email_entries`, `oauth_tokens`, `onboarding_events`,
`patient_context`, `sync_history` (`''` for system-wide rows).

`prompt_log`, `sync_history` and `agent_state` hold rows that belong to no patient;
they need a decision (nullable FK or a system patient) before (b).

### Rule 2: nullable `patient_id` (3)

- `codings`, `identifiers` — **by design**: NULL when the owner is shared reference
  data (an organisation, department, device or fact code).
- `curated_settings` — `TEXT PRIMARY KEY` without `NOT NULL`; SQLite then allows
  NULL. Fix: (b).

### Rule 2: `patient_id` with a DEFAULT (12) — fix (b)

`DEFAULT 'erika'` (single-patient era): `activity_log`, `conversation_entries`,
`documents`, `prompt_log`, `research_entries`, `treatment_events`.
`DEFAULT 'default'`: `calendar_entries`, `email_entries`, `oauth_tokens`.
`DEFAULT ''`: `agent_state`, `patient_context`, `sync_history`.

### Rule 3: `*_id` without FK (6) — fix (b)

- `documents.split_source_doc_id` — points at `documents.id`.
- `documents.group_id` — TEXT UUID shared by a document group; there is no groups
  table. Fix: (b) with a `document_groups` table, or rename to `group_key`.
- `fact_rereads.document_id`, `prompt_log.document_id` — point at `documents.id`.
- `calendar_entries.treatment_event_id` — points at `treatment_events.id`.
- `mcp_oauth_tokens.client_id` — points at `mcp_oauth_clients.client_id`.

### Rule 4: text copy of an FK (1) — fix (a)

- `documents.institution` — organisation code next to `organization_id`.

### Rule 4: codes that are not `snake_case` (3) — kept

Renaming a stored code is itself forbidden (rule 4), so these stay; they are listed
so no new domain copies the style. Domains with a `standard` keep its spelling and
are not listed.

- `curated_run_trigger` — `auto-preview`, `nightly-preview`.
- `device_kind` — `llm-extractor`.
- `diary_participant` — `claude.ai`, `claude-code` (client names).

### Rule 5: time value not named `*_at` / `*_date` (10) — fix (c)

- `calendar_entries.start_time`, `calendar_entries.end_time` — rule: `start_at` / `end_at`.
- `clinical_records.effective_start`, `clinical_records.effective_end`,
  `medication_events.effective_start`, `medication_events.effective_end` — rule:
  `effective_start_at` / `effective_end_at`.
- `procedures.performed_end` — rule: `performed_end_at`.
- `documents.gdrive_modified_time` — rule: `gdrive_modified_at`.
- `email_entries.date` — rule: `sent_at`.
- `oauth_tokens.token_expiry` — rule: `token_expires_at`.

### Rule 5: `*_at` that is not an ISO-8601 TEXT instant (4) — fix (c)

- `mcp_oauth_tokens.expires_at`, `session_revocations.revoked_at`,
  `session_revocations.expires_at` — INTEGER epoch seconds (FastMCP's token objects
  use epoch seconds; convert at that boundary).
- `patient_selection.updated_at` — `DEFAULT datetime('now')` (space, no `T`/`Z`).

### Rule 5: FHIR projection names as columns (20) — fix (c)

`last_updated` (rule: `updated_at`) and `version_id` (rule: `version`) on the #711
tables. `clinical_records` has `updated_at` **and** `last_updated`.

`clinical_records.last_updated`, `clinical_records.version_id`,
`cancer_stages.last_updated`, `cancer_stages.version_id`,
`conditions.last_updated`, `conditions.version_id`,
`devices.last_updated`, `devices.version_id`,
`medication_events.last_updated`, `medication_events.version_id`,
`procedures.last_updated`, `procedures.version_id`,
`reports.last_updated`, `reports.version_id`,
`specimens.last_updated`, `specimens.version_id`,
`treatment_courses.last_updated`, `treatment_courses.version_id`,
`treatment_cycles.last_updated`, `treatment_cycles.version_id`.

### Rule 6: JSON column not named `*_json` (20) — fix (c)

- Objects: `documents.structured_metadata`, `email_entries.structured_metadata`,
  `treatment_events.metadata`, `newsletter_subscribers.metadata`,
  `fact_codes.unit_conversions`, `agent_state.value`.
- Arrays: `documents.ai_tags`, `conversation_entries.tags`, `activity_log.tags`,
  `research_entries.tags`, `clinical_analyses.tags`, `clinical_record_notes.tags`,
  `email_entries.recipients`, `email_entries.labels`, `calendar_entries.attendees`,
  `fact_codes.aliases`, `oauth_tokens.granted_scopes`.
- Id arrays (also a 1NF break, rule 8): `email_entries.linked_document_ids`,
  `conversation_entries.document_ids`, `clinical_analyses.record_ids`.

---

## Known, not linted

Found during the #789 audit; no lint rule covers them yet.

- **Duplicates (rule 8):** `clinical_records.unit` vs `unit_printed` (see rule 6);
  `patients.profile` copies `patient_context` → `patient_type` and nothing in
  `src/` reads it.
- **Code as key without an FK:** `clinical_records.code` → `fact_codes.code` (rule 3
  would want `REFERENCES fact_codes(code)`; add it in the (b) rebuild).
- **Shortened FK names** without the entity: `course_id`, `cycle_id` (treatment),
  `request_id` (medication event), `record_id` (clinical record),
  `previous_version_id` (document). Allowed as they are; new FKs use
  `<role>_<entity>_id`.
- **Booleans without `is_` / `has_`:** `curated_runs.dry_run`,
  `curated_settings.nightly_enabled`, `documents.gdrive_parent_outside_root`
  (nullable, DEFAULT 0).
- **Rule-6 naming:** `treatment_courses.regimen_normalised` (rule: `_canonical`),
  `medication_events.route` holds the printed route (rule: `route_printed`).
- **Mixed vocabulary:** `clinical_records.status` mixes clinical status
  (active, resolved) with interpretation (high, low); new data uses
  `conditions.clinical_status` and `interpretation_code`.
- **Unvalidated codes:** most domains in `vocab.py` are not checked on write yet;
  the planned admin `data_quality_report` counts values outside the registry,
  orphan FKs and disagreeing duplicates (`documents.institution` vs
  `organization_id`).
- **Soft delete and versioning** are complete (`deleted_at` + `version_id` +
  `fhir_id`) only on the #711 tables; older patient tables mix them.
