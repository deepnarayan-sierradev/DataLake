output "lambda_function_arn" {
  description = "ARN of the entity resolution pipeline Lambda function."
  value       = aws_lambda_function.entity_resolution_pipeline.arn
}

output "lambda_function_name" {
  description = "Name of the entity resolution pipeline Lambda function."
  value       = aws_lambda_function.entity_resolution_pipeline.function_name
}

output "lambda_function_invoke_arn" {
  description = "Invoke ARN of the Lambda function (used by Step Functions as Resource ARN)."
  value       = aws_lambda_function.entity_resolution_pipeline.invoke_arn
}

output "lambda_security_group_id" {
  description = "ID of the security group attached to the entity resolution pipeline Lambda."
  value       = aws_security_group.entity_resolution_lambda.id
}

output "lambda_log_group_name" {
  description = "Name of the CloudWatch Log Group for Lambda execution logs."
  value       = aws_cloudwatch_log_group.lambda_execution.name
}
