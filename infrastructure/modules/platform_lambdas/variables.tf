variable "environment" {
  description = "Deployment environment (dev/staging/prod)."
  type        = string
}

variable "tags" {
  description = "Common resource tags."
  type        = map(string)
  default     = {}
}

# ─── Package ─────────────────────────────────────────────────────────────────

variable "lambda_package_s3_bucket" {
  description = "S3 bucket holding the shared Lambda deployment package."
  type        = string
}

variable "lambda_package_s3_key" {
  description = "S3 key of the shared Lambda deployment package."
  type        = string
}

variable "lambda_package_source_hash" {
  description = "Base64 SHA256 of the package, so a code change redeploys."
  type        = string
  default     = null
}

# ─── Roles: one per function, never shared ───────────────────────────────────
# The write-back function must not be able to read the webhook secret, and the portability
# function's delete permissions must not be granted to anything on the ingestion path.

variable "webhook_receiver_role_arn" {
  description = "Execution role for the webhook receiver."
  type        = string
}

variable "writeback_role_arn" {
  description = "Execution role for the write-back function; reads the -writeback secret only."
  type        = string
}

variable "workflow_runner_role_arn" {
  description = "Execution role for the workflow runner."
  type        = string
}

variable "portability_role_arn" {
  description = "Execution role for export/deletion; the only role with bulk delete permissions."
  type        = string
}

# ─── Shared infrastructure references ────────────────────────────────────────

variable "kms_key_arn" {
  description = "CMK for Lambda environment-variable encryption."
  type        = string
  default     = null
}

variable "logs_kms_key_arn" {
  description = "CMK for CloudWatch log group encryption."
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 365
}

variable "enable_xray_tracing" {
  description = "Enable active X-Ray tracing."
  type        = bool
  default     = true
}

# ─── Webhook receiver ────────────────────────────────────────────────────────

variable "webhook_ingest_queue_url" {
  description = "FIFO queue the receiver enqueues to; nothing is processed inline."
  type        = string
  default     = ""
}

variable "webhook_dedup_table_name" {
  description = "Short-TTL table for provider event ids."
  type        = string
  default     = "EdlWebhookEventDedup"
}

variable "control_plane_api_id" {
  description = <<-EOT
    HTTP API id to attach the webhook route to. Empty disables the route, which is the correct
    default for an environment with no provider webhooks configured — an unauthenticated route
    that nothing uses is still attack surface.
  EOT
  type        = string
  default     = ""
}

variable "control_plane_api_execution_arn" {
  description = "Execution ARN of the HTTP API, for the invoke permission."
  type        = string
  default     = ""
}

# ─── Write-back ──────────────────────────────────────────────────────────────

variable "entity_config_table_name" {
  description = "Entity configuration table; write-back reads writeback_enabled from it."
  type        = string
  default     = "EdlEntityExtractionConfig"
}

variable "run_audit_log_table_name" {
  description = "Run audit log; write-back is audited under a distinct stage value."
  type        = string
  default     = "EdlRunAuditLog"
}

# ─── Workflow runner ─────────────────────────────────────────────────────────

variable "workflow_definition_table_name" {
  type    = string
  default = "EdlWorkflowDefinition"
}

variable "workflow_execution_table_name" {
  type    = string
  default = "EdlWorkflowExecution"
}

variable "workflow_idempotency_table_name" {
  type    = string
  default = "EdlWorkflowIdempotency"
}

variable "workflow_destination_table_name" {
  type    = string
  default = "EdlWorkflowDestination"
}

variable "workflow_task_table_name" {
  type    = string
  default = "EdlWorkflowTask"
}

variable "workflow_schedule_enabled" {
  description = <<-EOT
    Create one workflow-runner schedule per tenant. Disabled by default: a schedule that fires
    against an empty definition table does nothing but cost invocations, and enabling it before
    any workflow is published would make the absence alarms noisy.
  EOT
  type        = bool
  default     = false
}

variable "workflow_schedule_expression" {
  description = "Schedule rate for workflow evaluation."
  type        = string
  default     = "rate(1 hour)"
}

variable "tenant_codes" {
  description = "Tenants to create a workflow schedule for; one schedule each."
  type        = list(string)
  default     = []
}

variable "scheduler_group_name" {
  description = "EventBridge Scheduler group."
  type        = string
  default     = "default"
}

variable "scheduler_invoke_role_arn" {
  description = "Role the scheduler assumes to invoke the runner."
  type        = string
  default     = ""
}

variable "workflow_dlq_arn" {
  description = "Dead-letter queue for schedule delivery failures."
  type        = string
  default     = ""
}

# ─── Portability ─────────────────────────────────────────────────────────────

variable "export_artefact_bucket_name" {
  description = "Bucket the export artefact is written to."
  type        = string
  default     = ""
}

variable "export_job_table_name" {
  type    = string
  default = "EdlExportJob"
}

variable "deletion_certificate_table_name" {
  type    = string
  default = "EdlDeletionCertificate"
}

variable "raw_s3_bucket_name" {
  description = "Raw layer bucket; deletion sweeps its tenant prefix."
  type        = string
  default     = ""
}

variable "curated_s3_bucket_name" {
  description = "Curated layer bucket."
  type        = string
  default     = ""
}

variable "analytics_s3_bucket_name" {
  description = "Analytics layer bucket."
  type        = string
  default     = ""
}

variable "reserved_concurrent_executions" {
  description = "Per-function concurrency ceiling. Bounds one function's share of the account pool."
  type        = number
  default     = 10
}
