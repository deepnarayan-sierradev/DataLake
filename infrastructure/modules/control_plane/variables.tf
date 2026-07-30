variable "environment" {
  description = "Deployment environment: dev, uat, or prod."
  type        = string
  validation {
    condition     = contains(["dev", "uat", "prod"], var.environment)
    error_message = "environment must be one of: dev, uat, prod."
  }
}

variable "kms_key_arn" {
  description = "ARN of the platform KMS key used to encrypt CloudWatch Logs for the control-plane Lambda and API Gateway access logs."
  type        = string
}

variable "log_retention_days" {
  description = "Retention period in days for the control-plane Lambda and API Gateway access log groups."
  type        = number
  default     = 365
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be a valid CloudWatch Logs retention value."
  }
}

variable "enable_xray_tracing" {
  description = "Enable AWS X-Ray tracing for the control-plane Lambda."
  type        = bool
  default     = true
}

variable "lambda_package_s3_bucket" {
  description = "S3 bucket holding the Lambda deployment zip (the control-plane Lambda ships in the same package as the rest of connector_runtime)."
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

variable "control_plane_role_arn" {
  description = "ARN of the IAM role assumed by the control-plane Lambda (defined in the iam module)."
  type        = string
}

variable "pipeline_trigger_queue_url" {
  description = "URL of the SQS FIFO pipeline trigger queue that pipeline_trigger_handler.py consumes (orchestration module output)."
  type        = string
}

variable "entity_config_table_name" {
  description = "Name of the entity extraction config DynamoDB table."
  type        = string
}

variable "entity_type_registry_table_name" {
  description = "Name of the entity type registry DynamoDB table (also used as the tenant registry for provisioning)."
  type        = string
}

variable "run_audit_log_table_name" {
  description = "Name of the run audit log DynamoDB table."
  type        = string
}

variable "analytics_s3_bucket_name" {
  description = "Analytics layer S3 bucket. Passed as ANALYTICS_S3_BUCKET so semantic queries can read golden records."
  type        = string
}

variable "twin_index_table_name" {
  description = "Name of the twin index DynamoDB table."
  type        = string
}

variable "semantic_model_table_name" {
  description = "Name of the semantic model DynamoDB table."
  type        = string
}

variable "saved_query_table_name" {
  description = "Name of the saved query DynamoDB table."
  type        = string
}

variable "cognito_password_minimum_length" {
  description = "Minimum password length enforced by the control-plane Cognito User Pool."
  type        = number
  default     = 12
}

variable "tags" {
  description = "Tags to apply to all resources created by this module."
  type        = map(string)
  default     = {}
}

variable "reserved_concurrent_executions" {
  description = "Per-function concurrency ceiling (CKV_AWS_115)."
  type        = number
  default     = 20
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

variable "name_prefix" {
  type        = string
  description = "Resource name prefix for the platform (e.g. 'datalake'). Combined with the environment to form every resource name."
}

variable "resource_names" {
  type        = map(string)
  description = "Every physical resource name this Lambda reads from its environment, built once in the environment root from the resources Terraform actually created."
}
