"""Restore oncofiles Turso DB from the latest GCS backup dump.

Used for disaster recovery (see DISASTER_RECOVERY.md §7 Scenario A) and for the
weekly automated restore test.

Usage (dry-run — just lists candidates):
    BACKUP_BUCKET=oncofiles-backups-eu \
    GOOGLE_APPLICATION_CREDENTIALS_JSON="$(cat admin-sa.json)" \
    uv run python scripts/restore_from_gcs.py --list

Usage (download + restore to a fresh SQLite file for smoke testing):
    uv run python scripts/restore_from_gcs.py \
        --date 2026/04/18 \
        --out /tmp/oncofiles-restore.db

Usage (restore into a Turso DB — requires Turso CLI + auth):
    uv run python scripts/restore_from_gcs.py \
        --date 2026/04/18 \
        --target-turso-db oncofiles-restore

The admin SA (oncofiles-backup-admin) is required for download — the writer SA
used by the daily cron cannot read. Key NEVER goes on Railway.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [restore] %(message)s",
)
log = logging.getLogger(__name__)


def _creds() -> dict:
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if not raw:
        raise SystemExit("GOOGLE_APPLICATION_CREDENTIALS_JSON not set (use admin SA for restore)")
    return json.loads(raw)


def _gcs_client():
    from google.cloud import storage
    from google.oauth2 import service_account

    info = _creds()
    creds = service_account.Credentials.from_service_account_info(info)
    return storage.Client(credentials=creds, project=info.get("project_id")), info


def list_backups(bucket_name: str) -> None:
    client, _ = _gcs_client()
    bucket = client.bucket(bucket_name)
    blobs = sorted(
        (b for b in bucket.list_blobs(prefix="") if b.name.endswith("turso_oncofiles.sql.gz")),
        key=lambda b: b.time_created,
        reverse=True,
    )
    log.info("%d dumps available:", len(blobs))
    for b in blobs[:30]:
        log.info("  %s  (%s bytes, %s)", b.name, b.size, b.time_created.isoformat())


def download_and_restore(bucket_name: str, date_prefix: str, out_path: Path) -> None:
    client, _ = _gcs_client()
    bucket = client.bucket(bucket_name)
    candidates = [b for b in bucket.list_blobs(prefix=date_prefix) if b.name.endswith(".sql.gz")]
    if not candidates:
        raise SystemExit(f"no dumps found under {date_prefix}")
    blob = max(candidates, key=lambda b: b.time_created)
    log.info("restoring %s (%d bytes)", blob.name, blob.size)

    with tempfile.NamedTemporaryFile(suffix=".sql.gz", delete=False) as tmp:
        blob.download_to_filename(tmp.name)
        gz_path = Path(tmp.name)

    if out_path.exists():
        raise SystemExit(f"refusing to overwrite existing file: {out_path}")

    with sqlite3.connect(out_path) as conn, gzip.open(gz_path, "rt", encoding="utf-8") as f:
        conn.executescript(f.read())
        conn.commit()
    gz_path.unlink()
    log.info("restore complete → %s (%d bytes)", out_path, out_path.stat().st_size)


def restore_to_turso(bucket_name: str, date_prefix: str, turso_db: str) -> None:
    """Restore into a fresh Turso DB. Requires `turso` CLI on PATH + auth."""
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        download_and_restore(bucket_name, date_prefix, Path(tmp.name))
        log.info("created Turso DB %s …", turso_db)
        subprocess.run(["turso", "db", "create", turso_db], check=True)
        log.info("piping dump into %s …", turso_db)
        with gzip.open(tmp.name + ".sql.gz", "rb") as _:
            pass  # the SQL was already applied locally; now replay against Turso
        # Simple approach: re-dump from local restored DB into Turso
        subprocess.run(
            f"sqlite3 {tmp.name} .dump | turso db shell {turso_db}",
            shell=True,  # noqa: S602 (operator-controlled input only)
            check=True,
        )
    log.info("Turso restore complete → %s", turso_db)


def smoke_test(db_path: Path) -> bool:
    """Minimal sanity queries. Returns True if all pass."""
    ok = True
    with sqlite3.connect(db_path) as conn:
        active_patients = conn.execute(
            "SELECT COUNT(*) FROM patients WHERE is_active=1"
        ).fetchone()[0]
        log.info("active patients: %d", active_patients)
        if active_patients < 1:
            log.error("smoke test FAILED: no active patients")
            ok = False

        rows = conn.execute(
            "SELECT patient_id, COUNT(*) FROM documents "
            "WHERE deleted_at IS NULL GROUP BY patient_id"
        ).fetchall()
        log.info("doc counts per patient: %s", rows)
        if not rows:
            log.error("smoke test FAILED: no documents")
            ok = False

        recent_prompts = conn.execute(
            "SELECT COUNT(*) FROM prompt_log WHERE created_at > date('now', '-1 day')"
        ).fetchone()[0]
        log.info("recent prompt_log entries (last 24h): %d", recent_prompts)
        # This can legitimately be 0 on idle days — warn, don't fail
        if recent_prompts == 0:
            log.warning("prompt_log idle for 24h — verify this matches expected traffic")

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bucket", default=os.environ.get("BACKUP_BUCKET", "oncofiles-backups-eu"))
    parser.add_argument("--list", action="store_true", help="list recent dumps")
    parser.add_argument("--date", help="date prefix YYYY/MM/DD")
    parser.add_argument("--out", type=Path, help="local SQLite path for restore")
    parser.add_argument("--smoke-test", action="store_true", help="run smoke queries after restore")
    parser.add_argument("--target-turso-db", help="restore into a fresh Turso DB by name")
    args = parser.parse_args()

    if args.list:
        list_backups(args.bucket)
        return 0

    if not args.date:
        raise SystemExit("--date YYYY/MM/DD required")

    if args.target_turso_db:
        restore_to_turso(args.bucket, args.date, args.target_turso_db)
        return 0

    if not args.out:
        raise SystemExit("--out PATH required for local restore")

    download_and_restore(args.bucket, args.date, args.out)
    if args.smoke_test and not smoke_test(args.out):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
