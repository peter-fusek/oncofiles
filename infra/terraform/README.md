# Oncofiles infra — Terraform

Provisions GCP resources for the backup & disaster-recovery plan documented in `DISASTER_RECOVERY.md` and tracked in oncofiles#425.

## Resources

- `google_storage_bucket.backups` — `oncofiles-backups-eu` in `europe-west3`, CMEK + versioning + 30-day retention lock + lifecycle Standard→Nearline→Coldline→Archive→delete at 730d
- `google_kms_key_ring.oncofiles` + `google_kms_crypto_key.backup` — CMEK with 90-day auto-rotation
- `google_service_account.backup` — `oncofiles-backup@…` — least-privilege writer (objectCreator only, no delete)
- `google_service_account.backup_admin` — break-glass admin (never deployed to Railway)

## Bootstrapping the TF state bucket (one-time, manual)

Terraform stores its own state in `gs://oncofiles-tfstate-eu`. Create it once before `terraform init`:

```bash
gcloud auth login
gcloud config set project oncofiles-490809
gsutil mb -l europe-west3 -b on gs://oncofiles-tfstate-eu
gsutil versioning set on gs://oncofiles-tfstate-eu
```

## Apply (via GCP Cloud Shell — recommended)

Cloud Shell already has your GCP auth. Open https://shell.cloud.google.com and:

```bash
git clone https://github.com/peter-fusek/oncofiles.git
cd oncofiles/infra/terraform
terraform init
terraform plan          # review carefully
terraform apply         # type "yes" to confirm
terraform output        # grab bucket URL + SA email
```

## Create + extract the backup SA key (one-time)

```bash
gcloud iam service-accounts keys create /tmp/oncofiles-backup-sa.json \
  --iam-account=$(terraform output -raw backup_sa_email)
cat /tmp/oncofiles-backup-sa.json
# Copy the JSON and set it on Railway:
#   railway variables set GOOGLE_APPLICATION_CREDENTIALS_JSON='<paste>'
# Then immediately delete the local key file:
shred -u /tmp/oncofiles-backup-sa.json   # or: rm -P on macOS
```

## Lock the retention policy (after first successful backup)

The bucket's retention policy is NOT locked initially — flip `is_locked = true` in `main.tf` and re-apply ONLY after you are fully satisfied with the 30-day value, because lock is irreversible.

## Cost estimate

At current data volumes (~100 MB daily compressed): < $2/mo. See `DISASTER_RECOVERY.md` §3.
