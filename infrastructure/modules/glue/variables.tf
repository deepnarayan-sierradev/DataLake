variable "environment" {
  description = "Deployment environment (dev | uat | prod)."
  type        = string
  validation {
    condition     = contains(["dev", "uat", "prod"], var.environment)
    error_message = "environment must be dev, uat, or prod."
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

variable "analytics_reader_principals" {
  description = "IAM principal ARNs granted explicit Lake Formation SELECT+DESCRIBE (table wildcard) on the curated and analytics databases — required because IAM_ALLOWED_PRINCIPALS doesn't satisfy Athena's GetUnfilteredTableMetadata path. Covers all tenants automatically (shared databases, tenant-scoped by table name)."
  type        = list(string)
  default     = []
}

variable "name_prefix" {
  type        = string
  description = "Resource name prefix for the platform (e.g. 'datalake'). Combined with the environment to form every resource name."
}
