output "log_group_name" {
  description = "CloudWatch log group receiving trail events; the IAM module's metric filter reads it."
  value       = aws_cloudwatch_log_group.trail.name
}

output "trail_arn" {
  description = "ARN of the platform audit trail."
  value       = aws_cloudtrail.platform.arn
}

output "trail_bucket_arn" {
  description = "ARN of the bucket holding the long-term audit record."
  value       = aws_s3_bucket.trail.arn
}
