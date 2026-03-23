# oncofiles

[![Tests](https://img.shields.io/badge/tests-607%20passed-brightgreen)]()
[![Python](https://img.shields.io/badge/python-3.12+-blue)]()
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)]()
[![ChatGPT](https://img.shields.io/badge/ChatGPT-MCP-10a37f)](https://chatgpt.com)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Your medical records are scattered across Gmail, Google Drive, and Calendar. Oncofiles reads everything, organizes it, and makes it available through AI — so you can ask about your health naturally in Claude or ChatGPT.

## What is this?

Oncofiles is an [MCP server](https://modelcontextprotocol.io/) built for cancer patients and their caregivers. It connects to your Google Drive, Gmail, and Calendar, reads all your medical documents (lab results, CT scans, pathology reports, prescriptions), and makes them accessible through any AI chat — Claude, ChatGPT, or any MCP client.

**Built from real need:** Created by a caregiver managing his wife's cancer treatment. Hundreds of documents, dozens of doctors, constantly changing lab results. Oncofiles organizes the chaos so you can focus on treatment, not on finding papers.

**Sister project:** [Oncoteam](https://github.com/peter-fusek/oncoteam) — an AI agent that analyzes your Oncofiles data: tracks lab trends, searches clinical trials, and prepares questions for your oncologist.

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
- **15 document categories** — labs, pathology, imaging, genetics, surgery, prescriptions, and more
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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[MIT](LICENSE)
