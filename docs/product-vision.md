# Oncofiles — Product Vision

## What

MCP server bridging a patient's medical documents on Google Drive with AI chat tools (Claude, ChatGPT) for cancer treatment management.

## Core Value

1. **GDrive = single source of truth**, MCP = the bridge
2. **Bidirectional sync** with structure preservation (categories, bilingual metadata, OCR)
3. **Write-back**: findings, decisions, lab values stored from chat sessions
4. **Medical data security** (GDPR Article 9, RBAC, audit logging)
5. **Oncoteam**: AI agents for research, treatment tracking, clinical suggestions
6. **Multi-role access**: patient, advocate, doctor via portal + notifications

## Architecture

```
Google Drive (documents, images, OCR)
        ↕ bidirectional sync
   Oncofiles MCP Server
   ├── SQLite/Turso (metadata, labs, conversations)
   ├── Anthropic Files API (AI summaries, metadata extraction)
   ├── APScheduler (sync, cleanup, metadata jobs)
   └── OAuth2 + bearer token auth
        ↕ MCP protocol
   AI Chat Tools (Claude, ChatGPT)
   └── Oncoteam agents (lab analysis, PubMed, ClinicalTrials.gov)
```

## Current State (v3.16.0)

- 55 MCP tools across 14 modules, 17 document categories
- 90 active documents, 9 treatment events, 43 conversation entries
- Standard filename convention: `YYYYMMDD_Patient_Inst_Cat_DescEN.ext`
- Bidirectional GDrive sync with OCR companion files
- AI summaries + structured metadata extraction (Claude Haiku)
- Lab values storage with trend analysis and pre-cycle safety checks
- Source attribution and document cross-references
- Custom domain: oncofiles.com
- Deployed on Railway (streamable-http)

## Roadmap

### Phase 0 — Stability (v3.7.0) ✅
- Fix Railway deploy to show latest version
- Idempotent migration runner (no crash on already-migrated DBs)
- Docker Compose for one-command local setup
- Product documentation and `.env.example`

### Phase 1 — Single-Patient Hardening (v3.8–3.9)
- Configurable `PATIENT_CONTEXT` (name, diagnosis, biomarkers via env/config)
- Comprehensive audit logging (who accessed what, when)
- GDrive setup wizard (guided folder structure creation)
- ChatGPT MCP compatibility testing and fixes
- Document versioning (track re-uploads of same document)

### Phase 2 — Multi-User (v4.x)
- `patient_id` on all tables (documents, labs, conversations, events)
- Per-patient GDrive folder isolation
- Multi-patient Oncoteam agents

> **Note**: User management, RBAC, consent management, and all UX/UI are planned for a future frontend layer — not in Oncofiles. Oncofiles stays a headless data/MCP layer.

### Phase 3 — Product Layer (v5.x)
- Integration with hospital information systems (HL7 FHIR)
- Advanced document versioning and dedup

> **Note**: Patient portal, WhatsApp notifications, clinician dashboard, and all user-facing features are planned for a future frontend layer.

## Technical Stack

- **Language**: Python 3.12+, async-first
- **Framework**: FastMCP 3.1+
- **Database**: SQLite (local) / Turso (cloud)
- **AI**: Anthropic Claude (Haiku for extraction, Opus/Sonnet via chat)
- **Storage**: Google Drive + Anthropic Files API
- **Deployment**: Railway (Docker, streamable-http)
- **Testing**: pytest, 335+ tests
