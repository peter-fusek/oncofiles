"""Daily backup of oncofiles Turso DB + ops state into GCS (EU region).

Runs as a Railway cron service at 02:00 UTC. Reads the embedded libsql replica on
the Railway Volume, dumps all tables to a gzipped SQL file, uploads under
`gs://oncofiles-backups-eu/{YYYY}/{MM}/{DD}/turso_oncofiles.sql.gz` with CMEK,
and emits a sha256 manifest next to it.

Authentication on Railway:
    GOOGLE_APPLICATION_CREDENTIALS_JSON — JSON service-account key (oncofiles-backup SA)
    TURSO_REPLICA_PATH                  — /data/oncofiles.db (default)
    BACKUP_BUCKET                       — oncofiles-backups-eu (default)
    BACKUP_KMS_KEY                      — projects/.../cryptoKeys/backup-key (required for CMEK)
    ONCOFILES_VERSION                   — optional, stamped into manifest

On success, exit 0. On any error, exit non-zero so Railway reports a failed run
(which you'll want to alert on via Railway's notification hooks — see
DISASTER_RECOVERY.md §4).

DR plan: oncofiles#425. Runbook: DISASTER_RECOVERY.md.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [backup] %(message)s",
)
log = logging.getLogger(__name__)


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"missing required env var: {name}")
    return value or ""


def _iso_path(now: datetime) -> str:
    return now.strftime("%Y/%m/%d")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dump_sqlite(replica_path: Path, out_path: Path) -> int:
    """Stream-dump the libsql embedded replica to a gzipped SQL file.

    Uses sqlite3.Connection.iterdump() — reads directly from the on-disk replica,
    which is maintained by the main MCP process via `db.sync_replica()` every
    4 minutes. Staleness tolerance: up to 4 min worth of writes. Acceptable for
    RPO=24h Phase 1.
    """
    if not replica_path.exists():
        raise SystemExit(f"replica not found: {replica_path}")

    bytes_written = 0
    # Open read-only to avoid interfering with embedded replica writes
    uri = f"file:{replica_path}?mode=ro"
    with (
        sqlite3.connect(uri, uri=True) as conn,
        gzip.open(out_path, "wt", encoding="utf-8", compresslevel=6) as gz,
    ):
        for line in conn.iterdump():
            gz.write(line + "\n")
            bytes_written += len(line) + 1
    return bytes_written


def _dump_schema(replica_path: Path, out_path: Path) -> None:
    """Schema-only dump for fast diff review (plain text, not gzipped)."""
    uri = f"file:{replica_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn, out_path.open("w", encoding="utf-8") as f:
        cur = conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
        )
        for (stmt,) in cur.fetchall():
            f.write(stmt + ";\n")


def _upload(
    local: Path,
    bucket_name: str,
    blob_name: str,
    kms_key: str,
    credentials_info: dict,
) -> str:
    """Upload one file to GCS with CMEK. Returns gs:// URL."""
    from google.cloud import storage  # lazy import
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(credentials_info)
    client = storage.Client(credentials=creds, project=credentials_info.get("project_id"))
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name, kms_key_name=kms_key)
    blob.upload_from_filename(str(local))
    return f"gs://{bucket_name}/{blob_name}"


def main() -> int:
    now = datetime.now(UTC)
    log.info("backup run started at %s", now.isoformat())

    replica_path = Path(_env("TURSO_REPLICA_PATH", "/data/oncofiles.db"))
    bucket_name = _env("BACKUP_BUCKET", "oncofiles-backups-eu")
    kms_key = _env("BACKUP_KMS_KEY", required=True)
    creds_json = _env("GOOGLE_APPLICATION_CREDENTIALS_JSON", required=True)
    oncofiles_version = _env("ONCOFILES_VERSION", "unknown")

    try:
        credentials_info = json.loads(creds_json)
    except json.JSONDecodeError as e:
        raise SystemExit(f"GOOGLE_APPLICATION_CREDENTIALS_JSON is not valid JSON: {e}") from e

    date_prefix = _iso_path(now)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")

    manifest: dict = {
        "created_at": now.isoformat(),
        "oncofiles_version": oncofiles_version,
        "replica_path": str(replica_path),
        "replica_size_bytes": replica_path.stat().st_size if replica_path.exists() else 0,
        "artifacts": {},
    }

    with tempfile.TemporaryDirectory(prefix="oncofiles-backup-") as tmpdir:
        tmp = Path(tmpdir)

        # ── 1. Full gzipped dump ──
        dump_path = tmp / "turso_oncofiles.sql.gz"
        log.info("dumping replica → %s", dump_path.name)
        dump_size = _dump_sqlite(replica_path, dump_path)
        manifest["artifacts"]["turso_oncofiles.sql.gz"] = {
            "uncompressed_bytes": dump_size,
            "compressed_bytes": dump_path.stat().st_size,
            "sha256": _sha256(dump_path),
        }

        # ── 2. Schema-only dump (plain text, small) ──
        schema_path = tmp / "turso_schema.sql"
        _dump_schema(replica_path, schema_path)
        manifest["artifacts"]["turso_schema.sql"] = {
            "bytes": schema_path.stat().st_size,
            "sha256": _sha256(schema_path),
        }

        # ── 3. Write manifest ──
        manifest_path = tmp / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        # ── 4. Upload all artifacts ──
        urls = []
        for artifact_path in (dump_path, schema_path, manifest_path):
            blob_name = f"{date_prefix}/{timestamp}_{artifact_path.name}"
            url = _upload(artifact_path, bucket_name, blob_name, kms_key, credentials_info)
            log.info("uploaded %s", url)
            urls.append(url)

    log.info("backup complete — %d artifacts", len(urls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
