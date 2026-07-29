variable "environment" {
  type        = string
  description = "Deployment environment: dev, staging, or prod."
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "secrets_kms_key_arn" {
  type        = string
  description = "ARN of the KMS key used to encrypt secrets at rest."
}

variable "logs_kms_key_arn" {
  type        = string
  description = "ARN of the KMS key used to encrypt the credential expiry notifier Lambda's environment variables and CloudWatch log group. Must have allow_cloudwatch_logs enabled — the secrets KMS key does not grant the CloudWatch Logs service principal access."
}

variable "extraction_runtime_role_arns" {
  type        = list(string)
  description = "IAM role ARNs permitted to call GetSecretValue on source credential secrets."
}

variable "secret_recovery_window_days" {
  type        = number
  default     = 30
  description = "Recovery window in days before a deleted secret is permanently removed."
  validation {
    condition     = var.secret_recovery_window_days >= 7 && var.secret_recovery_window_days <= 30
    error_message = "secret_recovery_window_days must be between 7 and 30."
  }
}

variable "salesforce_rotation_lambda_arn" {
  type        = string
  default     = null
  description = "ARN of the Lambda function for Salesforce credential rotation. When non-null, automatic rotation is enabled."
}

variable "netsuite_rotation_lambda_arn" {
  type        = string
  default     = null
  description = "ARN of the Lambda function for NetSuite credential rotation. When non-null, automatic rotation is enabled."
}

variable "mysql_rds_rotation_lambda_arn" {
  type        = string
  default     = null
  description = "ARN of the Lambda function for MySQL RDS credential rotation. When non-null, automatic rotation is enabled."
}

variable "secret_rotation_days" {
  type        = number
  default     = 90
  description = "Days between automatic rotations when a rotation Lambda is configured."
  validation {
    condition     = var.secret_rotation_days >= 30 && var.secret_rotation_days <= 365
    error_message = "secret_rotation_days must be between 30 and 365."
  }
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional resource tags merged with module-managed tags."
}

# ---------------------------------------------------------------------------
# Credential expiry notifier Lambda (SEC-6)
# ---------------------------------------------------------------------------

variable "credential_expiry_notifier_role_arn" {
  type        = string
  description = "IAM role ARN for the credential expiry notifier Lambda (module.iam.credential_expiry_notifier_role_arn)."
}

variable "credential_expiry_scheduler_role_arn" {
  type        = string
  description = "IAM role ARN for the EventBridge Scheduler that invokes the notifier Lambda (module.iam.credential_expiry_scheduler_role_arn)."
}

variable "alert_topic_arn" {
  type        = string
  description = "ARN of the platform alerts SNS topic (module.observability.platform_alerts_topic_arn)."
}

variable "lambda_package_s3_bucket" {
  type        = string
  description = "S3 bucket holding the Lambda deployment zip package."
}

variable "lambda_package_s3_key" {
  type        = string
  description = "S3 key of the Lambda deployment zip package."
}

variable "lambda_package_source_hash" {
  type        = string
  description = "Base64 SHA-256 hash of the Lambda zip package."
}

variable "log_retention_days" {
  type        = number
  default     = 365
  description = "CloudWatch Logs retention in days for the notifier Lambda."
}

variable "enable_xray_tracing" {
  type        = bool
  default     = true
  description = "Whether to enable AWS X-Ray active tracing on the notifier Lambda."
}

variable "rotation_warning_days" {
  type        = number
  default     = 14
  description = "Days before secret_rotation_days to start warning that a credential needs rotation."
}

variable "reserved_concurrent_executions" {
  description = "Per-function concurrency ceiling (CKV_AWS_115)."
  type        = number
  default     = 2
}

variable "code_signing_config_arn" {
  description = "Lambda code-signing configuration (CKV_AWS_272). Null leaves signing unattached."
  type        = string
  default     = null
}

variable "vpc_id" {
  description = "VPC the function's ENIs are created in (CKV_AWS_117). Null keeps the function outside a VPC."
  type        = string
  default     = null
}

variable "subnet_ids" {
  description = "Private subnets for the function's ENIs. Empty keeps the function outside a VPC."
  type        = list(string)
  default     = []
}

variable "security_group_ids" {
  description = "Additional security groups, alongside the one this module creates."
  type        = list(string)
  default     = []
}
