# erika-files-mcp

MCP server for persistent medical document management via Anthropic Files API.

## Quick start

```bash
uv sync --extra dev
uv run pytest
uv run ruff check
```

## Project structure

- `src/erika_files_mcp/` — main package
- `tests/` — pytest tests
- `migrations/` — SQL schema migrations
- `data/` — local dev SQLite database (gitignored)

## Conventions

- Python 3.12+, async-first
- FastMCP 3.0+ for MCP server
- Pydantic for data models
- ruff for linting/formatting
- Filenames follow YYYYMMDD convention: `YYYYMMDD_institution_category_description.ext`

## Key commands

- `uv run erika-mcp` — run MCP server (stdio)
- `uv run pytest` — run tests
- `uv run ruff check --fix` — lint and auto-fix
- `uv run ruff format` — format code
