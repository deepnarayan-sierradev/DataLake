variable "environment" {
  description = "Deployment environment: dev, staging, or prod."
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "engine" {
  description = "Target database engine. Determines the RDS engine identifier, default port, and license model."
  type        = string
  validation {
    condition     = contains(["mysql", "postgres", "sqlserver-se"], var.engine)
    error_message = "engine must be one of: mysql, postgres, sqlserver-se."
  }
}

variable "vpc_id" {
  description = "VPC ID to place the serving store database in."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for the DB subnet group (must span at least 2 AZs)."
  type        = list(string)
}

variable "storage_kms_key_arn" {
  description = "KMS key ARN for RDS storage encryption at rest."
  type        = string
}

variable "secrets_kms_key_arn" {
  description = "KMS key ARN for the AWS-managed master user Secrets Manager secret."
  type        = string
}

variable "instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t3.micro"
}

variable "allocated_storage_gb" {
  description = "Initial allocated storage in GB."
  type        = number
  default     = 20
}

variable "max_allocated_storage_gb" {
  description = "Storage autoscaling ceiling in GB."
  type        = number
  default     = 100
}

variable "engine_version" {
  description = "Engine version. Defaults to a per-engine sensible current version if unset."
  type        = string
  default     = null
}

variable "multi_az" {
  description = "Whether to deploy a standby replica in a second AZ."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Whether to enable RDS deletion protection. Should be true in staging/prod."
  type        = bool
  default     = true
}

variable "backup_retention_period_days" {
  description = "Automated backup retention period in days."
  type        = number
  default     = 7
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
}

variable "performance_insights_enabled" {
  description = "Performance Insights (CKV_AWS_353). Set false only if the instance class rejects it."
  type        = bool
  default     = true
}
