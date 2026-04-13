# oncofiles

[![Tests](https://img.shields.io/badge/tests-607%20passed-brightgreen)]()
[![Python](https://img.shields.io/badge/python-3.12+-blue)]()
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)]()
[![Claude](https://img.shields.io/badge/Claude-MCP-cc785c)](https://claude.ai)
[![ChatGPT](https://img.shields.io/badge/ChatGPT-MCP-10a37f)](https://chatgpt.com)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Your medical records are scattered across Gmail, Google Drive, and Calendar. Oncofiles reads everything, organizes it, and makes it available through AI — so you can ask about your health naturally in Claude or ChatGPT.

**[oncofiles.com](https://oncofiles.com)** | [Demo Dashboard](https://oncofiles.com/demo) | [Privacy Policy](https://oncofiles.com/privacy)

## What is this?

Oncofiles is an [MCP server](https://modelcontextprotocol.io/) built for cancer patients and their caregivers. It connects to your Google Drive, Gmail, and Calendar, reads all your medical documents (lab results, CT scans, pathology reports, prescriptions), and makes them accessible through any AI chat — Claude, ChatGPT, or any MCP client.

**Built from real need:** Created by a caregiver managing his wife's cancer treatment. Hundreds of documents, dozens of doctors, constantly changing lab results. Oncofiles organizes the chaos so you can focus on treatment, not on finding papers.

**Sister project:** [Oncoteam](https://oncofiles.com/oncoteam) — an AI agent that analyzes your Oncofiles data: tracks lab trends, searches clinical trials, and prepares questions for your oncologist.

**Your data, your control:** Everything stays in your Google Drive. No third-party data processing. Disconnect anytime — your files are always yours.

## Multi-Patient Support (v4.0+)

Oncofiles supports multiple patients on a single instance. Each patient gets:

- **Full data isolation** — documents, labs, treatment events, emails, and calendar entries are scoped to the patient
- **Separate bearer tokens** — each patient (or caregiver) gets their own `onco_*` token
- **Per-patient OAuth** — Google Drive, Gmail, and Calendar are authorized per patient
- **Dashboard patient selector** — switch between patients in the web dashboard

### Onboarding a new patient

1. **Dashboard wizard:** Open the dashboard, click "+ New Patient", and follow the 4-step wizard
2. **API:** `POST /api/patients` with `{patient_id, display_name}` — returns a bearer token
3. **Connect GDrive:** Visit `/oauth/authorize/drive?patient_id=X` to authorize document sync
4. **Trigger first sync:** `POST /api/sync-trigger` with `{patient_id}` to import documents

## Features

- **76 MCP tools** across 14 modules for comprehensive medical data management
- **14 document categories** — labs, pathology, imaging, genetics, surgery, consultation, prescriptions, and more
- **Lab value tracking** — store values, track trends, pre-cycle safety checks for chemo protocols
- **Google Drive sync** — bidirectional sync with automatic OCR companion files
- **Gmail & Calendar scanning** — medical emails and appointments are auto-detected and classified
- **Clinical research** — search PubMed and ClinicalTrials.gov, log research decisions
- **Treatment timeline** — track chemo cycles, surgeries, and other treatment events
- **Document versioning** — track document revisions with full history
- **AI-powered metadata** — automatic summaries, tags, and structured data extraction
- **Audit logging** — every tool call is logged for accountability

## Architecture

```
┌─────────────────┐     MCP Protocol      ┌──────────────┐
│  Claude / GPT   │◄────────────────────►  │  Oncofiles   │
│  (AI Assistant)  │    streamable-http     │  MCP Server  │
└─────────────────┘                        └──────┬───────┘
                                                  │
                                    ┌─────────────┼─────────────┐
                                    │             │             │
                              ┌─────▼─────┐ ┌────▼────┐ ┌─────▼─────┐
                              │  SQLite /  │ │ Google  │ │ Anthropic │
                              │   Turso    │ │  Drive  │ │ Files API │
                              └───────────┘ └─────────┘ └───────────┘
```

**Stack:** Python 3.12+ · FastMCP 3.1 · Pydantic · SQLite/Turso · Railway

## Quick start

```bash
# Clone and install
git clone https://github.com/peter-fusek/oncofiles.git
cd oncofiles
uv sync --extra dev

# Run locally (stdio mode for Claude Desktop)
uv run oncofiles-mcp

# Run tests
uv run pytest

# Lint
uv run ruff check
```

### Environment variables

```bash
# Required
DATABASE_PATH=data/oncofiles.db

# Optional — cloud database
TURSO_DATABASE_URL=libsql://...
TURSO_AUTH_TOKEN=...

# Optional — Google Drive sync
GOOGLE_DRIVE_FOLDER_ID=...
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...

# Optional — remote access
MCP_TRANSPORT=streamable-http  # default: stdio
MCP_HOST=0.0.0.0
MCP_PORT=8080
MCP_BEARER_TOKEN=...
```

### Connect to Claude Desktop

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "oncofiles": {
      "command": "uv",
      "args": ["run", "oncofiles-mcp"],
      "cwd": "/path/to/oncofiles"
    }
  }
}
```

### Connect to ChatGPT

Oncofiles works with ChatGPT's MCP integration (Developer Mode). Point ChatGPT to your instance's `/mcp` endpoint with a bearer token.

### Deploy to Railway

The included `Dockerfile` is ready for Railway deployment:

1. Push to GitHub
2. Connect repo in Railway
3. Set environment variables
4. Railway auto-deploys on push

Live instance: [oncofiles.com](https://oncofiles.com)

## Project structure

```
src/oncofiles/
├── server.py           # FastMCP server, auth, routes, scheduler
├── database/           # Mixin-based DB layer (SQLite/Turso)
├── tools/              # 14 tool modules (76 tools)
│   ├── documents.py    # CRUD, search, view, versioning
│   ├── lab_trends.py   # Lab values, trends, safety checks
│   ├── clinical.py     # Treatment events, research log
│   ├── research.py     # PubMed, ClinicalTrials.gov search
│   └── ...
├── sync.py             # Bidirectional Google Drive sync
├── gmail_sync.py       # Medical email detection and import
├── calendar_sync.py    # Calendar event classification
├── enhance.py          # AI metadata extraction (Haiku)
├── patient_middleware.py # Per-patient token → context resolution
├── patient_context.py  # Patient clinical profile
└── models.py           # Pydantic models
```

## MCP Registry

Listed on the [MCP Registry](https://registry.modelcontextprotocol.io/) as `io.github.peter-fusek/oncofiles`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[MIT](LICENSE)

---

## Who's Behind This

### The People

<table>
<tr>
<td width="50%">

**Peter Fusek** — CEO & Founder

Serial entrepreneur and AI strategist. 4 years at Tatra banka. Co-founded marketlocator (exit to Deutsche Telekom). Advisor to VÚB Bank CEO. 18+ years building technology products.

Built Oncofiles from personal need — managing his wife's cancer treatment across hundreds of documents, dozens of doctors, and constantly changing lab results.

[LinkedIn](https://www.linkedin.com/in/peterfusek/) · peter.fusek@instarea.com

</td>
<td width="50%">

**Peter Čapkovič** — CTO & Co-founder

Senior IT architect with 20+ years in enterprise banking (VÚB). Expert in .NET, Python, SQL, and systems architecture. Led architecture across all Instarea products.

Architecture, development, operations — everything under one roof.

[LinkedIn](https://www.linkedin.com/in/peter-capkovic/)

</td>
</tr>
</table>

### The Company

**[Instarea](https://www.instarea.com)** — 18 years, 23 products shipped. From telecom analytics and enterprise clients (Callinspector) to mobile-first fintech (InventButton), big data exits (marketlocator → Deutsche Telekom), IoT platforms, and AI-first products (PulseShape, ReplicaCity, HomeGrif).

Oncofiles and [Oncoteam](https://github.com/peter-fusek/oncoteam) are Instarea's latest — built with the same engineering discipline that delivered enterprise-grade products for banking, telecom, and data industries.

10+ verified, synchronized team members available across front-end, back-end, integration, data science, UX/UI, marketing, and cloud ops.

> *"We take it personally, with our own faces, like family and at work."*

Built by [Instarea](https://www.instarea.com) | Aligned with [EHDS](https://health.ec.europa.eu/ehealth-digital-health-and-care/european-health-data-space_en) principles
