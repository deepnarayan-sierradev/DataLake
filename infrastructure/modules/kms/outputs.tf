output "key_id" {
  description = "The globally unique KMS key ID."
  value       = aws_kms_key.this.key_id
}

output "key_arn" {
  description = "The ARN of the KMS key. Use this in resource encryption configurations."
  value       = aws_kms_key.this.arn
}

output "key_alias_arn" {
  description = "The ARN of the KMS key alias."
  value       = aws_kms_alias.this.arn
}

output "key_alias_name" {
  description = "The alias name (e.g. alias/dev-storage)."
  value       = aws_kms_alias.this.name
}
