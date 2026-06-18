output "lambda_function_arn" {
  description = "ARN of the extraction pipeline Lambda function."
  value       = aws_lambda_function.extraction_pipeline.arn
}

output "lambda_function_name" {
  description = "Name of the extraction pipeline Lambda function."
  value       = aws_lambda_function.extraction_pipeline.function_name
}

output "lambda_function_invoke_arn" {
  description = "Invoke ARN of the Lambda function (used by Step Functions as Resource ARN)."
  value       = aws_lambda_function.extraction_pipeline.invoke_arn
}

output "lambda_security_group_id" {
  description = "ID of the security group attached to the extraction pipeline Lambda."
  value       = aws_security_group.lambda_pipeline.id
}

output "lambda_log_group_name" {
  description = "Name of the CloudWatch Log Group for Lambda execution logs."
  value       = aws_cloudwatch_log_group.lambda_execution.name
}
