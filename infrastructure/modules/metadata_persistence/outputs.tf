output "watermark_repository_table_name" {
  description = "Name of the watermark repository DynamoDB table."
  value       = aws_dynamodb_table.watermark_repository.name
}

output "watermark_repository_table_arn" {
  description = "ARN of the watermark repository DynamoDB table."
  value       = aws_dynamodb_table.watermark_repository.arn
}

output "run_audit_log_table_name" {
  description = "Name of the run audit log DynamoDB table."
  value       = aws_dynamodb_table.run_audit_log.name
}

output "run_audit_log_table_arn" {
  description = "ARN of the run audit log DynamoDB table."
  value       = aws_dynamodb_table.run_audit_log.arn
}

output "extraction_failure_dlq_url" {
  description = "URL of the extraction failure dead-letter SQS queue."
  value       = aws_sqs_queue.extraction_failure_dlq.id
}

output "extraction_failure_dlq_arn" {
  description = "ARN of the extraction failure dead-letter SQS queue."
  value       = aws_sqs_queue.extraction_failure_dlq.arn
}

output "source_onboarding_registry_table_name" {
  description = "Name of the source onboarding registry DynamoDB table."
  value       = aws_dynamodb_table.source_onboarding_registry.name
}

output "source_onboarding_registry_table_arn" {
  description = "ARN of the source onboarding registry DynamoDB table."
  value       = aws_dynamodb_table.source_onboarding_registry.arn
}
