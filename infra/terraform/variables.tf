variable "project_id" {
  description = "GCP project ID hosting oncofiles infra"
  type        = string
  default     = "oncofiles-490809"
}

variable "region" {
  description = "GCP region for backup bucket + KMS. Must be EU for GDPR/SK patient data residency."
  type        = string
  default     = "europe-west3"
}

variable "backup_bucket_name" {
  description = "GCS bucket for oncofiles backups"
  type        = string
  default     = "oncofiles-backups-eu"
}

variable "kms_keyring_name" {
  description = "KMS keyring for backup encryption"
  type        = string
  default     = "oncofiles"
}

variable "kms_key_name" {
  description = "KMS key for backup CMEK"
  type        = string
  default     = "backup-key"
}

variable "backup_retention_days" {
  description = "Total retention before permanent delete"
  type        = number
  default     = 730
}

variable "object_lock_days" {
  description = "Compliance lock — objects cannot be deleted for this many days"
  type        = number
  default     = 30
}
