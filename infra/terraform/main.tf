locals {
  backup_sa_email = "oncofiles-backup@${var.project_id}.iam.gserviceaccount.com"
}

# ── KMS: customer-managed encryption key ──────────────────────────────────
resource "google_kms_key_ring" "oncofiles" {
  project  = var.project_id
  name     = var.kms_keyring_name
  location = var.region
}

resource "google_kms_crypto_key" "backup" {
  name     = var.kms_key_name
  key_ring = google_kms_key_ring.oncofiles.id
  purpose  = "ENCRYPT_DECRYPT"

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = "SOFTWARE"
  }

  rotation_period = "7776000s" # 90 days

  lifecycle {
    prevent_destroy = true
  }
}

# Grant GCS service account permission to use the key for CMEK
data "google_storage_project_service_account" "gcs_sa" {
  project = var.project_id
}

resource "google_kms_crypto_key_iam_member" "gcs_cmek" {
  crypto_key_id = google_kms_crypto_key.backup.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs_sa.email_address}"
}

# ── Service account for the Railway backup cron ───────────────────────────
resource "google_service_account" "backup" {
  project      = var.project_id
  account_id   = "oncofiles-backup"
  display_name = "Oncofiles daily backup writer"
  description  = "Railway cron service uploads Turso dumps + memory snapshots to the backup bucket. Write-only, cannot delete."
}

# Least-privilege: can create objects, cannot delete or list (listing via admin SA only)
resource "google_project_iam_member" "backup_writer" {
  project = var.project_id
  role    = "roles/storage.objectCreator"
  member  = "serviceAccount:${google_service_account.backup.email}"
}

resource "google_kms_crypto_key_iam_member" "backup_encrypter" {
  crypto_key_id = google_kms_crypto_key.backup.id
  role          = "roles/cloudkms.cryptoKeyEncrypter"
  member        = "serviceAccount:${google_service_account.backup.email}"
}

# ── GCS backup bucket ──────────────────────────────────────────────────────
resource "google_storage_bucket" "backups" {
  project                     = var.project_id
  name                        = var.backup_bucket_name
  location                    = var.region
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  encryption {
    default_kms_key_name = google_kms_crypto_key.backup.id
  }

  retention_policy {
    retention_period = var.object_lock_days * 86400
    is_locked        = false # flip to true after first run verified; once locked it cannot be reduced
  }

  lifecycle_rule {
    condition {
      age                = 30
      matches_storage_class = ["STANDARD"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  lifecycle_rule {
    condition {
      age                = 90
      matches_storage_class = ["NEARLINE"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "COLDLINE"
    }
  }

  lifecycle_rule {
    condition {
      age                = 365
      matches_storage_class = ["COLDLINE"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "ARCHIVE"
    }
  }

  lifecycle_rule {
    condition {
      age = var.backup_retention_days
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    app     = "oncofiles"
    purpose = "backup"
    gdpr    = "eu-resident"
  }

  depends_on = [google_kms_crypto_key_iam_member.gcs_cmek]
}

# ── Admin SA for restore / lifecycle ops (break-glass) ────────────────────
resource "google_service_account" "backup_admin" {
  project      = var.project_id
  account_id   = "oncofiles-backup-admin"
  display_name = "Oncofiles backup admin (break-glass)"
  description  = "Used only for restore or manual lifecycle ops. Key NOT deployed to Railway."
}

resource "google_storage_bucket_iam_member" "admin_full" {
  bucket = google_storage_bucket.backups.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.backup_admin.email}"
}

resource "google_kms_crypto_key_iam_member" "admin_decrypter" {
  crypto_key_id = google_kms_crypto_key.backup.id
  role          = "roles/cloudkms.cryptoKeyDecrypter"
  member        = "serviceAccount:${google_service_account.backup_admin.email}"
}
