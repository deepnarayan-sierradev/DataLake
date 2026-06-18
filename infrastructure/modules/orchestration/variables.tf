variable "environment" {
  description = "Deployment environment: dev, staging, or prod."
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "kms_key_arn" {
  description = "ARN of the platform KMS key used to encrypt CloudWatch Logs for Step Functions."
  type        = string
}

variable "step_functions_role_arn" {
  description = "ARN of the IAM role assumed by Step Functions to invoke Lambda and write logs."
  type        = string
}

variable "extraction_pipeline_lambda_arn" {
  description = "ARN of the Lambda function that executes the extraction pipeline (ExtractionWorkflow)."
  type        = string
}

variable "transformation_pipeline_lambda_arn" {
  description = "ARN of the Lambda function that executes the transformation pipeline (raw → curated)."
  type        = string
}

variable "entity_resolution_lambda_arn" {
  description = "ARN of the Lambda function that runs entity resolution and produces golden records."
  type        = string
}

variable "analytics_publisher_lambda_arn" {
  description = "ARN of the Lambda function that publishes analytics layer datasets and registers Glue Catalog tables."
  type        = string
}

variable "serving_store_loader_lambda_arn" {
  description = "ARN of the Lambda function that loads analytics datasets into the MySQL RDS serving store."
  type        = string
}

variable "state_machine_type" {
  description = "Step Functions state machine type. Use STANDARD for staging/prod (execution history, longer timeouts). Use EXPRESS for dev (lower cost)."
  type        = string
  default     = "STANDARD"
  validation {
    condition     = contains(["STANDARD", "EXPRESS"], var.state_machine_type)
    error_message = "state_machine_type must be STANDARD or EXPRESS."
  }
}

variable "log_retention_days" {
  description = "Retention period in days for the Step Functions execution log group."
  type        = number
  default     = 90
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be a valid CloudWatch Logs retention value."
  }
}

variable "enable_xray_tracing" {
  description = "Enable AWS X-Ray tracing for Step Functions executions."
  type        = bool
  default     = true
}

variable "alert_topic_arn" {
  description = "ARN of the SNS topic to notify when pipeline execution alarms fire. Empty string disables notifications."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags to apply to all resources created by this module."
  type        = map(string)
  default     = {}
}
