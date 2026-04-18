output "backup_bucket_url" {
  value       = "gs://${google_storage_bucket.backups.name}"
  description = "GCS path for daily dumps"
}

output "kms_key_id" {
  value       = google_kms_crypto_key.backup.id
  description = "KMS key resource ID for CMEK"
}

output "backup_sa_email" {
  value       = google_service_account.backup.email
  description = "Writer SA — key goes into Railway as GOOGLE_APPLICATION_CREDENTIALS_JSON"
}

output "backup_admin_sa_email" {
  value       = google_service_account.backup_admin.email
  description = "Break-glass admin SA — NEVER put in Railway env"
}
