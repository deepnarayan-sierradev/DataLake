variable "environment" {
  description = "Deployment environment (dev | staging | prod)."
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging, or prod."
  }
}

variable "curated_layer_bucket_id" {
  description = "S3 bucket ID for the curated layer (Glue table locations)."
  type        = string
}

variable "analytics_layer_bucket_id" {
  description = "S3 bucket ID for the analytics layer."
  type        = string
}

variable "athena_results_bucket_id" {
  description = "S3 bucket ID for Athena query results."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key ARN for encrypting Glue and Athena resources."
  type        = string
}

variable "tags" {
  description = "Resource tags applied to all managed resources."
  type        = map(string)
  default     = {}
}
