# Hard-data model and interoperability (design, 2026-11-11)

Status: **proposal for maintainer review** · Issues: #711 (epic), #714 (LLM-first extraction), #716 (standards), #619, #636
Research basis (with sources) is in the #716 comments: US (FHIR / US Core / SMART / Epic), EU + SK (EHDS / HL7 Europe IGs / eZdravie), oncology + terminologies (mCODE / ECCDM / ICD-O-3 / TNM / OMOP).

## 1. Decisions already made by the maintainer (2026-11-11)

1. **Oncofiles is the hard-data backend.** Every hard clinical fact — result, diagnosis, treatment/medication, procedure, visit — is stored with *what / when / where / who* and a verifiable link to its source document (document, page, verbatim evidence).
2. **Interpretation moves out** of Oncofiles (pre-cycle checklist, lab safety check, preventive-care advice, AI lab analysis …). They belong to a layer above (the chat client or a separate service).
3. **Patient context keeps soft data only** (socio-demographics, family/caregivers, preferences, notes). Hard items move to facts with provenance.
4. **Extraction is LLM-first** (#714): a local model extracts, a generic deterministic verifier decides trust. No new per-layout parsers.
5. **Map to the standards used in practice**, not a proprietary model: an agent that knows Epic / HL7 FHIR / SMART on FHIR must be able to consume Oncofiles, and data must be migratable in/out.

## 2. Target standards (what we align to)

| Layer | Target | Why |
|---|---|---|
| Interop format | **HL7 FHIR R4** | Epic, US Core, SMART App Launch, Bulk Data, mCODE, IPS and all European IGs are built on this release; the next major release is not published yet (versions: see the standards issue). |
| EU content | **IPS 2.0.1 / European Patient Summary**, **HL7 Europe Laboratory Report 2.1**, Hospital Discharge Report 1.0, Medication Prescription & Dispense 1.0, EU Core 2.0 | EHDS priority categories (patient summary 2029; lab, imaging, discharge 2031) will use the EEHRxF, expected on these IGs. |
| Oncology | **mCODE 4.0** semantics + **HL7 Europe Common Cancer Model** (ballot) | mCODE is US-realm, so we align semantically; ECCDM is the EU counterpart with FHIR + OMOP maps. |
| Research export (later) | **OMOP CDM 5.4/5.5 + Oncology extension** (EPISODE) | Used by EU secondary-use projects; export only, not internal. |
| Auth | MCP OAuth 2.1 (as today) with **SMART-style scopes** later (`patient/Observation.rs`) | Same OAuth/PKCE family; ties into the access-rights epic #650. |
| Internal storage | **Relational SQLite, FHIR-mappable losslessly** | We keep our own tables (provenance, verification, Slovak specifics) and project to FHIR at the API. Not a FHIR server internally. |

### Code systems per fact kind (store national **and** international where both exist)

| Fact | Store | Optional | Never |
|---|---|---|---|
| Lab / tumour marker | our short code + **LOINC**; printed unit + **UCUM** unit; NCZI lab-item code (OID `1.3.158.00165387.100.10.52`) when printed | NPU | — |
| Diagnosis | printed text + **MKCH-10** code (OID `…100.10.25`, with code-list version) ≈ **WHO ICD-10** (`http://hl7.org/fhir/sid/icd-10`); cancer: **ICD-O-3.2** morphology/behaviour + topography | SNOMED CT concept id (SK is a SNOMED member) | ICD-10-CM (US) |
| Stage | printed string + parsed system (UICC/AJCC), edition, prefix (c/p/yp/r), T, N, M, group, date | SNOMED AJCC qualifier id | TNM/AJCC criteria or derivation rules (licensed) |
| Medication | printed text + INN + **ATC** + **ŠÚKL** product code; dose in UCUM; route/form in EDQM terms | EMA SMS/PMS id | RxNorm (US-only; only in an OMOP export) |
| Regimen | printed label + normalised name | NCIt code (CC BY); HemOnc id only after a licence check | — |
| Procedure | printed text | SNOMED CT | ICD-10-PCS (US) |
| Genomics | HGNC gene + HGVS variant | — | — |
| Performance status | ECOG / Karnofsky LOINC answer | — | — |
| Organisation / practitioner | name + ÚDZS provider / doctor code, IČO | — | — |

Licensing rules for the public repo: add a LOINC notice (done with this doc); never commit SNOMED RF2 files, full ICD or MKCH code lists, TNM/AJCC content, OMOP Athena bundles or HemOnc content. Code lists are loaded at runtime from their publishers.

## 3. Data model (additive migrations; nothing is dropped in phase 1)

### 3.1 Shared building blocks

- **`codings`** — many codes per entity: `(owner_type, owner_id, system_uri_or_oid, code, display, system_version, is_original, mapped_by, equivalence)`. `is_original` = the code as printed (e.g. MKCH-10); mapped ones carry who/what mapped them. Replaces the single `fact_codes.loinc` column (kept for compatibility until phase 5).
- **`provenance`** — one row per action on any fact (FHIR Provenance): `target_type, target_id, target_version, activity (extract | reread | verify | correct | import | manual), agent_role (assembler | verifier | author | enterer), agent_device_id | agent_user, source_document_id, page_number, char_offset, char_length, quote, confidence, recorded_at`. Today's `page_number/evidence/extracted_by` columns become the denormalised "current" copy.
- **`devices`** — every extractor that can be an agent: OCR model + version, LLM extractor prompt/schema version (`EXTRACTOR_VERSION`), rule parsers (during the transition), the verifier version.
- **`identifiers`** — `(owner_type, owner_id, system, value)` for organisations, practitioners, encounters, documents (ÚDZS codes, IČO, accession numbers).
- **Identity & versions** on every fact: `fhir_id` (UUID, stable), `version_id`, `last_updated`. A correction creates a new version; re-extraction never silently rewrites a verified/corrected fact (as today).
- **Verification stays our QA vocabulary** (`machine | unverified | verified | corrected`) and is kept **separate** from clinical status fields. Export: AI-assisted facts carry `meta.security = AIAST` (AI Transparency IG, ballot) + Provenance (assembler device, quotation entity → DocumentReference + page); `verified` adds a `verifier` agent; `corrected` adds a new version with entity role `revision`. Never map `unverified` → `Observation.status = preliminary`.
- **Patient-entered data** (EHDS Art 5): facts entered by a caregiver without a document get `activity = manual`, `agent_role = author` (RelatedPerson), `verification = unverified`, reason `no_source_document`.

### 3.2 Observations (labs, tumour markers, vitals, performance status, genomic results) — extend `clinical_records`

Add: `result_status` (FHIR ObservationStatus as printed/implied by the report — normally `final`), `value_comparator` (`<`, `>`, `<=`, `>=`), `unit_printed` vs `unit_ucum`, `interpretation_code` (FHIR v3 H/L/HH/LL/A/N next to the printed flag), `effective_start/effective_end/effective_precision` (year | month | day | datetime; FHIR allows partial dates), `issued_at` (report time, separate from the sample time), `specimen_id`, `report_id`.
New: **`specimens`** (type SNOMED, collected_at, accession) and **`reports`** (DiagnosticReport: panel code, status, issued, interpreter, conclusion, source document).

### 3.3 Conditions (diagnoses) — new `conditions`

`printed_text`, codings (MKCH-10 / ICD-10 / ICD-O-3 / SNOMED), `category` (problem-list-item | encounter-diagnosis), `clinical_status` (active | recurrence | relapse | remission | resolved), `clinical_certainty` (FHIR verificationStatus: provisional | differential | confirmed | refuted — from the document's own wording, e.g. "susp." → provisional; **not** our QA), `onset_at`, `asserted_at` (date of diagnosis), `abatement_at`, `recorded_at`, `body_site` + `laterality`, `is_primary_cancer`, `primary_condition_id` (for metastases / secondary sites), `grade`, `asserter_practitioner_id`, `encounter_id`, `basis_of_diagnosis` (ECIS: clinical | histology | cytology | imaging | marker), plus provenance.
**`cancer_stages`**: `condition_id, printed, system (UICC|AJCC), edition, prefix (c|p|yc|yp|r), t, n, m, stage_group, assessed_at`, provenance.

### 3.4 Treatments — new `treatment_courses`, `treatment_cycles`, `medication_events`

- **`treatment_courses`** (regimen = FHIR CarePlan, OMOP EPISODE "Treatment Regimen"): `regimen_printed`, `regimen_normalised`, codings (NCIt), `line_of_therapy`, `intent` (curative | palliative | adjuvant | neoadjuvant …), `setting`, `start_at`, `end_at`, `status`, `termination_reason`.
- **`treatment_cycles`** (CarePlan `partOf` course; OMOP child episode): `course_id`, `cycle_number_printed`, `planned_at`, `actual_at`, `dose_modification_reason`.
- **`medication_events`** (FHIR MedicationRequest | MedicationAdministration | MedicationStatement | MedicationDispense): `kind`, `course_id`/`cycle_id`, `ingredient_printed`, INN, codings (ATC, ŠÚKL, SMS), `dose_value` + `dose_unit_ucum`, `normalization_basis` (per m² | per kg | flat), `route` (EDQM), `timing_text`, `effective_start/end`, `authored_at` (for requests), `prescriber_practitioner_id`, `status`, `request_id` (administration → its request), provenance.
- **`procedures`** (surgery, biopsy, radiotherapy course summary, other): `kind`, `printed_text`, codings (SNOMED), `performed_at` (+ end), `body_site`, `performer`, `outcome`, `encounter_id`, provenance.
- Legacy `treatment_events` (no provenance) is frozen and migrated into these tables in phase 4.

### 3.5 Encounters, practitioners, organisations, documents

Encounters exist (#603); add `class` (AMB | IMP | EMER | VR …), identifiers. Practitioners (patient-scoped) and organisations stay; departments become Organisations with `partOf` in the projection. Documents project to **DocumentReference** (LOINC document type mapped from our category, `content.attachment` = Drive URL + hash, OCR text as a `text/plain` rendition via `relatesTo = transforms`).

### 3.6 Patient context → soft data only

| Stays in context (soft) | Moves to facts (hard) |
|---|---|
| name, medical-record name, date of birth, sex, home region, patient type, language, caregivers, free notes | diagnosis, staging, histology, tumour site, diagnosis date → `conditions` + `cancer_stages`; biomarkers, germline findings → observations (genomic); regimen, current cycle → `treatment_courses`/`cycles`; metastases → secondary `conditions`; comorbidities → `conditions`; surgeries → `procedures`; excluded therapies → `conditions`/notes with provenance; physicians → `practitioners`; institution keys → `organisations` |

Migration tool (phase 4, extends #619): chunked, dry-run first, snapshot before. For each hard context item: find the source document among the patient's facts/documents; if found, link it (`activity = import`, evidence from the document); otherwise import as `unverified` with reason `no_source_document` and list it for the maintainer to attach a source. Never invent a source.

## 4. MCP surface

### 4.1 New FHIR-shaped tools (read-only, `readOnlyHint`, patient from token or `patient_slug`)

| Tool | FHIR equivalent |
|---|---|
| `fhir_capabilities` | CapabilityStatement (types, search params, code systems) |
| `fhir_search(type, params, _elements, _count, page)` | `GET [type]?…` → searchset Bundle; types: Patient, Observation, DiagnosticReport, Specimen, Condition, CarePlan, MedicationRequest, MedicationAdministration, MedicationStatement, Procedure, Encounter, Practitioner, PractitionerRole, Organization, DocumentReference, Provenance; params: `category`, `code` (`system|code` or `:text`), `date` ge/le, `status`, `_tag` (verification), `_include`, `_revinclude=Provenance:target` |
| `fhir_read(type, id)` | `GET [type]/[id]` |
| `patient_everything(start, end, _type, _since)` | `Patient/$everything` |
| `observation_lastn` / `observation_stats` | `Observation/$lastn`, `$stats` (over `latest_fact_v` / `fact_series`) |
| `document_text(document, page, offset, length)` | Binary / OCR rendition — lets an agent check the evidence itself |
| `export_patient_summary` | IPS / European Patient Summary document Bundle (unverified excluded by default) |
| `export_lab_report` | HL7 Europe Laboratory Report Bundle |
| `fhir_export(_type, _since)` | Bulk Data–style NDJSON |

Agent-friendliness: bounded responses (`_elements`, paging, size budget), OperationOutcome-style errors, terminology lookups, read-only by default.

### 4.2 Kept domain tools
`patient_overview`, `timeline`, `fact_catalog`, `fact_series`, `document_detail`, `encounter_detail`, `people_and_places`, the curated pipeline tools, document list/search/view (with `patient_slug`).

### 4.3 New write tools (audited, human as `verifier`/`author`)
`verify_fact`, `correct_fact` (new version + provenance; #636), `add_fact` (any kind, requires a source document **or** `no_source_document` reason; replaces `add_clinical_record`, `store_lab_values`, `add_treatment_event`).

### 4.4 Deprecations (from the 120-tool inventory)
- **Interpretation (8):** `get_lab_safety_check`, `get_precycle_checklist`, `get_lab_summary`, `get_preventive_care_status`, `analyze_labs`, `compare_labs`, `fetch_clinical_trials`, `add_clinical_analysis`. Also reduces EU MDR Rule 11 / AI-Act exposure.
- **Duplicates of the facts tools (10):** old `lab_values` readers/writers, `list_clinical_records`, `get_journey_timeline`, treatment-event tools, `get_document`/`get_document_by_id`, `get_related_documents`, `compare_lab_panels`.
- **Legacy pre-curated pipeline (32):** cloud enhance/split/consolidate, old Drive sync/rename/hygiene, Gmail/Calendar — phased; the ones the curated flow still needs (Drive auth/folder) stay.
- **Oncoteam storage (13):** `agent_state`, conversations, research, activity — **pending maintainer decision** on Oncoteam's end of life.
- Mechanism: description prefix `[DEPRECATED — use <tool>; removed after <date>]`, a per-tool usage counter, removal after 60 days without calls. Server instructions (`_MCP_INSTRUCTIONS`) updated to point at the new surface.

## 5. Phases

| Phase | Content | Gate |
|---|---|---|
| 0 | This design + decisions; LOINC notice | maintainer approval |
| 1 | Additive migrations: codings, provenance, devices, identifiers, conditions, cancer_stages, treatment tables, procedures, specimens, reports; Observation columns | snapshot; no data moved |
| 2 | LLM extractor (#714) emitting all kinds + generic verifier; shadow mode next to rule parsers | agreement metric per document |
| 3 | FHIR projection + read tools + IPS / EU-Lab export; HL7 validator in CI | validator clean on synthetic data |
| 4 | Context → facts migration tool (#619); `verify_fact`/`correct_fact`/`add_fact` (#636) | dry run + snapshot |
| 5 | Deprecations; client contracts; legacy tables frozen | 60 days of zero calls |

## 6. Open decisions for the maintainer

1. **Oncoteam** — end of life? (decides the 13 storage tools)
2. **Single LLM pass + verifier = `machine`?** (recommended: yes — same trust boundary as rules today; a second read gives `corroborated`)
3. **Internal storage** relational + FHIR projection (recommended) vs FHIR JSON as the native store.
4. **Deprecation window** 60 days (recommended).
5. **Regulatory**: legal opinion before 2029 on whether hosted Oncofiles is an "EHR system" under EHDS Art 2(2)(k) (self-declared conformity, CE, EU registration from 2031); do not market "interoperability with EHR systems" until conformant.
