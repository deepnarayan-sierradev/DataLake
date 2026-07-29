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
  description = "ARN of the Lambda function that loads analytics datasets into the MySQL RDS serving store. Leave empty to skip this stage (pipeline ends successfully after analytics publication)."
  type        = string
  default     = ""
}

variable "twin_build_lambda_arn" {
  description = "ARN of the twin builder Lambda invoked by the BuildTwin stage. Leave empty to skip the stage (a Pass that continues to the serving-store stage)."
  type        = string
  default     = ""
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
  default     = 365
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

variable "lambda_package_s3_bucket" {
  description = "S3 bucket holding the Lambda deployment zip (used by the pipeline trigger Lambda)."
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

variable "pipeline_trigger_role_arn" {
  description = "ARN of the IAM role assumed by the pipeline trigger Lambda."
  type        = string
}

variable "pipeline_trigger_reserved_concurrency" {
  description = "Reserved concurrent executions for the pipeline trigger Lambda."
  type        = number
  default     = 50
  validation {
    condition     = var.pipeline_trigger_reserved_concurrency >= 1 && var.pipeline_trigger_reserved_concurrency <= 1000
    error_message = "pipeline_trigger_reserved_concurrency must be between 1 and 1000."
  }
}

variable "dlq_processor_role_arn" {
  description = "ARN of the IAM role assumed by the DLQ processor Lambda."
  type        = string
}

variable "extraction_failure_dlq_arn" {
  description = "ARN of the extraction failure SQS DLQ consumed by the DLQ processor Lambda."
  type        = string
}

variable "run_audit_log_table_name" {
  description = "Name of the DynamoDB run audit log table (used by DLQ processor)."
  type        = string
}

variable "max_extraction_resume_attempts" {
  description = <<-EOT
    How many times one extraction may checkpoint and resume before the execution fails visibly
    (L14). Each resume is real progress — a committed partial watermark — so a generous bound is
    safe; the bound exists so a provider that throttles indefinitely ends at a terminal state an
    operator can see rather than looping forever and burying it in execution history.
  EOT
  type        = number
  default     = 12
}

variable "dlq_alarm_overrides" {
  description = <<-DESC
    Per-key overrides for the DLQ alarm thresholds derived from `environment`.

    Defaults live in `per_stage_dlq.tf`'s `dlq_alarm_defaults` and are sized for the 12-month
    production target (10-20 tenants, 5-12 sources, 100+ entities per source). Override only to
    deviate from the environment's default — e.g. tightening a blocking-stage threshold for a
    tenant SLA — so the sized numbers stay in one place rather than being copied per environment.

    Accepted keys: oldest_critical_path_seconds, oldest_additive_seconds, oldest_realtime_seconds,
    arrival_spike_per_period, backlog_depth.
  DESC
  type        = map(number)
  default     = {}

  validation {
    condition = length(setsubtract(keys(var.dlq_alarm_overrides), [
      "oldest_critical_path_seconds",
      "oldest_additive_seconds",
      "oldest_realtime_seconds",
      "arrival_spike_per_period",
      "backlog_depth",
    ])) == 0
    error_message = "dlq_alarm_overrides accepts only the five documented threshold keys."
  }
}

variable "dlq_processor_reserved_concurrency" {
  description = <<-DESC
    Reserved concurrent executions for the DLQ processor.

    Reserved rather than unbounded so a failure flood cannot consume account concurrency and
    starve the pipeline it is trying to help. It had none before 2026-07-29, while the pipeline
    trigger reserved 50.
  DESC
  type        = number
  default     = 10

  validation {
    condition     = var.dlq_processor_reserved_concurrency >= 1 && var.dlq_processor_reserved_concurrency <= 100
    error_message = "dlq_processor_reserved_concurrency must be between 1 and 100."
  }
}

variable "dlq_processor_batch_size" {
  description = <<-DESC
    SQS batch size for the DLQ processor.

    Was 1, justified as "clear per-message audit trail" — but the audit trail is one DynamoDB row
    per message regardless of batch size, so the two were conflated. At the 12-month target a bad
    deploy can fail one tenant's ~1,200 entities, which at batch_size 1 is 1,200 invocations.
    `ReportBatchItemFailures` is enabled, so a partial failure re-drives only the failed messages.
  DESC
  type        = number
  default     = 10

  validation {
    condition     = var.dlq_processor_batch_size >= 1 && var.dlq_processor_batch_size <= 10
    error_message = "dlq_processor_batch_size must be between 1 and 10."
  }
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
