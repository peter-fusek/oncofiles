# oncofiles

[![Tests](https://img.shields.io/badge/tests-458%20passed-brightgreen)]()
[![Python](https://img.shields.io/badge/python-3.12+-blue)]()
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)]()
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Patient-side MCP server for persistent medical document management. Connect your AI assistant to your health data.

## What is this?

Oncofiles is a self-hosted [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server that gives AI assistants persistent access to a patient's medical records. It handles document storage, lab value tracking, clinical research, and treatment event management.

**Key idea:** Your medical data stays on your infrastructure. AI assistants connect via MCP to read, search, and analyze your records — without your data leaving your control.

## Features

- **55 MCP tools** across 14 modules for comprehensive medical data management
- **17 document categories** — labs, pathology, imaging, genetics, surgery, prescriptions, and more
- **Lab value tracking** — store values, track trends, pre-cycle safety checks for chemo protocols
- **Google Drive sync** — bidirectional sync with automatic OCR companion files
- **Clinical research** — search PubMed and ClinicalTrials.gov, log research decisions
- **Treatment timeline** — track chemo cycles, surgeries, and other treatment events
- **Document versioning** — track document revisions with full history
- **Cross-references** — automatic linking of related documents (same visit, related dates)
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
git clone https://github.com/instarea-sk/oncofiles.git
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
├── server.py           # FastMCP server, auth, routes
├── database/           # Mixin-based DB layer (SQLite/Turso)
├── tools/              # 14 tool modules (55 tools)
│   ├── documents.py    # CRUD, search, view, versioning
│   ├── lab_trends.py   # Lab values, trends, safety checks
│   ├── clinical.py     # Treatment events, research log
│   ├── research.py     # PubMed, ClinicalTrials.gov search
│   └── ...
├── sync.py             # Bidirectional Google Drive sync
├── enhance.py          # AI metadata extraction (Haiku)
├── patient_context.py  # Patient clinical profile
└── models.py           # Pydantic models
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[MIT](LICENSE)
