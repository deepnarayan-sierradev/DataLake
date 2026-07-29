output "endpoint_id" {
  description = "Client VPN endpoint id, or empty when the module is disabled."
  value       = var.enabled ? aws_ec2_client_vpn_endpoint.serving_store[0].id : ""
}

output "endpoint_dns_name" {
  description = "DNS name a BI gateway connects to, or empty when disabled."
  value       = var.enabled ? aws_ec2_client_vpn_endpoint.serving_store[0].dns_name : ""
}

output "client_security_group_id" {
  description = <<-EOT
    SG the serving-store security group should reference as its only non-SG inbound source.

    Empty when disabled, which is why the serving-store SG must treat an empty value as
    "no VPN path yet" rather than as an open rule.
  EOT
  value       = var.enabled ? aws_security_group.vpn_clients[0].id : ""
}

output "connection_log_group_name" {
  description = "Log group holding the serving-store access audit trail."
  value       = var.enabled ? aws_cloudwatch_log_group.vpn[0].name : ""
}

output "is_enabled" {
  description = "Whether a BI network path exists. False means gap register item 4 is still open."
  value       = var.enabled
}
