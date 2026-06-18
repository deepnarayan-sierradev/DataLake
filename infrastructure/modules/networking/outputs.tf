output "vpc_id" {
  description = "The ID of the data lake VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "The CIDR block of the VPC."
  value       = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  description = "IDs of the private subnets. Use these for all data lake compute resources."
  value       = aws_subnet.private[*].id
}

output "public_subnet_ids" {
  description = "IDs of the public subnets. Used only for NAT Gateways."
  value       = aws_subnet.public[*].id
}

output "vpc_endpoint_security_group_id" {
  description = "Security group ID for VPC interface endpoints."
  value       = aws_security_group.vpc_endpoints.id
}

output "s3_vpc_endpoint_id" {
  description = "ID of the S3 Gateway VPC endpoint."
  value       = aws_vpc_endpoint.s3.id
}

output "dynamodb_vpc_endpoint_id" {
  description = "ID of the DynamoDB Gateway VPC endpoint."
  value       = aws_vpc_endpoint.dynamodb.id
}

output "interface_endpoint_ids" {
  description = "Map of interface endpoint name to endpoint ID."
  value       = { for k, v in aws_vpc_endpoint.interface : k => v.id }
}

output "nat_gateway_public_ips" {
  description = "Elastic IP addresses of the NAT Gateways. Add to Salesforce/NetSuite IP allowlists."
  value       = aws_eip.nat[*].public_ip
}
