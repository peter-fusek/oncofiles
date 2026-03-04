"""Settings loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent.parent.resolve()
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
DATABASE_PATH: Path = Path(os.environ.get("DATABASE_PATH", str(DATA_DIR / "erika_files.db")))

# Google Drive (v0.2)
GOOGLE_DRIVE_FOLDER_ID: str = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
GOOGLE_CREDENTIALS_BASE64: str = os.environ.get("GOOGLE_CREDENTIALS_BASE64", "")
GOOGLE_APPLICATION_CREDENTIALS: str = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")

# Turso (v0.4)
TURSO_DATABASE_URL: str = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN: str = os.environ.get("TURSO_AUTH_TOKEN", "")

# Cloud transport (v0.4)
MCP_TRANSPORT: str = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST: str = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.environ.get("MCP_PORT", os.environ.get("PORT", "8000")))
MCP_BEARER_TOKEN: str = os.environ.get("MCP_BEARER_TOKEN", "")
