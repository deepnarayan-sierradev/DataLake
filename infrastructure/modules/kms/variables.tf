variable "environment" {
  type        = string
  description = "Deployment environment: dev, staging, or prod."
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "aws_region" {
  type        = string
  description = "AWS region where the key is created (used in CloudWatch Logs key policy condition)."
}

variable "capability" {
  type        = string
  description = "Platform capability this key protects, e.g. 'storage', 'database', 'secrets', 'logs'."
}

variable "description" {
  type        = string
  description = "Human-readable description of the key purpose."
}

variable "deletion_window_in_days" {
  type        = number
  default     = 30
  description = "Key deletion window in days. Minimum 7 (required by AWS)."
  validation {
    condition     = var.deletion_window_in_days >= 7 && var.deletion_window_in_days <= 30
    error_message = "deletion_window_in_days must be between 7 and 30."
  }
}

variable "key_user_role_arns" {
  type        = list(string)
  default     = []
  description = "IAM role ARNs permitted to use this key for encrypt/decrypt. Never use wildcards."
}

variable "key_policy" {
  type        = string
  default     = null
  description = "Custom IAM key policy JSON. If null, the default least-privilege policy is used."
}

variable "allow_cloudwatch_logs" {
  type        = bool
  default     = false
  description = "Whether to allow the CloudWatch Logs service principal to use this key."
}

variable "allow_sns" {
  type        = bool
  default     = false
  description = "Whether to allow the SNS service principal to use this key."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional resource tags merged with module-managed tags."
}
