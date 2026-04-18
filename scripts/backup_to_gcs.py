"""Daily backup of oncofiles Turso DB + ops state into GCS (EU region).

Runs as a Railway cron service at 02:00 UTC. Produces under
`gs://oncofiles-backups-eu/{YYYY}/{MM}/{DD}/`:

    {TS}_turso_oncofiles.sql.gz   Full DB dump (all medical metadata, biomarkers,
                                  treatment events, OCR text, prompt log, file refs)
    {TS}_turso_schema.sql         Schema-only, for fast diff review
    {TS}_railway_env.json         Filtered env vars (secrets in plain JSON, protected
                                  by bucket-level CMEK + IAM — see DR plan §9)
    {TS}_issues_oncofiles.json    GitHub issues snapshot (state, labels, bodies)
    {TS}_prs_oncofiles.json       GitHub PRs snapshot (open + recently-closed)
    {TS}_manifest.json            sha256 per artifact + build metadata

Authentication on Railway:
    GOOGLE_APPLICATION_CREDENTIALS_JSON — JSON SA key (oncofiles-backup SA)
    TURSO_REPLICA_PATH                  — /data/oncofiles.db (default)
    BACKUP_BUCKET                       — oncofiles-backups-eu (default)
    BACKUP_KMS_KEY                      — projects/.../cryptoKeys/backup-key (required)
    BACKUP_GITHUB_TOKEN                 — PAT with public_repo scope (optional;
                                          if absent, GitHub snapshot is skipped with a warning)
    BACKUP_GITHUB_REPO                  — peter-fusek/oncofiles (default)
    ONCOFILES_VERSION                   — optional, stamped into manifest

Env var export rules: include keys that would be painful to reconstruct
(Turso, Anthropic, Google OAuth, MCP_BEARER_TOKEN, admin emails). Exclude
RAILWAY_* ephemeral metadata (regenerated on each deploy). Exclude the backup
SA JSON itself (chicken-and-egg — the admin SA holds the break-glass key).

On success, exit 0. Partial failures (e.g. missing GITHUB_TOKEN) log WARN but
still exit 0 — we want the DB dump to succeed even if auxiliary artifacts fail.
Any failure that would leave the DB un-backed-up exits non-zero so Railway
reports a failed run.

DR plan: oncofiles#425. Runbook: DISASTER_RECOVERY.md.
Oncoteam parallel DR strategy: oncoteam#NEW (same template, separate bucket).
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


_ENV_INCLUDE_PREFIXES = (
    "TURSO_",
    "ANTHROPIC_",
    "GOOGLE_",
    "OAUTH_",
    "MCP_",
    "DASHBOARD_",
    "ENHANCEMENT_",
    "SYNC_",
    "MEMORY_",
)
_ENV_INCLUDE_EXACT = {
    "PORT",
    "ONCOFILES_VERSION",
    "SENTRY_DSN",
}
_ENV_EXCLUDE_PREFIXES = (
    "RAILWAY_",  # ephemeral per-deploy metadata
    "PATH",
    "HOME",
    "PWD",
    "HOSTNAME",
)
# Hard exclude — these are the backup path itself, so dumping them creates a loop
_ENV_EXCLUDE_EXACT = {
    "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    "BACKUP_KMS_KEY",
    "BACKUP_BUCKET",
    "BACKUP_GITHUB_TOKEN",
}


def _dump_railway_env(out_path: Path) -> int:
    """Filter + dump selected env vars to JSON. Returns number of keys exported."""
    selected = {}
    for key, value in os.environ.items():
        if key in _ENV_EXCLUDE_EXACT:
            continue
        if any(key.startswith(p) for p in _ENV_EXCLUDE_PREFIXES):
            continue
        if key in _ENV_INCLUDE_EXACT or any(key.startswith(p) for p in _ENV_INCLUDE_PREFIXES):
            selected[key] = value
    out_path.write_text(json.dumps(selected, indent=2, sort_keys=True))
    return len(selected)


def _snapshot_github(repo: str, token: str, out_issues: Path, out_prs: Path) -> dict:
    """Snapshot issues + PRs via REST API. Returns per-artifact counts."""
    import urllib.request

    def _fetch_all(endpoint: str, state: str) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            url = (
                f"https://api.github.com/repos/{repo}/{endpoint}"
                f"?state={state}&per_page=100&page={page}"
            )
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "oncofiles-backup",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                batch = json.loads(resp.read().decode("utf-8"))
            if not batch:
                break
            results.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return results

    # /issues endpoint returns BOTH issues and PRs — filter out PRs for the issues file
    raw_issues = _fetch_all("issues", "all")
    issues = [i for i in raw_issues if "pull_request" not in i]
    prs = _fetch_all("pulls", "all")

    out_issues.write_text(json.dumps(issues, indent=2))
    out_prs.write_text(json.dumps(prs, indent=2))
    return {"issues": len(issues), "prs": len(prs)}


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

    github_token = os.environ.get("BACKUP_GITHUB_TOKEN", "")
    github_repo = _env("BACKUP_GITHUB_REPO", "peter-fusek/oncofiles")

    with tempfile.TemporaryDirectory(prefix="oncofiles-backup-") as tmpdir:
        tmp = Path(tmpdir)
        artifacts: list[Path] = []

        # ── 1. Full gzipped DB dump ──
        dump_path = tmp / "turso_oncofiles.sql.gz"
        log.info("dumping replica → %s", dump_path.name)
        dump_size = _dump_sqlite(replica_path, dump_path)
        manifest["artifacts"]["turso_oncofiles.sql.gz"] = {
            "uncompressed_bytes": dump_size,
            "compressed_bytes": dump_path.stat().st_size,
            "sha256": _sha256(dump_path),
        }
        artifacts.append(dump_path)

        # ── 2. Schema-only dump (plain text, small) ──
        schema_path = tmp / "turso_schema.sql"
        _dump_schema(replica_path, schema_path)
        manifest["artifacts"]["turso_schema.sql"] = {
            "bytes": schema_path.stat().st_size,
            "sha256": _sha256(schema_path),
        }
        artifacts.append(schema_path)

        # ── 3. Railway env snapshot (filtered — see _ENV_INCLUDE/_EXCLUDE) ──
        env_path = tmp / "railway_env.json"
        try:
            env_key_count = _dump_railway_env(env_path)
            manifest["artifacts"]["railway_env.json"] = {
                "bytes": env_path.stat().st_size,
                "sha256": _sha256(env_path),
                "key_count": env_key_count,
            }
            artifacts.append(env_path)
            log.info("env snapshot: %d keys", env_key_count)
        except Exception as e:  # noqa: BLE001
            log.warning("env snapshot failed, skipping: %s", e)
            manifest["artifacts"]["railway_env.json"] = {"skipped": str(e)}

        # ── 4. GitHub issues + PRs snapshot (skip gracefully if no token) ──
        if github_token:
            try:
                issues_path = tmp / "issues_oncofiles.json"
                prs_path = tmp / "prs_oncofiles.json"
                counts = _snapshot_github(github_repo, github_token, issues_path, prs_path)
                manifest["artifacts"]["issues_oncofiles.json"] = {
                    "bytes": issues_path.stat().st_size,
                    "sha256": _sha256(issues_path),
                    "issue_count": counts["issues"],
                }
                manifest["artifacts"]["prs_oncofiles.json"] = {
                    "bytes": prs_path.stat().st_size,
                    "sha256": _sha256(prs_path),
                    "pr_count": counts["prs"],
                }
                artifacts.extend([issues_path, prs_path])
                log.info("github snapshot: %d issues, %d prs", counts["issues"], counts["prs"])
            except Exception as e:  # noqa: BLE001
                log.warning("github snapshot failed, skipping: %s", e)
                manifest["artifacts"]["github_snapshot"] = {"skipped": str(e)}
        else:
            log.warning("BACKUP_GITHUB_TOKEN not set — github snapshot skipped")
            manifest["artifacts"]["github_snapshot"] = {"skipped": "no token"}

        # ── 5. Manifest last — it references everything above ──
        manifest_path = tmp / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        artifacts.append(manifest_path)

        # ── 6. Upload all artifacts ──
        urls = []
        for artifact_path in artifacts:
            blob_name = f"{date_prefix}/{timestamp}_{artifact_path.name}"
            url = _upload(artifact_path, bucket_name, blob_name, kms_key, credentials_info)
            log.info("uploaded %s", url)
            urls.append(url)

    log.info("backup complete — %d artifacts", len(urls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
