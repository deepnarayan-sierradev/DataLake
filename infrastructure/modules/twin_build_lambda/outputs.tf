output "lambda_function_arn" {
  description = "ARN of the twin builder Lambda function."
  value       = aws_lambda_function.twin_builder.arn
}

output "lambda_function_name" {
  description = "Name of the twin builder Lambda function."
  value       = aws_lambda_function.twin_builder.function_name
}

output "lambda_function_invoke_arn" {
  description = "Invoke ARN of the Lambda function (used by Step Functions as Resource ARN)."
  value       = aws_lambda_function.twin_builder.invoke_arn
}

output "lambda_log_group_name" {
  description = "Name of the CloudWatch Log Group for Lambda execution logs."
  value       = aws_cloudwatch_log_group.lambda_execution.name
}
