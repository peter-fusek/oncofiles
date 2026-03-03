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

# Turso (v0.4)
TURSO_DATABASE_URL: str = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN: str = os.environ.get("TURSO_AUTH_TOKEN", "")
