"""Settings loaded from environment variables."""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent.parent.resolve()
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
DATABASE_PATH: Path = Path(os.environ.get("DATABASE_PATH", str(DATA_DIR / "oncofiles.db")))

# Google Drive (v0.2)
GOOGLE_DRIVE_FOLDER_ID: str = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
GOOGLE_CREDENTIALS_BASE64: str = os.environ.get("GOOGLE_CREDENTIALS_BASE64", "")
GOOGLE_APPLICATION_CREDENTIALS: str = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")

# Google OAuth 2.0 (v1.0)
GOOGLE_OAUTH_CLIENT_ID: str = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET: str = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI: str = os.environ.get(
    "GOOGLE_OAUTH_REDIRECT_URI", "https://oncofiles.com/oauth/callback"
)

# Turso (v0.4)
TURSO_DATABASE_URL: str = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN: str = os.environ.get("TURSO_AUTH_TOKEN", "")
TURSO_REPLICA_PATH: str = os.environ.get(
    "TURSO_REPLICA_PATH", ""
)  # local file for embedded replica

# Cloud transport (v0.4)
MCP_TRANSPORT: str = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST: str = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.environ.get("MCP_PORT", os.environ.get("PORT", "8000")))
MCP_BEARER_TOKEN: str = os.environ.get("MCP_BEARER_TOKEN", "")

# Fair Use Policy (FUP) — free tier document limit per patient
MAX_DOCUMENTS_PER_PATIENT: int = int(os.environ.get("MAX_DOCUMENTS_PER_PATIENT", "200"))

# Sync scheduler (v1.0)
SYNC_INTERVAL_MINUTES: int = int(os.environ.get("SYNC_INTERVAL_MINUTES", "5"))
SYNC_ENABLED: bool = os.environ.get("SYNC_ENABLED", "true").lower() in ("true", "1", "yes")

# Nightly-only AI pipeline (v5.10.0, #433)
# When AI_NIGHTLY_ONLY=true (default), LLM-touching jobs collapse into one
# CronTrigger at AI_NIGHT_WINDOW_START_UTC and only process new records
# (ai_processed_at IS NULL AND created_at > now - AI_REPROCESS_MAX_AGE_HOURS).
# Setting AI_NIGHTLY_ONLY=false restores the legacy 5-min interval + 4 nightly
# sweep jobs — rollback knob.
AI_NIGHTLY_ONLY: bool = os.environ.get("AI_NIGHTLY_ONLY", "true").lower() in (
    "true",
    "1",
    "yes",
)
AI_NIGHT_WINDOW_START_UTC: int = int(os.environ.get("AI_NIGHT_WINDOW_START_UTC", "23"))
AI_NIGHT_WINDOW_END_UTC: int = int(os.environ.get("AI_NIGHT_WINDOW_END_UTC", "3"))
DAILY_AI_DOC_CAP: int = int(os.environ.get("DAILY_AI_DOC_CAP", "20"))
AI_REPROCESS_MAX_AGE_HOURS: int = int(os.environ.get("AI_REPROCESS_MAX_AGE_HOURS", "36"))
ENABLE_INTEGRITY_CHECK: bool = os.environ.get("ENABLE_INTEGRITY_CHECK", "false").lower() in (
    "true",
    "1",
    "yes",
)

# Gmail + Calendar integration (#104)
GMAIL_ENABLED: bool = os.environ.get("GMAIL_ENABLED", "false").lower() in ("true", "1", "yes")
CALENDAR_ENABLED: bool = os.environ.get("CALENDAR_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)

# Oncoteam webhook (real-time document processing)
ONCOTEAM_WEBHOOK_URL: str = os.environ.get("ONCOTEAM_WEBHOOK_URL", "")
ONCOTEAM_WEBHOOK_TOKEN: str = os.environ.get("ONCOTEAM_WEBHOOK_TOKEN", "")

# GitHub bug reporting
GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO: str = os.environ.get("GITHUB_REPO", "peter-fusek/oncofiles")

# Patient context
PATIENT_CONTEXT_PATH: str = os.environ.get(
    "PATIENT_CONTEXT_PATH", str(DATA_DIR / "patient_context.json")
)

# Dashboard auth — comma-separated admin emails (see all patients)
# Non-admin users can still sign in but only see their own patients.
DASHBOARD_ADMIN_EMAILS: list[str] = [
    e.strip().lower()
    for e in os.environ.get(
        "DASHBOARD_ALLOWED_EMAILS",  # env var kept for backward compat
        "peterfusek1980@gmail.com,peter.fusek@instarea.sk",
    ).split(",")
    if e.strip()
]

# Localization
PREFERRED_LANG: str = os.environ.get("ONCOFILES_PREFERRED_LANG", "sk")

# Logging
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

# Version (single source of truth from pyproject.toml)
try:
    VERSION: str = importlib.metadata.version("oncofiles")
except importlib.metadata.PackageNotFoundError:
    VERSION = "0.0.0-dev"
