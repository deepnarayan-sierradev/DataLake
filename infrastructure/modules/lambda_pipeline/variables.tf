variable "environment" {
  description = "Deployment environment: dev, staging, or prod."
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "kms_key_arn" {
  description = "ARN of the platform KMS key for Lambda environment variable encryption."
  type        = string
}

variable "execution_role_arn" {
  description = "ARN of the IAM role the Lambda assumes at runtime (extraction_runtime_role)."
  type        = string
}

variable "lambda_package_s3_bucket" {
  description = "S3 bucket holding the Lambda deployment zip package."
  type        = string
}

variable "lambda_package_s3_key" {
  description = "S3 key of the Lambda deployment zip package (e.g. 'lambda/extraction-pipeline.zip')."
  type        = string
}

variable "lambda_package_source_hash" {
  description = "Base64 SHA-256 hash of the Lambda zip package. Used by Terraform to detect code changes."
  type        = string
}

variable "raw_s3_bucket_name" {
  description = "Name (not ARN) of the raw layer S3 bucket. Passed to Lambda as RAW_S3_BUCKET env var."
  type        = string
}

variable "schema_snapshot_s3_bucket_name" {
  description = "Name of the schema snapshot S3 bucket. Passed to Lambda as SCHEMA_SNAPSHOT_S3_BUCKET."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for Lambda VPC configuration."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security group IDs for Lambda VPC configuration."
  type        = list(string)
}

variable "cloudwatch_log_group_arn" {
  description = "ARN of the CloudWatch Log Group for this Lambda (/edl/{env}/connector-runtime)."
  type        = string
}

variable "log_retention_days" {
  description = "Retention period in days for Lambda execution logs."
  type        = number
  default     = 30
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 731], var.log_retention_days)
    error_message = "log_retention_days must be a valid CloudWatch Logs retention value."
  }
}

variable "memory_size_mb" {
  description = "Lambda memory allocation in MB. Increase for large-volume entity extractions."
  type        = number
  default     = 1024
  validation {
    condition     = var.memory_size_mb >= 128 && var.memory_size_mb <= 10240
    error_message = "memory_size_mb must be between 128 and 10240 MB."
  }
}

variable "timeout_seconds" {
  description = "Lambda timeout in seconds. Maximum single-entity extraction duration."
  type        = number
  default     = 900
  validation {
    condition     = var.timeout_seconds >= 10 && var.timeout_seconds <= 900
    error_message = "timeout_seconds must be between 10 and 900."
  }
}

variable "reserved_concurrent_executions" {
  description = "Reserved concurrency for the Lambda. -1 = unreserved (uses account pool)."
  type        = number
  default     = -1
}

variable "enable_xray_tracing" {
  description = "Enable AWS X-Ray active tracing for Lambda invocations."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags to apply to all resources in this module."
  type        = map(string)
  default     = {}
}
