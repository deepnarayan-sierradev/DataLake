output "state_machine_arn" {
  description = "ARN of the Step Functions full pipeline state machine."
  value       = aws_sfn_state_machine.extraction_pipeline.arn
}

output "state_machine_name" {
  description = "Name of the Step Functions full pipeline state machine."
  value       = aws_sfn_state_machine.extraction_pipeline.name
}

output "schedule_group_name" {
  description = "Name of the EventBridge Scheduler schedule group for extraction entities."
  value       = aws_scheduler_schedule_group.extraction_schedules.name
}

output "schedule_group_arn" {
  description = "ARN of the EventBridge Scheduler schedule group."
  value       = aws_scheduler_schedule_group.extraction_schedules.arn
}

output "sfn_log_group_name" {
  description = "Name of the CloudWatch Log Group capturing Step Functions execution history."
  value       = aws_cloudwatch_log_group.sfn_execution.name
}

output "sfn_log_group_arn" {
  description = "ARN of the CloudWatch Log Group capturing Step Functions execution history."
  value       = aws_cloudwatch_log_group.sfn_execution.arn
}

output "pipeline_execution_failures_alarm_arn" {
  description = "ARN of the CloudWatch alarm that fires when any pipeline execution fails."
  value       = aws_cloudwatch_metric_alarm.sfn_execution_failures.arn
}

output "pipeline_executions_throttled_alarm_arn" {
  description = "ARN of the CloudWatch alarm that fires when Step Functions executions are throttled."
  value       = aws_cloudwatch_metric_alarm.sfn_executions_throttled.arn
}
