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
    "GOOGLE_OAUTH_REDIRECT_URI", "https://aware-kindness-production.up.railway.app/oauth/callback"
)

# Turso (v0.4)
TURSO_DATABASE_URL: str = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN: str = os.environ.get("TURSO_AUTH_TOKEN", "")

# Cloud transport (v0.4)
MCP_TRANSPORT: str = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST: str = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.environ.get("MCP_PORT", os.environ.get("PORT", "8000")))
MCP_BEARER_TOKEN: str = os.environ.get("MCP_BEARER_TOKEN", "")

# Sync scheduler (v1.0)
SYNC_INTERVAL_MINUTES: int = int(os.environ.get("SYNC_INTERVAL_MINUTES", "30"))
SYNC_ENABLED: bool = os.environ.get("SYNC_ENABLED", "true").lower() in ("true", "1", "yes")

# Logging
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

# Version (single source of truth from pyproject.toml)
try:
    VERSION: str = importlib.metadata.version("oncofiles")
except importlib.metadata.PackageNotFoundError:
    VERSION = "0.0.0-dev"
