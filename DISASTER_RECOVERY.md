# Disaster Recovery & Backup Plan

**Status**: DRAFT — tracked in oncofiles#425. Not yet implemented.
**Last revised**: 2026-04-18
**Owner**: Peter Fusek (peter.fusek@instarea.sk)

## 1. Scope

Oncofiles holds medical data for EU/SK cancer patients. Data loss would destroy months of AI-derived structured metadata, treatment event history, lab trends, and biomarker tracking. RTO/RPO targets:

| Target | Value | Rationale |
|---|---|---|
| RTO (Recovery Time Objective) | 4 h | Single-operator team; tolerable overnight |
| RPO (Recovery Point Objective) | 24 h daily / 1 h for critical | Daily dumps acceptable; prompt_log + structured_metadata benefit from hourly |

## 2. Data inventory

| Asset | Primary store | Replicated? | Irreplaceable? |
|---|---|---|---|
| Turso DB (documents, ocr_text, treatment_events, prompt_log, oauth_tokens, agent_state, patient_tokens) | Turso cloud | Embedded replica on Railway Volume (read) | YES — AI-derived metadata is expensive to recompute |
| Raw PDFs / images | Google Drive (user-owned) + `files` table in Turso | Duplicated in Turso | NO — user still owns GDrive copy |
| Source code | GitHub `peter-fusek/oncofiles` | Local clones | NO (GitHub-protected) |
| GitHub issues / plans | GitHub | None | Semi (export via `gh api`) |
| OAuth tokens (Google Drive/Gmail/Cal) | Turso | Embedded replica | NO — users can re-auth |
| Railway env vars (MCP_BEARER_TOKEN, Turso URL + token, Anthropic key) | Railway secrets | None | NO — can regenerate |
| GCP OAuth app (`oncofiles-490809`) | GCP | None | Semi — reconfigurable |
| Memory `/Users/.../memory/` | Local FS | Git (sometimes) | Semi |

## 3. Backup target: GCS (EU region)

- **Bucket**: `oncofiles-backups-eu` in `europe-west3` (Frankfurt — EU data sovereignty for SK patients)
- **Encryption**: CMEK via Cloud KMS key `projects/oncofiles-490809/locations/europe-west3/keyRings/oncofiles/cryptoKeys/backup-key`
- **Versioning**: enabled
- **Retention policy (lifecycle)**:
  - 0-30 days: Standard
  - 30-90 days: Nearline
  - 90-365 days: Coldline
  - 365-730 days: Archive
  - ≥730 days: delete
- **Object lock (compliance)**: 30 days (prevents accidental + malicious delete within window)
- **Service account**: `oncofiles-backup@oncofiles-490809.iam.gserviceaccount.com` with `roles/storage.objectCreator` only (no delete). Separate admin SA for break-glass restore.
- **Estimated cost**: <$2/mo at current data volume (~100 MB daily compressed)

## 4. Daily backup job — `02:00 UTC`

Runs as Railway scheduled service (separate from main MCP process to avoid resource contention).

Artifacts produced per run under `gs://oncofiles-backups-eu/{YYYY}/{MM}/{DD}/`:

| File | Source | Why |
|---|---|---|
| `turso_oncofiles.sql.gz` | `turso db shell oncofiles ".dump" \| gzip` | Full relational schema + data |
| `turso_schema.sql` | `turso db shell oncofiles ".schema"` | Schema-only for fast diff review |
| `memory_snapshot.tar.gz` | `~/.claude/.../memory/` tar | Session context + decisions |
| `railway_env.json.enc` | `railway variables` → KMS-encrypted | Env reconstruction |
| `issues_oncofiles.json` | `gh issue list --json …` | Issue state snapshot |
| `issues_oncoteam.json` | `gh issue list --repo .../oncoteam --json …` | Ditto |
| `manifest.json` | generated | sha256 of each above + timestamps |

Post-run:
1. Compute `sha256sum` of each artifact → compare to previous day (detect corruption)
2. Upload `backup_success` metric to Railway healthcheck or `/readiness`
3. Alert (email/Slack) if any artifact missing or sha256 collision suggests unchanged DB (Turso idle)

## 5. Hourly incremental — WAL shipping (Phase 2)

For critical tables (`prompt_log`, `structured_metadata`, `treatment_events`):

- Turso supports `turso db shell oncofiles ".wal-checkpoint"` and WAL export
- Ship WAL delta every hour to `gs://.../hourly/{YYYY-MM-DD-HH}.wal.gz`
- Aggregate into nightly full dump at 02:00 UTC
- RPO drops from 24h → 1h for these tables

## 6. Weekly restore test — `Sunday 03:00 UTC`

1. Spin up empty Turso DB `oncofiles-dr-test`
2. Restore yesterday's `turso_oncofiles.sql.gz` into it
3. Smoke-test queries:
   - `SELECT COUNT(*) FROM patients WHERE is_active=1` → expect 5
   - `SELECT patient_id, COUNT(*) FROM documents WHERE deleted_at IS NULL GROUP BY patient_id` → expect matches prod ±1% sync drift
   - `SELECT COUNT(*) FROM prompt_log WHERE created_at > date('now', '-1 day')` → expect > 0
4. Post result to oncofiles `/readiness.jobs.restore_test.last_ok`
5. Tear down the test DB
6. Alert if any smoke test fails

## 7. Disaster scenarios + procedures

### Scenario A: Turso DB corruption / data loss

1. Stop `_run_sync` + `_db_keepalive` jobs in Railway (`railway run`)
2. Create fresh Turso DB: `turso db create oncofiles-restore`
3. Download latest dump: `gsutil cp gs://oncofiles-backups-eu/$(date +%Y/%m/%d)/turso_oncofiles.sql.gz .`
4. Restore: `gunzip -c turso_oncofiles.sql.gz | turso db shell oncofiles-restore`
5. Update Railway env: `railway variables set TURSO_DATABASE_URL=libsql://oncofiles-restore-...turso.io`
6. Redeploy: `railway up`
7. Verify `/readiness` returns OK + doc counts match
8. Rename `oncofiles-restore` → `oncofiles` once verified; delete old corrupted DB after 7 days

**Expected RTO**: 30 min

### Scenario B: Railway project deleted / account suspended

1. Provision replacement host (Fly.io / DigitalOcean / Render)
2. Clone repo + deploy: `railway link` (or new platform equivalent)
3. Restore env vars from `gs://.../railway_env.json.enc` (decrypt with KMS)
4. Point to existing Turso DB (assuming Turso still up)
5. Update `oncofiles.com` DNS to new host
6. Verify `/health`

**Expected RTO**: 2 h

### Scenario C: Turso account suspended (total data loss + no Turso)

1. Install `libsql-server` in container (it's open source)
2. Deploy libsql on Railway Volume (or Fly volume): `docker run libsql/sqld`
3. Restore from GCS dump into libsql
4. Update `TURSO_DATABASE_URL` to point to libsql container
5. Redeploy app
6. Lose: multi-region replication + embedded replica Railway integration (acceptable for recovery)

**Expected RTO**: 4 h

### Scenario D: Total cloud loss (Turso + Railway + GCP)

1. Clone oncofiles source locally
2. Run local stdio MCP mode with local SQLite (`MCP_TRANSPORT=stdio uv run oncofiles-mcp`)
3. Restore from GCS mirror (if GCS also gone, from weekly off-site mirror — see Phase 3)
4. Communicate downtime to users via oncofiles.com static page (Cloudflare Pages fallback)

**Expected RTO**: 1 business day

## 8. Phase 3 — multi-cloud mirror

Weekly mirror from GCS to:
- **Cloudflare R2** (S3-compatible, no egress fees, separate billing/identity)
- Optional: encrypted tarball to a personal offline drive for black-swan scenarios

This guards against the GCP account itself being suspended.

## 9. Legal / compliance

- **GDPR Art. 32**: encryption at rest (CMEK), encryption in transit (HTTPS to GCS), access logs on KMS key
- **Art. 17 (right to erasure)**: documented individual-patient purge procedure (delete rows in live DB + purge all GCS objects older than 30 days referring to that patient — requires grep of dump content, manual verification)
- **Medical retention**: SK MZ SR standard is 10 years for medical records. We back up 2 years in GCS; patient's own GDrive holds the originals. Longer retention = user's own GDrive responsibility.
- **Data residency**: all backups in `europe-west3` (Frankfurt). Never use US regions.

## 10. Implementation checklist

See oncofiles#425 for live issue tracking.

- [ ] GCS bucket `oncofiles-backups-eu` created in `europe-west3`
- [ ] KMS key ring + key created
- [ ] Lifecycle policy + retention lock applied
- [ ] Service account + IAM bindings
- [ ] `scripts/backup_to_gcs.py` implemented
- [ ] Railway cron service deployed (cron job or separate 1-RU container)
- [ ] First successful daily backup verified
- [ ] Weekly restore test implemented
- [ ] `/readiness` job telemetry exposed
- [ ] Email/Slack alert wired
- [ ] Runbook drill performed (recover into staging DB)
- [ ] Hourly WAL shipping (Phase 2)
- [ ] R2 mirror (Phase 3)

## 11. Changelog

- 2026-04-18: Initial draft filed as oncofiles#425 (triggered by live triage session + user request for "materializacia / persistent / backup / disaster recovery").
