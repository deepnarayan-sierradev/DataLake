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
