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

variable "entity_config_table_arn" {
  type        = string
  description = "ARN of the entity extraction config DynamoDB table (read-only by the extraction runtime)."
}

variable "entity_type_registry_table_arn" {
  type        = string
  description = "ARN of the entity type registry DynamoDB table (ARCH-2; read by entity resolution and analytics publisher runtimes)."
}

variable "serving_store_config_table_arn" {
  type        = string
  description = "ARN of the serving store config DynamoDB table (read-only by the serving store loader runtime)."
}

variable "twin_index_table_arn" {
  type        = string
  description = "ARN of the twin index DynamoDB table (written by twin build, read by the control plane)."
}

variable "semantic_model_table_arn" {
  type        = string
  description = "ARN of the semantic model DynamoDB table (read by the control plane)."
}

variable "saved_query_table_arn" {
  type        = string
  description = "ARN of the saved query DynamoDB table (read/written by the control plane)."
}

variable "serving_store_secret_arns" {
  type        = list(string)
  description = "Secrets Manager ARNs the serving store loader role may read/create/update: the writer credential(s) plus the edl/serving-store/* reader-credential prefix."
}

variable "kms_key_arns_for_extraction" {
  type        = list(string)
  description = "KMS key ARNs the extraction runtime role is allowed to use."
}

variable "kms_key_arns_for_transformation" {
  type        = list(string)
  description = "KMS key ARNs the transformation job role is allowed to use."
}

variable "kms_key_arns_for_credential_expiry_notifier" {
  type        = list(string)
  description = "KMS key ARNs the credential expiry notifier role is allowed to use (decrypts its own Lambda environment variables)."
}

variable "kms_key_arns_for_serving_store" {
  type        = list(string)
  description = "KMS key ARNs the serving store loader role may use to decrypt the (CMK-encrypted) writer credential secret."
  default     = []
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
