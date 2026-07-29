output "function_names" {
  description = "Deployed function names, keyed by role in the platform."
  value       = { for key, fn in aws_lambda_function.platform : key => fn.function_name }
}

output "function_arns" {
  description = "Deployed function ARNs, keyed by role in the platform."
  value       = { for key, fn in aws_lambda_function.platform : key => fn.arn }
}

output "writeback_function_name" {
  description = "Named separately because the workflow write-back action invokes it by name."
  value       = aws_lambda_function.platform["writeback"].function_name
}

output "webhook_route_enabled" {
  description = "Whether the unauthenticated webhook route was created in this environment."
  value       = var.control_plane_api_id != ""
}
