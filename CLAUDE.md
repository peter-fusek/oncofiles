# oncofiles

AI-powered medical document management for cancer patients and caregivers. Connects Google Drive, Gmail, and Calendar to Claude, ChatGPT, and any MCP client.

## Quick start

```bash
uv sync --extra dev
uv run pytest
uv run ruff check
```

## Project structure

- `src/oncofiles/` — main package
- `tests/` — pytest tests
- `migrations/` — SQL schema migrations
- `data/` — local dev SQLite database (gitignored)

## Conventions

- Python 3.12+, async-first
- FastMCP 3.0+ for MCP server
- Pydantic for data models
- ruff for linting/formatting
- Filenames follow standard convention: `YYYYMMDD_PatientName_Institution_Category_DescriptionEN.ext`
  - Separators: underscores only
  - Description: English, CamelCase, max 60 chars
  - Category tokens: Labs, Report, Pathology, CT, USG, Genetics, Surgery, SurgicalReport, Prescription, Referral, Discharge, DischargeSummary, ChemoSheet, Reference, Advocate, Other
  - Legacy formats (space+dash, underscore-separated) still parsed for backward compat

## Key commands

- `uv run oncofiles-mcp` — run MCP server (stdio)
- `MCP_BEARER_TOKEN=test MCP_TRANSPORT=streamable-http uv run oncofiles-mcp` — run HTTP server locally
- `uv run pytest` — run tests
- `uv run ruff check --fix` — lint and auto-fix
- `uv run ruff format` — format code

## Deployment

- **Railway**: `oncofiles.com` (streamable-http at /mcp)
- Push to `main` auto-deploys via Railway
- 607 tests, CI green

## Infrastructure gotchas

- `/health` is a **liveness probe only** — NO DB calls, NO blocking I/O (Railway kills process at 120s)
- `/readiness` is the DB connectivity check (5s timeout) — use for dashboards, not healthchecks
- `reconnect_if_stale(timeout=10.0)` — always pass a timeout; unbounded reconnect cascades up to 105s
- Turso Hrana streams expire during idle — DB keepalive job pings every 4 min to prevent stale connections
- `railway.toml`: healthcheckTimeout=120, overlapSeconds=30 — do not lower these
- **Turso single-connection**: NEVER use `asyncio.gather` for concurrent DB queries — serialize them. Dashboard fetches must also be sequential (no `Promise.all`). The single libsql connection blocks under concurrent access.
- **uv.lock**: After bumping version in pyproject.toml, always run `uv lock` — Railway uses `--locked` flag which rejects stale lockfiles
- **Dashboard i18n**: Uses `data-sk`/`data-en` attributes on elements. `applyDashLang()` queries all `[data-sk][data-en]` elements. Add both attributes when adding new user-visible text.
