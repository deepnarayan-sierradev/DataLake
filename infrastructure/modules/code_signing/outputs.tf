output "code_signing_config_arn" {
  description = "Attach to every aws_lambda_function via code_signing_config_arn."
  value       = aws_lambda_code_signing_config.platform.arn
}

output "signing_profile_version_arn" {
  description = "Version ARN the build must sign against once Enforce is enabled."
  value       = aws_signer_signing_profile.lambda.version_arn
}
