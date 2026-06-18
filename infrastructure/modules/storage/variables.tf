variable "environment" {
  type        = string
  description = "Deployment environment: dev, staging, or prod."
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "project_name" {
  type        = string
  description = "Short project identifier used in bucket names (e.g. 'edl')."
}

variable "storage_kms_key_arn" {
  type        = string
  description = "ARN of the KMS key used for S3 server-side encryption across all buckets."
}

variable "extraction_runtime_role_arns" {
  type        = list(string)
  description = "IAM role ARNs permitted to write to the raw layer bucket. All other principals are denied."
}

variable "raw_object_lock_retention_days" {
  type        = number
  default     = 365
  description = "Default Object Lock retention period in days for the raw layer bucket."
  validation {
    condition     = var.raw_object_lock_retention_days >= 1
    error_message = "raw_object_lock_retention_days must be at least 1."
  }
}

variable "raw_noncurrent_version_retention_days" {
  type        = number
  default     = 30
  description = "Days to retain non-current versions in the raw layer before expiry."
}

variable "access_logs_retention_days" {
  type        = number
  default     = 90
  description = "Days to retain S3 access logs."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional resource tags merged with module-managed tags."
}
