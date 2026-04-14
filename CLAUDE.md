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
  - Category tokens: Labs, Report, Pathology, CT, USG, Genetics, Surgery, SurgicalReport, Prescription, Referral, Discharge, DischargeSummary, ChemoSheet, Reference, Advocate, Other, Vaccination, Dental, Preventive
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
- 687 tests, CI green

## Infrastructure gotchas

- `/health` is a **liveness probe only** — NO DB calls, NO blocking I/O (Railway kills process at 120s)
- `/readiness` is the DB connectivity check (5s timeout) — use for dashboards, not healthchecks
- `reconnect_if_stale(timeout=10.0)` — always pass a timeout; unbounded reconnect cascades up to 105s
- Turso Hrana streams expire during idle — DB keepalive job pings every 4 min to prevent stale connections
- `railway.toml`: healthcheckTimeout=120, overlapSeconds=30 — do not lower these
- **Turso single-connection**: NEVER use `asyncio.gather` for concurrent DB queries — serialize them. Dashboard fetches must also be sequential (no `Promise.all`). The single libsql connection blocks under concurrent access.
- **uv.lock**: After bumping version in pyproject.toml, always run `uv lock` — Railway uses `--locked` flag which rejects stale lockfiles
- **Dashboard i18n**: Uses `data-sk`/`data-en` attributes on elements. `applyDashLang()` queries all `[data-sk][data-en]` elements. Add both attributes when adding new user-visible text.
- **Multi-patient isolation**: ALL functions that use patient identity (get_patient_name, get_context, is_standard_format, rename_to_standard, parse_filename) MUST pass `patient_id`. The ContextVar fallback in `get_context()` catches missed callers during tool calls, but explicit is better. Never call `get_patient_name()` without patient_id in new code.
- **Open signup + patient scoping**: `DASHBOARD_ADMIN_EMAILS` (config.py) controls who sees all patients. Non-admin session users only see patients where `caregiver_email` matches. `_get_patient_id(required=True)` raises ValueError when no patient selected — use `required=False` only for bootstrapping tools (list_patients, select_patient).
- **Document limit**: `MAX_DOCUMENTS_PER_PATIENT` (200) enforced at `insert_document()` DB level. Sync has its own FUP check (`fup_reached` flag). Both must remain in sync.
- **Patient types**: `patient_type` in patient context — `"oncology"` (default) or `"general"`. Controls folder creation (oncology skips vaccination/dental/preventive; general skips chemo_sheet/pathology/genetics), lab thresholds (mFOLFOX6 vs EU/WHO general health), and preventive care screening.
- **Stateless HTTP**: `stateless_http=True` in `mcp.run()` — no server-side sessions. Survives Railway deploys. Do not change to stateful unless SSE push is needed.
- **Patient isolation (bearer tokens)**: `verify_token()` in `PersistentOAuthProvider` sets `_verified_patient_id` ContextVar. Middleware reads this — NEVER access `_session._access_token` (doesn't exist in FastMCP 3.1.1 stateless mode). Auth paths: `onco_*` patient tokens → specific patient; static `MCP_BEARER_TOKEN` → default patient; OAuth → default patient; stdio → default patient.
- **GDrive folder validation**: `gdrive_set_folder` (both MCP tool and REST API) validates the folder exists and is a folder before persisting. Prevents typos (e.g., `I` vs `l`) from causing cascading sync failures. If validation fails, the folder ID is NOT saved.
- **Folder 404 skip logic**: `_folder_404_counts` in `server.py` tracks consecutive 404 failures per patient. After 3 failures, sync skips that patient until `gdrive_set_folder` is called (which clears the counter). `/health` endpoint surfaces `folder_404_suspended` when patients are skipped.
- **Scheduler semaphore**: `_sync_semaphore = Semaphore(1)` — all DB-touching jobs serialized. Do not increase without fixing Turso single-connection constraint.
- **APScheduler + sys.exit**: NEVER use `sys.exit()` inside APScheduler jobs. Sync jobs run in a thread pool (SystemExit caught by executor), async jobs have SystemExit caught as job error. Use `os.kill(os.getpid(), signal.SIGTERM)` instead. See #244.
- **Graceful restart**: Single mechanism in `periodic_memory_check()` (async, every 5 min). Hard ceiling at 600 MB in `/health` as scheduler-independent failsafe. Threshold: `MEMORY_RESTART_THRESHOLD_MB` env var (default 420).
- **Turso row format**: `_TursoCursor.fetchall()` returns `list[dict]`, not index-accessible tuples. Always access rows by column name when writing raw DB queries (e.g., `query_db` tool).
- **Turso migrations**: NEVER use `ALTER TABLE RENAME` — Turso's Hrana protocol silently fails. Use `DROP TABLE IF EXISTS` + `CREATE TABLE` instead. Never INSERT patient-specific data in migrations (runs in test DBs). Use `WHERE EXISTS (SELECT 1 FROM patients WHERE ...)` guard if seeding data. `json_each()` works in SQLite/Turso for iterating JSON arrays in migrations (see 045).
- **Institution mapping**: `PROVIDER_TO_INSTITUTION` in `enhance.py` maps provider names to institution codes. Uses diacritic-insensitive matching. When adding new providers, also write a migration to backfill existing docs (see 044). General health providers added in v5.2.2.
- **Date validation**: `_safe_date()` in `_converters.py` handles invalid date strings gracefully (returns None + logs warning). `_safe_row_to_document()` wraps full row conversion so one bad row never crashes a batch. Write paths in `sync.py` and `backfill_document_fields()` validate dates with `date()` constructor before DB writes. Migration 043 NULLed all existing invalid dates. See #258.
- **Sync category protection**: `_sync_category_from_folder()` never downgrades an AI-validated category to "other" — GDrive folder detection only overrides when it detects a specific (non-"other") category. See #256.
