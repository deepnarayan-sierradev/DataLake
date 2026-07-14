output "db_instance_identifier" {
  description = "Identifier of the serving store RDS instance."
  value       = aws_db_instance.serving_store.identifier
}

output "db_instance_endpoint" {
  description = "Connection endpoint (host:port) of the serving store RDS instance."
  value       = aws_db_instance.serving_store.endpoint
}

output "db_instance_address" {
  description = "Hostname (no port) of the serving store RDS instance."
  value       = aws_db_instance.serving_store.address
}

output "db_instance_port" {
  description = "Port of the serving store RDS instance."
  value       = aws_db_instance.serving_store.port
}

output "master_user_secret_arn" {
  description = "Secrets Manager ARN of the AWS-managed master user credential."
  value       = aws_db_instance.serving_store.master_user_secret[0].secret_arn
}

output "security_group_id" {
  description = "ID of the security group attached to the serving store RDS instance."
  value       = aws_security_group.serving_store_database.id
}
