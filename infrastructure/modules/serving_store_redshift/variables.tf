variable "environment" {
  description = "Deployment environment: dev, staging, or prod."
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "vpc_id" {
  description = "VPC ID to place the serving store Redshift Serverless workgroup in."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for the Redshift Serverless workgroup (must span >= 3 AZs)."
  type        = list(string)
}

variable "storage_kms_key_arn" {
  description = "KMS key ARN for Redshift namespace encryption at rest."
  type        = string
}

variable "secrets_kms_key_arn" {
  description = "KMS key ARN for the managed admin credential and connection-metadata secret."
  type        = string
}

variable "analytics_bucket_arn" {
  description = "ARN of the analytics-layer S3 bucket the COPY IAM role may read from."
  type        = string
}

variable "analytics_kms_key_arn" {
  description = "KMS key ARN protecting the analytics-layer bucket (for COPY kms:Decrypt)."
  type        = string
}

variable "database_name" {
  description = "Top-level Redshift database created in the namespace (schema-per-tenant lives here)."
  type        = string
  default     = "edl_serving"
}

variable "base_capacity_rpu" {
  description = "Redshift Serverless base capacity in RPUs (increments of 8)."
  type        = number
  default     = 8
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
}
