# EU AI Act — Per-Feature Risk Classification Matrix

**Status:** Pre-counsel worksheet. Not legal advice. Classifications below are
engineering best-guess pending review by an EU medical-device lawyer.
**Version:** Oncofiles v5.11.0-dev (commit `efdb8f0`, 2026-04-21)
**Deadline:** EU AI Act high-risk provisions apply **2026-08-02**. Any tool
classified High must carry the conformity assessment + CE marking
documentation by that date OR be de-risked (scope narrowed, disclaimers
strengthened) before that date.
**Jurisdiction in scope:** EU AI Act (Regulation 2024/1689) + Medical Device
Regulation (MDR 2017/745) interaction. EHDS not yet in force (2026).

---

## Framing questions for counsel

1. **Is Oncofiles "putting on the market" an AI system** under AI Act Art. 3(1)?
   - Yes — it's deployed, accessible via oncofiles.com and MCP connectors.
   - Open-source exemption (Art. 2(12)) only applies to non-commercial
     general-purpose components. Oncofiles is a specialized application, so
     MIT-license alone is not an exemption.

2. **Is the intended purpose "medical device" per MDR Art. 2(1)?**
   - MDR Art. 2(1)(a) covers "diagnosis, prevention, monitoring, prediction,
     prognosis, treatment or alleviation of disease."
   - Oncofiles's **explicit stance** (landing page, dashboard disclaimers,
     tool-level docstrings post #400): "This is not clinical decision support.
     It does not diagnose, treat, or replace physician judgment. Physician-
     enhancing framing only."
   - **Open question:** Does the AI-generated summary of a pathology report
     cross the monitoring/prognosis line, even with disclaimers? Counsel call.

3. **If not a medical device, does it still fall under AI Act Annex III
   high-risk categories?**
   - Annex III §5(a) covers AI systems intended to evaluate access to essential
     private services and benefits — does triaging labs → flagging urgent
     re-reads count? Probably no (the user is the patient themselves, not a
     gatekeeper), but counsel call.
   - Annex III §5(c) covers risk assessment and pricing of health insurance —
     N/A for us.
   - **Most probable outcome:** Limited-risk transparency obligations (Art. 52)
     — must disclose AI involvement to users (already done via footer + tool
     docstrings). No high-risk burden.

4. **Do we need to register as a provider or a deployer?**
   - Provider = develops and places on market (Art. 3(3)). That's Peter /
     instarea.sk.
   - Deployer = uses the system under its authority (Art. 3(4)). Each caregiver
     is technically a deployer when they run the MCP in their own AI chat.
   - **Counsel question:** Does self-hosting the MCP via MIT source code change
     any provider obligations? (Open-source exemption partial coverage.)

---

## Per-tool risk classification

### Legend

- **U** = Unacceptable (banned by Art. 5) — must not ship
- **H** = High (Annex III or medical device) — conformity assessment required
- **L** = Limited (Art. 52 transparency) — disclosure sufficient
- **M** = Minimal (no specific AI Act requirements)
- **?** = Ambiguous — counsel determination needed

### Document ingestion + management (low concern)

| Tool | Category | Intended Purpose | Best-guess risk | Rationale |
|------|----------|------------------|-----------------|-----------|
| `upload_document` | documents | File storage | M | Plain file upload, no AI |
| `get_document`, `get_document_by_id`, `list_documents`, `search_documents` | documents | Retrieval | M | Plain query |
| `delete_document`, `restore_document`, `list_trash` | documents | Soft-delete lifecycle | M | File ops |
| `get_document_group`, `get_document_versions` | documents | Metadata lookup | M | |
| `gdrive_*` (auth_url, sync, set_folder, sync_status, auth_status) | gdrive | Google Drive integration | M | Pass-through to Google |
| `gmail_*`, `calendar_*` | integrations | Google Gmail/Calendar | M | Pass-through |
| `export_document_package`, `export_manifest` | export | Bulk export | M | File packaging |
| `reconcile_gdrive`, `find_duplicates`, `unblock_stuck_documents` | hygiene | Admin hygiene | M | No user-facing inference |

### AI content generation on medical documents (higher concern — review)

| Tool | Category | Intended Purpose | Best-guess risk | Rationale |
|------|----------|------------------|-----------------|-----------|
| `enhance_documents` | enhance_tools | AI summary + tags | **L / ?** | Generates text summary of medical content. Limited-risk transparency obligation: disclose AI authorship. HIGH risk only if users rely on summary as diagnostic input — our framing explicitly rejects this. |
| `extract_document_metadata`, `extract_all_metadata` | enhance_tools | Structured field extraction | **L / ?** | Extracts diagnoses/medications/dates from text. User-facing flag that values may be AI-inferred already in place. Counsel check: does "extracted diagnosis" cross into prognosis? |
| `detect_and_split_documents`, `detect_and_consolidate_documents` | enhance_tools | Multi-doc PDF organization | L | AI classification, no clinical inference |
| `backfill_ai_classification`, `validate_categories` | hygiene | Category backfill | L | Metadata-only, no clinical output |
| `view_document` | documents | AI-OCR + text extraction | L | OCR is pass-through |

### Lab & clinical analysis (highest concern — DEFINITELY review)

| Tool | Category | Intended Purpose | Best-guess risk | Rationale |
|------|----------|------------------|-----------------|-----------|
| `analyze_labs`, `compare_labs`, `compare_lab_panels` | analysis | Lab trend analysis + flagging | **H / ?** | **Most likely high-risk.** Outputs trend interpretation, reference-range flagging, pre-cycle safety alerts — crosses monitoring/prognosis line. Mitigation in code: every output ends with "Questions for oncologist" block, explicit disclaimer. **Counsel must rule:** is the disclaimer sufficient to stay under MDR / is this a Class IIa medical device? |
| `get_lab_safety_check` | analysis | Pre-treatment lab gating | **H / ?** | Safety check = clinical decision support. Same concern as analyze_labs — even more direct. |
| `get_precycle_checklist` | analysis | Pre-chemo checklist | **H / ?** | Same concern. |
| `get_lab_trends`, `get_lab_trends_by_parameter`, `get_lab_time_series`, `get_lab_summary` | lab_trends | Lab value retrieval + trending | **L / H?** | Pure numeric data retrieval is minimal; trend computation with reference ranges *might* be monitoring. |
| `store_lab_values` | lab_trends | Lab value persistence | M | Storage only |
| `get_patient_context`, `update_patient_context` | patient | Clinical state read/write | **L / ?** | Pure CRUD on patient-written data. Risk only if AI auto-populates it (some paths do via `enhance.py`). Counsel: does that auto-population cross into diagnosis? |

### Clinical records (new in #450 Phase 1 — same profile as patient_context)

| Tool | Category | Intended Purpose | Best-guess risk | Rationale |
|------|----------|------------------|-----------------|-----------|
| `add_clinical_record`, `get_clinical_record` | clinical_records | Clinical fact CRUD | **L / ?** | Same profile as `patient_context`. Pure CRUD when source='manual' or 'mcp-claude'; risk appears only when `source='ai-extract'` automates it. |
| `add_clinical_record_note`, `list_clinical_record_notes` | clinical_records | Free-form annotation | M | User-authored text with tags |

### Research + trials (informational)

| Tool | Category | Intended Purpose | Best-guess risk | Rationale |
|------|----------|------------------|-----------------|-----------|
| `fetch_clinical_trials` | clinical | Passthrough to CT.gov | M | No AI inference, public data |
| `search_clinical_trials*`, `check_trial_eligibility` (Oncoteam-side) | — | Eligibility screening | **H / ?** | Eligibility screening for a specific patient could be high-risk if the user treats it as definitive. Currently Oncoteam-side, not Oncofiles. |
| `search_pubmed`, `fetch_pubmed_article` (Oncoteam-side) | — | Literature search | M | Passthrough |
| `get_clinical_protocol` (Oncoteam-side) | — | Protocol lookup | **L** | Cited literature, user interprets |

### Admin / observability (no patient-facing inference)

| Tool | Category | Intended Purpose | Best-guess risk | Rationale |
|------|----------|------------------|-----------------|-----------|
| `get_pipeline_status`, `get_document_status_matrix` | hygiene | Ops visibility | M | |
| `audit_document_pipeline`, `audit_patient_isolation` | hygiene | Admin audit | M | |
| `integration_status`, `system_health` | integrations | Ops monitoring | M | |
| `search_prompt_log`, `get_prompt_log_entry` | prompt_log | AI-call observability | M | Internal telemetry |
| `log_conversation`, `get_conversation`, `search_conversations` | conversations | Chat transcript store | M | |
| `set_agent_state`, `get_agent_state`, `list_agent_states` | agent_state | MCP-agent KV | M | |
| `add_activity_log`, `get_activity_stats`, `search_activity_log` | activity | Activity log | M | |
| `add_research_entry`, `list_research_entries`, `search_research` | research | Research bookmarks | M | |
| `add_treatment_event`, `list_treatment_events`, etc. | treatment | Treatment log | M | User-authored data |
| `qa_analysis`, `query_db` | db_query | Admin queries | M | |
| `select_patient`, `list_patients` | patient | Patient switching | M | |
| `list_tool_definitions` | — | MCP introspection | M | |
| `setup_gdrive`, `gdrive_fix_permissions`, `gdrive_auth_callback`, `gdrive_auth_enable` | gdrive | Auth bootstrap | M | |
| `rename_documents_to_standard`, `reassign_document`, `update_document_category` | naming/documents | Rename ops | M | Deterministic rules + AI-assist hybrid |

---

## Summary: ~5 tools likely need the closest counsel review

1. **`analyze_labs` / `compare_labs` / `compare_lab_panels`** — lab trend
   interpretation. The "Questions for oncologist" disclaimer block is the
   strongest mitigation we have. Counsel: is it sufficient?
2. **`get_lab_safety_check`** — explicitly named "safety". Strongest
   high-risk signal in the name alone.
3. **`get_precycle_checklist`** — pre-treatment gating logic.
4. **`enhance_documents` (summary + tags)** — medical document summary. L or H.
5. **`extract_document_metadata`** — structured biomarker/diagnosis extraction
   from free-text reports.

Everything else is minimal or limited risk based on best-guess. Counsel should
validate this clustering, not redo the whole classification.

---

## Risk-mitigation options by priority

### Tier 1 — already shipped (v5.8.0)

- Physician-enhancing framing on landing + dashboard + footer (#400)
- Disclaimers on `analyze_labs`, `get_lab_safety_check`, `get_precycle_checklist`,
  `get_patient_context`
- "Questions for oncologist" block ends every lab analysis
- Source attribution: every claim links back to a specific source document

### Tier 2 — readily available if counsel asks for more

- Add an interstitial modal on first use: "Oncofiles is not a medical device.
  It does not replace your oncologist. Continue? [y/n]"
- Rename `get_lab_safety_check` → `get_lab_summary_for_oncologist_review` (less
  safety-critical framing in the tool name itself — the AI Act reads the
  "intended purpose" partly from the tool's named purpose)
- Per-output "confidence" score from the AI, surfaced to user

### Tier 3 — if counsel classifies anything H

- Conformity assessment per Annex VI (internal control) — feasible for a
  solo project
- Post-market monitoring plan (Art. 72) — already have prompt_log
- Transparency for users (Art. 13) — already have it
- Registration in the EU AI database (Art. 49) — admin action only
- CE marking coordination (MDR Art. 10 if medical device) — would be **big**
  lift; candidates for descoping out of the tool entirely

---

## Counsel engagement plan

### Budget target
€3-5k (per the competitive-research note in #444 / sprint plan).

### Counsel profile
- EU medical-device lawyer, preferably based in DE / AT / SK / CZ for EU-side
  practice familiarity
- Prior work on Class I or IIa medical device software classification
- Comfortable with open-source + SaaS delivery model

### First-meeting materials (pack to send after counsel selected)
1. This risk matrix (1 page of executive summary + full table)
2. `memory/project_cost_budget.md` (clarifies commercial model)
3. Landing page + dashboard screenshots showing current disclaimers
4. Tool docstrings for the 5 flagged tools above
5. Sample `analyze_labs` output (anonymized) showing the full disclaimer chain

### Deliverable from counsel
A 3-5 page memo:
- Classification per tool (confirming or adjusting this matrix)
- Concrete disclaimer/framing adjustments per Tier 2 if needed
- List of documents/actions required by 2026-08-02
- Risk of any specific decisions (e.g. renaming `get_lab_safety_check`)
- Quote for conformity-assessment support if anything lands in H

### Candidate firms (to be identified — Peter to shortlist 3)
- 1 in Germany (AI Act + MDR strong)
- 1 in Austria or Slovakia (national EHR context)
- 1 EU tech-practice boutique

Not yet started. Target: shortlist 3 by end of v5.12, engage by 2026-05-30 to
leave buffer before the August deadline.

---

## Change log

- 2026-04-21 v5.11 initial draft (Peter's engineering best-guess across 87 tools)
- NEXT: counsel review, then incorporate their classification
