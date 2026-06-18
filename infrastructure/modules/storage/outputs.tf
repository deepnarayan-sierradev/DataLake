output "raw_layer_bucket_id" {
  description = "Name of the raw layer S3 bucket."
  value       = aws_s3_bucket.raw_layer.id
}

output "raw_layer_bucket_arn" {
  description = "ARN of the raw layer S3 bucket."
  value       = aws_s3_bucket.raw_layer.arn
}

output "curated_layer_bucket_id" {
  description = "Name of the curated layer S3 bucket."
  value       = aws_s3_bucket.curated_layer.id
}

output "curated_layer_bucket_arn" {
  description = "ARN of the curated layer S3 bucket."
  value       = aws_s3_bucket.curated_layer.arn
}

output "analytics_layer_bucket_id" {
  description = "Name of the analytics layer S3 bucket."
  value       = aws_s3_bucket.analytics_layer.id
}

output "analytics_layer_bucket_arn" {
  description = "ARN of the analytics layer S3 bucket."
  value       = aws_s3_bucket.analytics_layer.arn
}

output "schema_snapshots_bucket_id" {
  description = "Name of the schema snapshots S3 bucket."
  value       = aws_s3_bucket.schema_snapshots.id
}

output "schema_snapshots_bucket_arn" {
  description = "ARN of the schema snapshots S3 bucket."
  value       = aws_s3_bucket.schema_snapshots.arn
}

output "access_logs_bucket_id" {
  description = "Name of the S3 access logs bucket."
  value       = aws_s3_bucket.access_logs.id
}
