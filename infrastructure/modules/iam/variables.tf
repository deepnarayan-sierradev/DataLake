variable "environment" {
  type        = string
  description = "Deployment environment: dev, staging, or prod."
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "raw_layer_bucket_arn" {
  type        = string
  description = "ARN of the raw layer S3 bucket."
}

variable "curated_layer_bucket_arn" {
  type        = string
  description = "ARN of the curated layer S3 bucket."
}

variable "analytics_layer_bucket_arn" {
  type        = string
  description = "ARN of the analytics layer S3 bucket."
}

variable "schema_snapshots_bucket_arn" {
  type        = string
  description = "ARN of the schema snapshots S3 bucket."
}

variable "watermark_table_arn" {
  type        = string
  description = "ARN of the watermark repository DynamoDB table."
}

variable "run_audit_log_table_arn" {
  type        = string
  description = "ARN of the run audit log DynamoDB table."
}

variable "kms_key_arns_for_extraction" {
  type        = list(string)
  description = "KMS key ARNs the extraction runtime role is allowed to use."
}

variable "kms_key_arns_for_transformation" {
  type        = list(string)
  description = "KMS key ARNs the transformation job role is allowed to use."
}

variable "dlq_arn" {
  type        = string
  description = "ARN of the dead-letter SQS queue for failed extraction runs."
}

variable "github_org" {
  type        = string
  description = "GitHub organisation name for OIDC trust policy condition."
}

variable "github_repo" {
  type        = string
  description = "GitHub repository name for OIDC trust policy condition."
}

variable "cicd_deployment_policy_arns" {
  type        = list(string)
  default     = []
  description = "List of IAM managed policy ARNs to attach to the CI/CD deployment role."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional resource tags merged with module-managed tags."
}
