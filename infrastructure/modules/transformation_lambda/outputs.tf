output "lambda_function_arn" {
  description = "ARN of the transformation pipeline Lambda function."
  value       = aws_lambda_function.transformation_pipeline.arn
}

output "lambda_function_name" {
  description = "Name of the transformation pipeline Lambda function."
  value       = aws_lambda_function.transformation_pipeline.function_name
}

output "lambda_function_invoke_arn" {
  description = "Invoke ARN of the Lambda function (used by Step Functions as Resource ARN)."
  value       = aws_lambda_function.transformation_pipeline.invoke_arn
}

output "lambda_security_group_id" {
  description = "ID of the security group attached to the transformation pipeline Lambda."
  value       = aws_security_group.transformation_lambda.id
}

output "lambda_log_group_name" {
  description = "Name of the CloudWatch Log Group for Lambda execution logs."
  value       = aws_cloudwatch_log_group.lambda_execution.name
}
