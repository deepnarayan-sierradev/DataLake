output "entity_extraction_config_table_name" {
  description = "Name of the entity extraction config DynamoDB table."
  value       = aws_dynamodb_table.entity_extraction_config.name
}

output "entity_extraction_config_table_arn" {
  description = "ARN of the entity extraction config DynamoDB table."
  value       = aws_dynamodb_table.entity_extraction_config.arn
}

output "entity_type_registry_table_name" {
  description = "Name of the entity type registry DynamoDB table (ARCH-2)."
  value       = aws_dynamodb_table.entity_type_registry.name
}

output "entity_type_registry_table_arn" {
  description = "ARN of the entity type registry DynamoDB table (ARCH-2)."
  value       = aws_dynamodb_table.entity_type_registry.arn
}

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

output "extraction_failure_dlq_name" {
  description = "Name of the extraction failure dead-letter SQS queue (used for CloudWatch DLQ depth alarm)."
  value       = aws_sqs_queue.extraction_failure_dlq.name
}

output "serving_store_config_table_name" {
  description = "Name of the serving store config DynamoDB table."
  value       = aws_dynamodb_table.serving_store_config.name
}

output "serving_store_config_table_arn" {
  description = "ARN of the serving store config DynamoDB table."
  value       = aws_dynamodb_table.serving_store_config.arn
}

output "source_onboarding_registry_table_name" {
  description = "Name of the source onboarding registry DynamoDB table."
  value       = aws_dynamodb_table.source_onboarding_registry.name
}

output "source_onboarding_registry_table_arn" {
  description = "ARN of the source onboarding registry DynamoDB table."
  value       = aws_dynamodb_table.source_onboarding_registry.arn
}

output "twin_index_table_name" {
  description = "Name of the twin index DynamoDB table."
  value       = aws_dynamodb_table.twin_index.name
}

output "twin_index_table_arn" {
  description = "ARN of the twin index DynamoDB table."
  value       = aws_dynamodb_table.twin_index.arn
}

output "semantic_model_table_name" {
  description = "Name of the semantic model DynamoDB table."
  value       = aws_dynamodb_table.semantic_model.name
}

output "semantic_model_table_arn" {
  description = "ARN of the semantic model DynamoDB table."
  value       = aws_dynamodb_table.semantic_model.arn
}

output "saved_query_table_name" {
  description = "Name of the saved query DynamoDB table."
  value       = aws_dynamodb_table.saved_query.name
}

output "saved_query_table_arn" {
  description = "ARN of the saved query DynamoDB table."
  value       = aws_dynamodb_table.saved_query.arn
}
