variable "environment" {
  description = "Deployment environment: dev, staging, or prod."
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "kms_key_arn" {
  description = "ARN of the platform KMS key for Lambda env var encryption and CloudWatch log encryption."
  type        = string
}

variable "execution_role_arn" {
  description = "ARN of the IAM role the Lambda assumes at runtime (twin_build_runtime role)."
  type        = string
}

variable "lambda_package_s3_bucket" {
  description = "S3 bucket holding the Lambda deployment zip package."
  type        = string
}

variable "lambda_package_s3_key" {
  description = "S3 key of the Lambda deployment zip package."
  type        = string
}

variable "lambda_package_source_hash" {
  description = "Base64 SHA-256 hash of the Lambda zip package."
  type        = string
}

variable "analytics_s3_bucket_name" {
  description = "Analytics layer S3 bucket. Passed as ANALYTICS_S3_BUCKET (read golden records, write intermediate edges)."
  type        = string
}

variable "relationship_rules_s3_bucket_name" {
  description = "Bucket holding relationship-rules config JSON. Passed as RELATIONSHIP_RULES_S3_BUCKET (read-only)."
  type        = string
}

variable "log_retention_days" {
  description = "Retention period in days for Lambda execution logs."
  type        = number
  default     = 365
}

variable "memory_size_mb" {
  description = "Lambda memory allocation in MB (DuckDB benefits from more)."
  type        = number
  default     = 1024
  validation {
    condition     = var.memory_size_mb >= 256 && var.memory_size_mb <= 10240
    error_message = "memory_size_mb must be between 256 and 10240."
  }
}

variable "timeout_seconds" {
  description = "Lambda execution timeout in seconds."
  type        = number
  default     = 300
  validation {
    condition     = var.timeout_seconds >= 30 && var.timeout_seconds <= 900
    error_message = "timeout_seconds must be between 30 and 900."
  }
}

variable "reserved_concurrent_executions" {
  description = "Reserved concurrency limit. -1 means unreserved."
  type        = number
  default     = -1
}

variable "enable_xray_tracing" {
  description = "Whether to enable AWS X-Ray active tracing."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Additional tags applied to all resources."
  type        = map(string)
  default     = {}
}
