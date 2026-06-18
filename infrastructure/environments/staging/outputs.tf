output "vpc_id" {
  description = "Dev VPC ID."
  value       = module.networking.vpc_id
}

output "private_subnet_ids" {
  description = "Dev private subnet IDs."
  value       = module.networking.private_subnet_ids
}

output "nat_gateway_public_ips" {
  description = "NAT Gateway public IPs. Add to Salesforce/NetSuite IP allowlists."
  value       = module.networking.nat_gateway_public_ips
}

output "raw_layer_bucket_id" {
  description = "Raw layer S3 bucket name."
  value       = module.storage.raw_layer_bucket_id
}

output "curated_layer_bucket_id" {
  description = "Curated layer S3 bucket name."
  value       = module.storage.curated_layer_bucket_id
}

output "analytics_layer_bucket_id" {
  description = "Analytics layer S3 bucket name."
  value       = module.storage.analytics_layer_bucket_id
}

output "watermark_repository_table_name" {
  description = "Watermark repository DynamoDB table name."
  value       = module.metadata_persistence.watermark_repository_table_name
}

output "run_audit_log_table_name" {
  description = "Run audit log DynamoDB table name."
  value       = module.metadata_persistence.run_audit_log_table_name
}

output "extraction_failure_dlq_url" {
  description = "Extraction failure DLQ URL."
  value       = module.metadata_persistence.extraction_failure_dlq_url
}

output "extraction_runtime_role_arn" {
  description = "Extraction runtime IAM role ARN."
  value       = module.iam.extraction_runtime_role_arn
}

output "salesforce_credentials_secret_arn" {
  description = "Salesforce credentials Secrets Manager ARN."
  value       = module.secrets.salesforce_credentials_secret_arn
}

output "platform_alerts_topic_arn" {
  description = "SNS platform alerts topic ARN."
  value       = module.observability.platform_alerts_topic_arn
}

output "state_machine_arn" {
  description = "Full pipeline Step Functions state machine ARN."
  value       = module.orchestration.state_machine_arn
}

output "state_machine_name" {
  description = "Full pipeline Step Functions state machine name."
  value       = module.orchestration.state_machine_name
}

output "schedule_group_name" {
  description = "EventBridge Scheduler schedule group name for extraction entities."
  value       = module.orchestration.schedule_group_name
}
