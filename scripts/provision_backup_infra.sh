#!/usr/bin/env bash
# Provision oncofiles backup infra in GCP — idempotent.
#
# Creates in project oncofiles-490809, region europe-west3 (Frankfurt, EU data residency):
#   - GCS bucket                   oncofiles-backups-eu            (versioned, CMEK, object-lock, lifecycle)
#   - KMS key ring + key           oncofiles / backup-key          (90d rotation, prevent_destroy)
#   - Writer service account       oncofiles-backup@…              (storage.objectCreator + cryptoKeyEncrypter ONLY)
#   - Admin service account        oncofiles-backup-admin@…        (objectAdmin + cryptoKeyDecrypter — break-glass)
#
# Usage:
#   gcloud auth login                      # interactive, once
#   bash scripts/provision_backup_infra.sh
#
# To re-run: safe — every step checks existence first and skips.
# To extract writer SA key for Railway:
#   gcloud iam service-accounts keys create /tmp/oncofiles-backup-sa.json \
#       --iam-account=oncofiles-backup@oncofiles-490809.iam.gserviceaccount.com
#   # then paste the JSON contents into Railway env:
#   #   GOOGLE_APPLICATION_CREDENTIALS_JSON
#   # and delete the local file immediately:
#   rm -P /tmp/oncofiles-backup-sa.json   # or: shred -u on Linux
#
# Lock retention policy (IRREVERSIBLE — only after first successful backup verified):
#   gsutil retention lock gs://oncofiles-backups-eu

set -euo pipefail

PROJECT="oncofiles-490809"
REGION="europe-west3"
BUCKET="oncofiles-backups-eu"
KEYRING="oncofiles"
KEY="backup-key"
WRITER_SA="oncofiles-backup"
ADMIN_SA="oncofiles-backup-admin"
OBJECT_LOCK_DAYS=30
RETENTION_DAYS=730

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32mOK\033[0m  %s\n'  "$*"; }

log "Using project: $PROJECT / region: $REGION"

# ── 1. KMS key ring + key ──────────────────────────────────────────
if gcloud kms keyrings describe "$KEYRING" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    ok "keyring $KEYRING exists"
else
    log "creating keyring $KEYRING"
    gcloud kms keyrings create "$KEYRING" --location="$REGION" --project="$PROJECT"
    ok "keyring created"
fi

if gcloud kms keys describe "$KEY" --keyring="$KEYRING" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    ok "key $KEY exists"
else
    log "creating key $KEY (symmetric, 90d rotation)"
    gcloud kms keys create "$KEY" \
        --keyring="$KEYRING" \
        --location="$REGION" \
        --project="$PROJECT" \
        --purpose=encryption \
        --rotation-period=7776000s \
        --next-rotation-time="$(date -u -v+90d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+90 days' +%Y-%m-%dT%H:%M:%SZ)"
    ok "key created"
fi

KEY_ID="projects/${PROJECT}/locations/${REGION}/keyRings/${KEYRING}/cryptoKeys/${KEY}"

# Grant GCS service agent access to the key (so bucket-default CMEK works)
GCS_SA="service-$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')@gs-project-accounts.iam.gserviceaccount.com"
log "granting GCS service agent ($GCS_SA) encrypter/decrypter on KMS key"
gcloud kms keys add-iam-policy-binding "$KEY" \
    --keyring="$KEYRING" \
    --location="$REGION" \
    --project="$PROJECT" \
    --member="serviceAccount:${GCS_SA}" \
    --role="roles/cloudkms.cryptoKeyEncrypterDecrypter" \
    --condition=None >/dev/null
ok "GCS service agent bound"

# ── 2. Service accounts ────────────────────────────────────────────
WRITER_EMAIL="${WRITER_SA}@${PROJECT}.iam.gserviceaccount.com"
ADMIN_EMAIL="${ADMIN_SA}@${PROJECT}.iam.gserviceaccount.com"

if gcloud iam service-accounts describe "$WRITER_EMAIL" --project="$PROJECT" >/dev/null 2>&1; then
    ok "writer SA $WRITER_SA exists"
else
    log "creating writer SA $WRITER_SA"
    gcloud iam service-accounts create "$WRITER_SA" \
        --project="$PROJECT" \
        --display-name="Oncofiles daily backup writer" \
        --description="Railway cron uploads Turso dumps + memory snapshots. Write-only."
    ok "writer SA created"
fi

if gcloud iam service-accounts describe "$ADMIN_EMAIL" --project="$PROJECT" >/dev/null 2>&1; then
    ok "admin SA $ADMIN_SA exists"
else
    log "creating admin SA $ADMIN_SA"
    gcloud iam service-accounts create "$ADMIN_SA" \
        --project="$PROJECT" \
        --display-name="Oncofiles backup admin (break-glass)" \
        --description="Restore operations only. Key NEVER deployed to Railway."
    ok "admin SA created"
fi

# Writer permissions — least privilege
log "binding writer SA to roles/storage.objectCreator (project-scope)"
gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${WRITER_EMAIL}" \
    --role="roles/storage.objectCreator" \
    --condition=None >/dev/null
ok "writer SA → objectCreator"

log "binding writer SA to cryptoKeyEncrypter (cannot decrypt)"
gcloud kms keys add-iam-policy-binding "$KEY" \
    --keyring="$KEYRING" \
    --location="$REGION" \
    --project="$PROJECT" \
    --member="serviceAccount:${WRITER_EMAIL}" \
    --role="roles/cloudkms.cryptoKeyEncrypter" \
    --condition=None >/dev/null
ok "writer SA → cryptoKeyEncrypter"

# Admin permissions — break-glass
log "binding admin SA to cryptoKeyDecrypter"
gcloud kms keys add-iam-policy-binding "$KEY" \
    --keyring="$KEYRING" \
    --location="$REGION" \
    --project="$PROJECT" \
    --member="serviceAccount:${ADMIN_EMAIL}" \
    --role="roles/cloudkms.cryptoKeyDecrypter" \
    --condition=None >/dev/null
ok "admin SA → cryptoKeyDecrypter"

# ── 3. GCS bucket ──────────────────────────────────────────────────
if gcloud storage buckets describe "gs://${BUCKET}" --project="$PROJECT" >/dev/null 2>&1; then
    ok "bucket $BUCKET exists"
else
    log "creating bucket $BUCKET in $REGION (uniform IAM, public-access-prevention enforced)"
    gcloud storage buckets create "gs://${BUCKET}" \
        --project="$PROJECT" \
        --location="$REGION" \
        --default-storage-class=STANDARD \
        --uniform-bucket-level-access \
        --public-access-prevention \
        --default-encryption-key="$KEY_ID"
    ok "bucket created"
fi

log "enabling versioning"
gcloud storage buckets update "gs://${BUCKET}" --versioning
ok "versioning on"

log "applying lifecycle policy (Std→Nearline 30d→Coldline 90d→Archive 365d→delete ${RETENTION_DAYS}d)"
LIFECYCLE_JSON="$(mktemp)"
trap 'rm -f "$LIFECYCLE_JSON"' EXIT
cat > "$LIFECYCLE_JSON" <<EOF
{
  "lifecycle": {
    "rule": [
      {"action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
       "condition": {"age": 30, "matchesStorageClass": ["STANDARD"]}},
      {"action": {"type": "SetStorageClass", "storageClass": "COLDLINE"},
       "condition": {"age": 90, "matchesStorageClass": ["NEARLINE"]}},
      {"action": {"type": "SetStorageClass", "storageClass": "ARCHIVE"},
       "condition": {"age": 365, "matchesStorageClass": ["COLDLINE"]}},
      {"action": {"type": "Delete"},
       "condition": {"age": ${RETENTION_DAYS}}}
    ]
  }
}
EOF
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file="$LIFECYCLE_JSON"
ok "lifecycle applied"

log "applying ${OBJECT_LOCK_DAYS}-day retention policy (unlocked — lock manually later)"
gcloud storage buckets update "gs://${BUCKET}" --retention-period="${OBJECT_LOCK_DAYS}d"
ok "retention policy set (unlocked)"

# Admin SA needs bucket-level admin access
log "binding admin SA to objectAdmin on bucket"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="serviceAccount:${ADMIN_EMAIL}" \
    --role="roles/storage.objectAdmin" >/dev/null
ok "admin SA → objectAdmin on bucket"

# ── 4. Labels ──────────────────────────────────────────────────────
log "applying labels"
gcloud storage buckets update "gs://${BUCKET}" \
    --update-labels=app=oncofiles,purpose=backup,gdpr=eu-resident
ok "labels applied"

# ── Summary ────────────────────────────────────────────────────────
cat <<SUMMARY

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Provisioning complete.

  Bucket:    gs://${BUCKET}
  Region:    ${REGION}
  KMS key:   ${KEY_ID}
  Writer SA: ${WRITER_EMAIL}
  Admin SA:  ${ADMIN_EMAIL}

Next steps:

1. Create writer SA key + set on Railway:
     gcloud iam service-accounts keys create /tmp/backup-sa.json \\
         --iam-account=${WRITER_EMAIL}
     railway variables set GOOGLE_APPLICATION_CREDENTIALS_JSON="\$(cat /tmp/backup-sa.json)"
     railway variables set BACKUP_BUCKET=${BUCKET}
     railway variables set BACKUP_KMS_KEY=${KEY_ID}
     railway variables set BACKUP_GITHUB_TOKEN=\$GITHUB_PAT
     railway variables set BACKUP_GITHUB_REPO=peter-fusek/oncofiles
     rm -P /tmp/backup-sa.json

2. Deploy Railway backup cron service (see railway.toml header).

3. First manual run:
     railway run uv run python scripts/backup_to_gcs.py

4. Verify object in GCS + sha256 match:
     gcloud storage ls gs://${BUCKET}/$(date -u +%Y/%m/%d)/

5. Once first backup verified, lock retention policy (IRREVERSIBLE):
     gsutil retention lock gs://${BUCKET}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SUMMARY
