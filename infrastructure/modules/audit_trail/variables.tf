variable "environment" {
  description = "Deployment environment (dev/staging/prod)."
  type        = string
}

variable "account_id" {
  description = "AWS account id, used in the trail bucket name and the write prefix."
  type        = string
}

variable "kms_key_arn" {
  description = "CMK for the trail's S3 objects and log-file encryption."
  type        = string
}

variable "logs_kms_key_arn" {
  description = "CMK for the CloudWatch log group receiving trail events."
  type        = string
}

variable "data_bucket_arns" {
  description = <<-EOT
    Data-plane bucket ARNs to record object-level events for. Empty disables the data-event selector,
    which also means the tenant boundary's audit stage sees API calls without their object keys — so
    "would this have been denied" becomes unanswerable for S3. Pass the buckets.
  EOT
  type        = list(string)
  default     = []
}

variable "retention_days" {
  description = "Days before trail objects expire. SOC 2 evidence retention (DL-SEC-17)."
  type        = number
  default     = 400

  validation {
    condition     = var.retention_days >= 365
    error_message = "retention_days must be at least 365: a trail shorter than the audit period is not evidence."
  }
}

variable "log_group_retention_days" {
  description = "CloudWatch retention for trail events; the S3 copy is the long-term record."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
