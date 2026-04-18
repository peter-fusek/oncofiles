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

Runs as Railway scheduled cron service (separate container from the main MCP process — avoids the #426 memory-leak interaction).

### 4a. Railway-side script — `scripts/backup_to_gcs.py`
Artifacts produced per run under `gs://oncofiles-backups-eu/{YYYY}/{MM}/{DD}/`:

| File | Source | Why |
|---|---|---|
| `{TS}_turso_oncofiles.sql.gz` | `sqlite3.iterdump()` over embedded replica `/data/oncofiles.db`, gzipped | Full DB: patients, documents + AI metadata, ocr_text, treatment_events, patient_context (biomarkers + home_region + excluded_therapies), oauth_tokens, prompt_log, document_groups |
| `{TS}_turso_schema.sql` | `SELECT sql FROM sqlite_master` | Schema-only for fast diff review |
| `{TS}_railway_env.json` | Filtered `os.environ` — includes `TURSO_*`, `ANTHROPIC_*`, `GOOGLE_*`, `OAUTH_*`, `MCP_*`, `DASHBOARD_*`. Excludes `RAILWAY_*` ephemeral + `GOOGLE_APPLICATION_CREDENTIALS_JSON` (break-glass path, stored separately) | Service reconstruction — painful to regenerate manually (Anthropic key rotation, Turso token, etc.) |
| `{TS}_issues_oncofiles.json` | GitHub REST API via `urllib.request` (no `gh` CLI dep on Railway) | Issue state + bodies — context for "why did we do X?" |
| `{TS}_prs_oncofiles.json` | Same, `/pulls` endpoint | PR state snapshot |
| `{TS}_manifest.json` | Generated | sha256 of each above + sizes + timestamps + oncofiles version |

All artifacts written with CMEK via bucket-level encryption (writer SA only holds `cryptoKeyEncrypter` — cannot decrypt anything it uploaded; only the admin SA has `cryptoKeyDecrypter`).

### 4b. Local-side script — `scripts/backup_memory_local.sh`
macOS-specific — lives in the operator's personal memory folder which Railway can't reach:

| File | Source | Why |
|---|---|---|
| `memory/{YYYY}/{MM}/{DD}/{TS}_memory_snapshot.tar.gz` | `~/.claude/projects/-Users-.../memory/` tar | Accumulated cross-session context (user feedback, project memory, verification protocols, clinical references) |

Runs via `com.oncofiles.memory-backup.plist` launchd agent at 02:30 local. Uses `gcloud` ADC (Application Default Credentials) so no SA key sits on the laptop filesystem — auth comes from `gcloud auth application-default login`.

### Post-run checks
1. Compute `sha256` of each artifact (embedded in manifest.json) → next-day backup compares to detect corruption or zero-change pathological state
2. Expose `jobs.backup.last_ok` on `/readiness`
3. Alert (email/Slack) via Railway notification hook on any non-zero exit
4. Partial failures (missing `BACKUP_GITHUB_TOKEN`, GitHub API rate limit) log WARN but don't fail the run — DB dump success is the must-have

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

See oncofiles#425 for live issue tracking. Provisioning is via `scripts/provision_backup_infra.sh` — idempotent gcloud/gsutil script, no Terraform (swapped 2026-04-18 for simplicity — single-operator, ~10 resources, rarely changes).

### Phase 1 — Daily backup (this sprint)
- [ ] `bash scripts/provision_backup_infra.sh` — creates bucket + KMS + SAs
- [ ] Writer SA key extracted + set on Railway (`GOOGLE_APPLICATION_CREDENTIALS_JSON`, `BACKUP_BUCKET`, `BACKUP_KMS_KEY`, `BACKUP_GITHUB_TOKEN`, `BACKUP_GITHUB_REPO`)
- [ ] Railway backup cron service deployed (per `railway.toml` comments)
- [ ] First manual run verified (object in GCS with CMEK, sha256 matches)
- [ ] `jobs.backup.last_ok` on `/readiness`
- [ ] Email/Slack alert on non-zero exit
- [ ] Retention policy LOCKED via `gsutil retention lock gs://oncofiles-backups-eu` (IRREVERSIBLE — only after first-run verified)

### Phase 1b — Local memory backup (Mac launchd)
- [ ] `gcloud auth application-default login` (one-time, for ADC)
- [ ] `cp scripts/com.oncofiles.memory-backup.plist ~/Library/LaunchAgents/`
- [ ] `launchctl load ~/Library/LaunchAgents/com.oncofiles.memory-backup.plist`
- [ ] First manual run verified (`bash scripts/backup_memory_local.sh`)

### Phase 2 — Restore testing + hourly WAL
- [ ] `scripts/restore_from_gcs.py` runnable with admin SA
- [ ] Weekly restore test script + cron
- [ ] Hourly WAL shipping for `prompt_log`, `structured_metadata`, `treatment_events`

### Phase 3 — Multi-cloud + compliance docs
- [ ] R2 mirror (weekly)
- [ ] GDPR Art. 17 individual-purge procedure tested
- [ ] Annual DR drill calendar event

## 11. Related plans — Oncoteam

Oncoteam runs in a separate Railway project with its own Turso DB and has independent state (research decisions, session notes, agent_state, clinical trial cache). Oncoteam must run its **own** parallel DR plan — this template is reusable 1:1, just with different names:

- Bucket: `oncoteam-backups-eu`
- KMS key: either same `oncofiles` key ring (shared) or separate `oncoteam` ring (stronger isolation — recommended)
- Writer SA: `oncoteam-backup@…`
- Same RTO/RPO targets, same lifecycle, same scenarios

**Tracked for oncoteam agents in oncoteam#NEW** — includes the full action checklist so another agent can pick up the work without re-reading this entire doc.

## 12. Changelog

- 2026-04-18: Initial draft filed as oncofiles#425 (triggered by live triage session + user request for "materializacia / persistent / backup / disaster recovery").
- 2026-04-18: Extended backup scope — Railway env, GitHub issues+PRs snapshot, separate local memory backup via launchd. Filed oncoteam parallel DR issue for their agents to mirror.
- 2026-04-18: Swapped Terraform → idempotent shell script (`scripts/provision_backup_infra.sh`). Terraform overhead (state bucket, providers) wasn't justified for ~10 resources under single-operator ownership.
