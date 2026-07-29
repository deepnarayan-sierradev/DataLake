output "web_acl_arn" {
  description = "ARN of the control-plane Web ACL."
  value       = aws_wafv2_web_acl.control_plane.arn
}

output "web_acl_name" {
  description = "Name of the control-plane Web ACL."
  value       = aws_wafv2_web_acl.control_plane.name
}

output "security_log_group_name" {
  description = "Name of the dedicated WAF security log group (extended retention)."
  value       = aws_cloudwatch_log_group.waf.name
}

output "enforcement_mode" {
  description = "Whether the ACL is counting or blocking; audit until verified clean."
  value       = var.enforcement_mode
}
