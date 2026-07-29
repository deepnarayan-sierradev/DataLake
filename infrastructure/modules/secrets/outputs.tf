output "salesforce_credentials_secret_arn" {
  description = "ARN of the Salesforce credentials secret."
  value       = aws_secretsmanager_secret.salesforce_credentials.arn
}

output "netsuite_credentials_secret_arn" {
  description = "ARN of the NetSuite credentials secret."
  value       = aws_secretsmanager_secret.netsuite_credentials.arn
}

output "mysql_rds_credentials_secret_arn" {
  description = "ARN of the MySQL RDS credentials secret."
  value       = aws_secretsmanager_secret.mysql_rds_credentials.arn
}

output "sage_intacct_credentials_secret_arn" {
  description = "ARN of the Sage Intacct credentials secret."
  value       = aws_secretsmanager_secret.sage_intacct_credentials.arn
}

output "sage_x3_credentials_secret_arn" {
  description = "ARN of the Sage X3 credentials secret."
  value       = aws_secretsmanager_secret.sage_x3_credentials.arn
}

output "all_source_credential_secret_arns" {
  description = "ARNs of every source-credential secret this module manages (SEC-6 expiry notifier input)."
  value = [
    aws_secretsmanager_secret.salesforce_credentials.arn,
    aws_secretsmanager_secret.netsuite_credentials.arn,
    aws_secretsmanager_secret.mysql_rds_credentials.arn,
    aws_secretsmanager_secret.sage_intacct_credentials.arn,
    aws_secretsmanager_secret.sage_x3_credentials.arn,
  ]
}
