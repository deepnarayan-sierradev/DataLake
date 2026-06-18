output "extraction_runtime_role_arn" {
  description = "ARN of the extraction runtime IAM role."
  value       = aws_iam_role.extraction_runtime.arn
}

output "extraction_runtime_role_name" {
  description = "Name of the extraction runtime IAM role."
  value       = aws_iam_role.extraction_runtime.name
}

output "transformation_job_role_arn" {
  description = "ARN of the transformation job IAM role."
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
