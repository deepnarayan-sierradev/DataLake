output "api_invoke_url" {
  description = "Invoke URL of the control-plane HTTP API ($default stage)."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "api_id" {
  description = "ID of the control-plane HTTP API."
  value       = aws_apigatewayv2_api.control_plane.id
}

output "cognito_user_pool_id" {
  description = "ID of the control-plane Cognito User Pool."
  value       = aws_cognito_user_pool.control_plane.id
}

output "cognito_user_pool_arn" {
  description = "ARN of the control-plane Cognito User Pool."
  value       = aws_cognito_user_pool.control_plane.arn
}

output "cognito_app_client_id" {
  description = "ID of the control-plane Cognito User Pool App Client."
  value       = aws_cognito_user_pool_client.control_plane.id
}

output "control_plane_lambda_arn" {
  description = "ARN of the control-plane Lambda function."
  value       = aws_lambda_function.control_plane.arn
}

output "control_plane_lambda_name" {
  description = "Name of the control-plane Lambda function."
  value       = aws_lambda_function.control_plane.function_name
}
