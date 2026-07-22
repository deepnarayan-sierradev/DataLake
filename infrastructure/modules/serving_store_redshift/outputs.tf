output "namespace_name" {
  description = "Redshift Serverless namespace name."
  value       = aws_redshiftserverless_namespace.serving_store.namespace_name
}

output "workgroup_name" {
  description = "Redshift Serverless workgroup name (used for redshift-serverless:GetCredentials)."
  value       = aws_redshiftserverless_workgroup.serving_store.workgroup_name
}

output "workgroup_arn" {
  description = "ARN of the Redshift Serverless workgroup (scope target for the loader's IAM auth)."
  value       = aws_redshiftserverless_workgroup.serving_store.arn
}

output "endpoint_address" {
  description = "Workgroup endpoint hostname."
  value       = aws_redshiftserverless_workgroup.serving_store.endpoint[0].address
}

output "endpoint_port" {
  description = "Workgroup endpoint port."
  value       = aws_redshiftserverless_workgroup.serving_store.endpoint[0].port
}

output "security_group_id" {
  description = "ID of the security group attached to the Redshift Serverless workgroup."
  value       = aws_security_group.redshift.id
}

output "copy_iam_role_arn" {
  description = "ARN of the IAM role Redshift assumes for COPY from the analytics bucket."
  value       = aws_iam_role.copy.arn
}

output "connection_secret_arn" {
  description = "Secrets Manager ARN holding the redshift connection metadata (no password)."
  value       = aws_secretsmanager_secret.connection.arn
}
