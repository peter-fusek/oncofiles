# Data model and access rights

Status: index v9. The diagrams are Mermaid and render on GitHub.
The full column list per table is generated from `migrations/` (73 files). See
"Regenerate the table list" at the end.

Contents:
1. Core model: patients, documents, facts
2. Users and access rights today
3. Target access-rights model (proposal, #650)
4. Drive replica (planned, #616)

---

## 1. Core model

Shared reference data has no `patient_id`: `organizations`, `departments`, `fact_codes`.
Every other table carries `patient_id`, and every query filters on it.
The int-id helpers require `patient_id=` as a keyword (#499).

```mermaid
erDiagram
  patients ||--o{ documents : owns
  patients ||--o{ clinical_records : owns
  patients ||--o{ encounters : has
  patients ||--o{ practitioners : "sees (patient-scoped)"
  patients ||--o{ treatment_events : has
  patients ||--o{ links : has
  patients ||--|| patient_context : has
  patients ||--o{ curated_runs : has
  documents ||--o{ document_pages : "has pages"
  documents ||--o{ clinical_records : "source of"
  documents ||--o{ document_cross_references : relates
  documents |o--o| documents : "previous version"
  fact_codes ||--o{ clinical_records : classifies
  encounters ||--o{ documents : groups
  encounters ||--o{ clinical_records : groups
  organizations ||--o{ departments : has
  organizations ||--o{ encounters : at
  practitioners ||--o{ encounters : leads
  clinical_records ||--o{ clinical_record_audit : audited
  clinical_records ||--o{ clinical_record_notes : annotated

  documents {
    int id PK
    text patient_id FK
    text file_id "gdrive:<id> for curated"
    text document_date
    text category
    text source_url "Drive link"
    int encounter_id FK
  }
  clinical_records {
    int id PK
    text patient_id FK
    text code FK
    text occurred_at
    real value_num
    real value_canonical
    text flag "H/L"
    int source_document_id FK
    int page_number
    text evidence "exact source text"
    text verification "machine|unverified|verified|corrected"
    text extracted_by "rule:bory-v1 ..."
    text metadata_json "date_source, flag_source ..."
  }
```

**Lineage of one value:**
1. Drive inbox.
2. Local OCR.
3. Sidecar `.md`.
4. `documents` + `document_pages` (curated index).
5. `clinical_records`, built by the rule parsers in `curated/facts_rules.py`.
6. `fact_series_v`.
7. MCP `fact_series`: dedupe, statistics over verified values, `latest_any`.
8. The answer in the client, with a Drive link and page number.

---

## 2. Users and access rights today

**There is no user entity.** A user is a Google email string that appears in four unrelated
places. Admin is an environment variable.

```mermaid
erDiagram
  GOOGLE_EMAIL ||--o{ patients : "listed in caregiver_email (CSV)"
  GOOGLE_EMAIL ||--o{ mcp_oauth_tokens : "bound at /authorize"
  GOOGLE_EMAIL ||--o| patient_selection : "current patient pick"
  GOOGLE_EMAIL ||--o{ DASHBOARD_SESSION : "signed cookie email|exp|tid|sig"
  GOOGLE_EMAIL |o--o| ENV_ADMIN_LIST : "DASHBOARD_ADMIN_EMAILS"
  patients ||--o{ patient_tokens : "onco_* bearer = full access"
  patients ||--o{ oauth_tokens : "Drive/Gmail connection (owner_email)"
  mcp_oauth_clients ||--o{ mcp_oauth_tokens : issues
  STATIC_BEARER ||--|| ENV_ADMIN_LIST : "MCP_BEARER_TOKEN = implicit admin"

  GOOGLE_EMAIL {
    string email "no table, compared as lower-case string"
  }
  patients {
    text patient_id PK
    text slug
    text caregiver_email "comma-separated emails = all members, no roles"
    text tier "billing per patient"
    int is_active
  }
  patient_tokens {
    int id PK
    text patient_id FK
    text token_hash
    text label
    int is_active "no expiry, no owner, no role"
  }
  mcp_oauth_tokens {
    text token PK
    text client_id
    text user_email
    int expires_at
  }
  patient_selection {
    text owner_email PK
    text patient_id FK
  }
  oauth_tokens {
    int id PK
    text patient_id
    text provider
    text owner_email "Google account that owns the Drive"
    text gdrive_folder_id
  }
  session_revocations {
    text tid PK
    int expires_at
  }
```

### How a request gets its patient

```mermaid
flowchart TD
  R[request] --> K{credential}
  K -->|static MCP_BEARER_TOKEN| S["admin = true<br/>patient = single active patient, else none"]
  K -->|MCP OAuth token| O["email = mcp_oauth_tokens.user_email<br/>admin = email in DASHBOARD_ADMIN_EMAILS"]
  K -->|onco_* patient token| P["patient = token's patient<br/>admin = false"]
  K -->|dashboard cookie| D["email from signed cookie<br/>admin = email in DASHBOARD_ADMIN_EMAILS"]
  K -->|stdio| T["first active patient, admin = false"]
  O --> M{"patients whose caregiver_email<br/>contains the email"}
  M -->|1| ONE[that patient]
  M -->|many| SEL["patient_selection[email]<br/>or patient_slug per call"]
  M -->|0| NONE["NO_PATIENT_ACCESS_SENTINEL"]
  S & ONE & SEL & P & D & T --> G{"_resolve_patient_id(slug)<br/>ACL gate"}
  G -->|admin| ALLOW[allow any patient]
  G -->|"bound patient = slug's patient<br/>or email == caregiver_email"| ALLOW
  G -->|otherwise| DENY["access denied<br/>(no existence oracle)"]
```

### Gaps in the current model
1. **No users or roles.** Membership is a comma-separated string, and every member has full
   read and write access. Nothing distinguishes a read-only family member, the patient themselves,
   or a second-opinion doctor.
2. **No grant history.** Anyone who already matches `caregiver_email` can PATCH the list:
   add any email, or remove the other members. There is no invitation, consent, `granted_by` or revocation trail.
3. **Admin is overloaded.** `DASHBOARD_ADMIN_EMAILS` is used for system endpoints *and* gives
   full PHI access to every patient (MCP resolver, body ACL). The `_is_admin_email` docstring
   says the opposite ("NOT automatic access to all patients' medical data"). Admin PHI reads
   are not audited.
4. **Inconsistent matching.** The MCP resolver ACL compares `caller_email == caregiver_email`
   as a whole string. Login resolution, `list_patients` and the dashboard split the CSV.
   A second caregiver in the list therefore resolves to the patient by default but is denied
   when they pass `patient_slug`. This fails closed, so there is no leak.
5. **Tokens are owner-less.** `onco_*` patient tokens have no user, no role cap and no expiry.
   The static bearer (Oncoteam) is an implicit admin over all patients, not a service with
   explicit grants.
6. **Three copies of the policy**, in `persistent_oauth.py`, `tools/_helpers.py` and `server.py`.

---

## 3. Target access-rights model (proposal, #650)

```mermaid
erDiagram
  users ||--o{ patient_members : "member of"
  patients ||--o{ patient_members : "has members"
  users ||--o{ api_tokens : owns
  service_principals ||--o{ api_tokens : owns
  patients ||--o{ api_tokens : "scoped to"
  service_principals ||--o{ service_grants : has
  patients ||--o{ service_grants : "granted to service"
  users ||--o| patient_selection : selects
  users ||--o{ mcp_oauth_tokens : "bound to"
  users ||--o{ data_connections : "connected by"
  patients ||--o{ data_connections : has
  users ||--o{ access_audit : "acts (principal)"
  patients ||--o{ access_audit : "about"

  users {
    text user_id PK "uuid"
    text email UK "normalised"
    text google_sub UK "stable across email changes"
    text display_name
    text system_role "none|operator|admin"
    text status "active|disabled"
    text created_at
  }
  patient_members {
    text patient_id PK, FK
    text user_id PK, FK
    text role "owner|editor|viewer"
    text relation "self|spouse|parent|child|other|clinician"
    text status "invited|active|revoked"
    text granted_by FK
    text granted_at
    text revoked_by FK
    text revoked_at
  }
  api_tokens {
    int id PK
    text token_hash UK
    text user_id FK "or service_id"
    text service_id FK
    text patient_id FK "scope"
    text role_cap "<= member role"
    text expires_at
    text last_used_at
    text revoked_at
  }
  service_principals {
    text service_id PK "e.g. oncoteam"
    text name
  }
  service_grants {
    text service_id PK, FK
    text patient_id PK, FK
    text role "editor|viewer"
  }
  data_connections {
    int id PK
    text patient_id FK
    text provider "google"
    text connected_by FK
    text account_email
    text folder_id
  }
  access_audit {
    int id PK
    text at
    text principal "user:<id> | service:<id> | token:<id>"
    text patient_id
    text action
    text decision "allow|deny|break_glass"
    text reason
  }
```

### Roles and permissions

| Action | owner | editor | viewer | system operator | system admin |
|---|:-:|:-:|:-:|:-:|:-:|
| Read documents, facts, timeline | ✓ | ✓ | ✓ | – | break-glass, reason + audit |
| Upload, verify/correct facts, notes, treatment | ✓ | ✓ | – | – | break-glass |
| Run curated pipeline / index | ✓ | ✓ | – | – | ✓ (ops) |
| Connect Drive/Gmail, change folder | ✓ | – | – | – | – |
| Invite, change role, revoke members; issue tokens | ✓ | – | – | – | – |
| Archive patient | ✓ | – | – | – | ✓ |
| Health detail, schedules, costs, aggregated logs (no PHI) | – | – | – | ✓ | ✓ |
| Cross-patient tools (`reassign_document`, `query_db`) | – | – | – | – | ✓ audited |

A patient always has at least one `owner`. The last owner cannot leave or be revoked.
`relation` describes the person and grants nothing. `self` marks the patient themselves.

### One decision point

```mermaid
flowchart LR
  C[credential] --> PR["principal<br/>user / service / token"]
  PR --> A{"authorize(principal, patient, action)"}
  A -->|disabled or revoked| DENY
  A -->|system action| SR{system_role}
  A -->|patient action| MB{"active membership or service grant<br/>role permits action"}
  MB -->|token| CAP{"token scope = patient<br/>and action <= role_cap"}
  MB -->|no| BG{"admin + break-glass reason?"}
  SR & CAP --> ALLOW
  BG -->|yes| AUD["allow + access_audit"]
  BG -->|no| DENY
```

`authorize()` replaces the three current copies. MCP tools, REST endpoints and the dashboard
all call it, and denials keep the current "no existence oracle" response.

### Migration (no behaviour change until the switch)
1. Create the tables. Backfill `users` from every distinct email in `caregiver_email`, `mcp_oauth_tokens.user_email`,
   `patient_selection` and the admin list.
   Every current `caregiver_email` member becomes `owner`, so access is unchanged.
2. Run in shadow mode for one release: `authorize()` runs next to the old checks and logs
   mismatches. Writes to `caregiver_email` are mirrored into `patient_members`.
3. Switch the three call sites to `authorize()`. `caregiver_email` becomes read-only, later dropped
   (no `ALTER TABLE RENAME`, see CLAUDE.md).
4. The owner assigns real roles in the dashboard (invite, role, revoke).

---

## 4. Drive replica (planned, #616)

The database is the master. A derived, read-only copy goes back into the patient's own Drive folder
(`_record.md`, `_record.json`, plus a description and properties on each filed file). An agent
with only the native Google Drive connector can then use the folder without Oncofiles. The copy is
never read back as input, with one exception: human verifications can be restored after a DB loss (#649).

```mermaid
flowchart LR
  DB[(DB master)] -->|export, hash skip| REC["_record.md / _record.json"]
  DB -->|description + properties| SRC[filed source files]
  REC & SRC --> NATIVE["Native Google Drive connector<br/>(no Oncofiles)"]
  REC -.human verifications only.-> DB
```

Sub-issues: #644 contract and ignore rules, #645 exporter, #646 writer and triggers, #647 file
metadata, #648 acceptance with the native connector, #649 restore of human verifications.

---

## Regenerate the table list

```bash
python3 - <<'EOF'
import sqlite3, glob
c = sqlite3.connect(":memory:")
for f in sorted(glob.glob("migrations/*.sql")):
    try: c.executescript(open(f).read())
    except Exception: pass  # 043 references a table created later
for (t,) in c.execute("select name from sqlite_master where type='table' and name not like '%fts%' order by 1"):
    print(t, [r[1] for r in c.execute(f"pragma table_info('{t}')")])
EOF
```
