output "platform_alerts_topic_arn" {
  description = "ARN of the SNS topic that receives CloudWatch alarm notifications."
  value       = aws_sns_topic.platform_alerts.arn
}

output "log_group_arns" {
  description = "Map of service name to CloudWatch log group ARN."
  value       = { for k, v in aws_cloudwatch_log_group.platform_services : k => v.arn }
}

output "log_group_names" {
  description = "Map of service name to CloudWatch log group name."
  value       = { for k, v in aws_cloudwatch_log_group.platform_services : k => v.name }
}

output "xray_group_arn" {
  description = "ARN of the X-Ray tracing group for the platform."
  value       = aws_xray_group.platform.arn
}

output "extraction_activity_absent_alarm_arn" {
  description = "ARN of the alarm that fires when no extraction records have been emitted (silent pipeline failure detection)."
  value       = aws_cloudwatch_metric_alarm.extraction_activity_absent.arn
}

output "transformation_quality_blocked_alarm_arn" {
  description = "ARN of the alarm that fires when quality policy blocking violations halt curated publication."
  value       = aws_cloudwatch_metric_alarm.transformation_quality_blocked.arn
}

output "serving_store_load_failures_alarm_arn" {
  description = "ARN of the alarm that fires when serving store load errors are detected."
  value       = aws_cloudwatch_metric_alarm.serving_store_load_failures.arn
}

output "extraction_slo_dashboard_arn" {
  description = "ARN of the extraction SLO CloudWatch dashboard."
  value       = aws_cloudwatch_dashboard.extraction_slo.dashboard_arn
}

output "transformation_slo_dashboard_arn" {
  description = "ARN of the transformation SLO CloudWatch dashboard."
  value       = aws_cloudwatch_dashboard.transformation_slo.dashboard_arn
}

output "serving_slo_dashboard_arn" {
  description = "ARN of the serving store SLO CloudWatch dashboard."
  value       = aws_cloudwatch_dashboard.serving_slo.dashboard_arn
}
