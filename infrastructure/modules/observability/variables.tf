variable "environment" {
  type        = string
  description = "Deployment environment: dev, staging, or prod."
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "logs_kms_key_arn" {
  type        = string
  description = "ARN of the KMS key for encrypting CloudWatch log groups and the SNS alert topic."
}

variable "log_retention_days" {
  type        = number
  default     = 90
  description = "CloudWatch log retention period in days for all platform service log groups."
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.log_retention_days)
    error_message = "log_retention_days must be a valid CloudWatch retention period."
  }
}

variable "alert_email" {
  type        = string
  default     = ""
  description = "Email address for CloudWatch alarm SNS notifications. Leave empty to skip subscription."
}

variable "watermark_lag_slo_seconds" {
  type        = number
  default     = 86400 # 24 hours
  description = "Watermark lag in seconds that triggers an SLO breach alarm."
}

variable "extraction_absence_period_seconds" {
  type        = number
  default     = 3600 # 1-hour check window
  description = "CloudWatch period in seconds for the extraction-activity-absent alarm."
  validation {
    condition     = contains([60, 300, 600, 900, 1800, 3600, 86400], var.extraction_absence_period_seconds)
    error_message = "extraction_absence_period_seconds must be a valid CloudWatch period: 60, 300, 600, 900, 1800, 3600, or 86400."
  }
}

variable "extraction_absence_evaluation_periods" {
  type        = number
  default     = 2
  description = "Consecutive periods with zero RecordsExtracted before the absent-extraction alarm fires."
  validation {
    condition     = var.extraction_absence_evaluation_periods >= 1
    error_message = "extraction_absence_evaluation_periods must be at least 1."
  }
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional resource tags merged with module-managed tags."
}
