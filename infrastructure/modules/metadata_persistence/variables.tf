variable "environment" {
  type        = string
  description = "Deployment environment: dev, staging, or prod."
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "database_kms_key_arn" {
  type        = string
  description = "ARN of the KMS key for DynamoDB and SQS encryption at rest."
}

variable "orchestration_role_arns" {
  type        = list(string)
  description = "IAM role ARNs (Step Functions roles) permitted to send to the failure DLQ."
}

variable "replay_operator_role_arns" {
  type        = list(string)
  description = "IAM role ARNs permitted to read and delete from the failure DLQ for replay operations."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional resource tags merged with module-managed tags."
}
