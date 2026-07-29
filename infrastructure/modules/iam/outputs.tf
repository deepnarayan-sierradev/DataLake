output "extraction_runtime_role_arn" {
  description = "ARN of the extraction runtime IAM role."
  value       = aws_iam_role.extraction_runtime.arn
}

output "extraction_runtime_role_name" {
  description = "Name of the extraction runtime IAM role."
  value       = aws_iam_role.extraction_runtime.name
}

output "transformation_runtime_role_arn" {
  description = "ARN of the transformation runtime IAM role (assumed by the transformation pipeline Lambda)."
  value       = aws_iam_role.transformation_runtime.arn
}

output "entity_resolution_runtime_role_arn" {
  description = "ARN of the entity resolution runtime IAM role (assumed by the entity resolution pipeline Lambda)."
  value       = aws_iam_role.entity_resolution_runtime.arn
}

output "analytics_publisher_runtime_role_arn" {
  description = "ARN of the analytics publisher runtime IAM role (assumed by the analytics layer publisher Lambda)."
  value       = aws_iam_role.analytics_publisher_runtime.arn
}

output "serving_store_loader_runtime_role_arn" {
  description = "ARN of the serving store loader runtime IAM role (assumed by the serving store loader Lambda)."
  value       = aws_iam_role.serving_store_loader_runtime.arn
}

output "twin_build_runtime_role_arn" {
  description = "ARN of the twin builder runtime IAM role (assumed by the twin builder Lambda)."
  value       = aws_iam_role.twin_build_runtime.arn
}

output "transformation_job_role_arn" {
  description = "ARN of the transformation job IAM role (assumed by Glue jobs)."
  value       = aws_iam_role.transformation_job.arn
}

output "orchestration_step_functions_role_arn" {
  description = "ARN of the Step Functions orchestration IAM role."
  value       = aws_iam_role.orchestration_step_functions.arn
}

output "eventbridge_scheduler_role_arn" {
  description = "ARN of the EventBridge Scheduler IAM role."
  value       = aws_iam_role.eventbridge_scheduler.arn
}

output "cicd_deployment_role_arn" {
  description = "ARN of the CI/CD GitHub Actions deployment role."
  value       = aws_iam_role.cicd_deployment.arn
}

output "pipeline_trigger_role_arn" {
  description = "ARN of the pipeline trigger Lambda IAM role."
  value       = aws_iam_role.pipeline_trigger.arn
}

output "dlq_processor_role_arn" {
  description = "ARN of the DLQ processor Lambda IAM role."
  value       = aws_iam_role.dlq_processor.arn
}

output "credential_expiry_notifier_role_arn" {
  description = "ARN of the credential expiry notifier Lambda IAM role (SEC-6)."
  value       = aws_iam_role.credential_expiry_notifier.arn
}

output "credential_expiry_scheduler_role_arn" {
  description = "ARN of the EventBridge Scheduler role that invokes the credential expiry notifier Lambda (SEC-6)."
  value       = aws_iam_role.credential_expiry_scheduler.arn
}

output "control_plane_role_arn" {
  description = "ARN of the control-plane API Lambda IAM role."
  value       = aws_iam_role.control_plane.arn
}

# ─── S8/S9 platform Lambda roles ─────────────────────────────────────────────

output "webhook_receiver_role_arn" {
  description = "Webhook receiver execution role; enqueue + signing secret only."
  value       = aws_iam_role.webhook_receiver.arn
}

output "writeback_role_arn" {
  description = "Write-back execution role; reads the -writeback secret suffix only."
  value       = aws_iam_role.writeback.arn
}

output "workflow_runner_role_arn" {
  description = "Workflow runner execution role."
  value       = aws_iam_role.workflow_runner.arn
}

output "portability_role_arn" {
  description = "Portability execution role; the only role with bulk object deletion."
  value       = aws_iam_role.portability.arn
}
