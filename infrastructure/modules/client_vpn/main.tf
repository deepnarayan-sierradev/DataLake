
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

locals {
  common_tags = merge(var.tags, {
    Module    = "client_vpn"
    Component = "bi-network-path"
  })

  enabled = var.enabled ? 1 : 0
}


resource "aws_cloudwatch_log_group" "vpn" {
  count = local.enabled

  name              = "/aws/clientvpn/${var.name_prefix}-${var.environment}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn

  tags = merge(local.common_tags, { Purpose = "vpn-connection-log" })
}

resource "aws_cloudwatch_log_stream" "vpn" {
  count = local.enabled

  name           = "connections"
  log_group_name = aws_cloudwatch_log_group.vpn[0].name
}

resource "aws_security_group" "vpn_clients" {
  count = local.enabled

  name        = "${var.name_prefix}-vpn-clients-${var.environment}-sg"
  description = "Client VPN association SG; the only non-SG source the serving store accepts."
  vpc_id      = var.vpc_id

  egress {
    description = "Serving-store database access from a connected VPN client."
    from_port   = var.serving_store_port
    to_port     = var.serving_store_port
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
  }

  egress {
    description = "DNS resolution inside the VPC."
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = var.private_subnet_cidrs
  }

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-vpn-clients-${var.environment}-sg" })
}

resource "aws_ec2_client_vpn_endpoint" "serving_store" {
  count = local.enabled

  description            = "BI access to the ${var.environment} serving store (DL-SERV-01)."
  server_certificate_arn = var.server_certificate_arn
  client_cidr_block      = var.client_cidr_block
  split_tunnel           = true
  vpc_id                 = var.vpc_id
  security_group_ids     = [aws_security_group.vpn_clients[0].id]
  session_timeout_hours  = var.session_timeout_hours
  transport_protocol     = "udp"

  authentication_options {
    type                       = "certificate-authentication"
    root_certificate_chain_arn = var.client_root_certificate_arn
  }

  connection_log_options {
    enabled               = true
    cloudwatch_log_group  = aws_cloudwatch_log_group.vpn[0].name
    cloudwatch_log_stream = aws_cloudwatch_log_stream.vpn[0].name
  }

  dns_servers = var.dns_servers

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-serving-store-vpn-${var.environment}" })
}

resource "aws_ec2_client_vpn_network_association" "private" {
  count = var.enabled ? length(var.private_subnet_ids) : 0

  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.serving_store[0].id
  subnet_id              = var.private_subnet_ids[count.index]
}


resource "aws_ec2_client_vpn_authorization_rule" "per_tenant" {
  for_each = var.enabled ? var.tenant_access_groups : {}

  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.serving_store[0].id
  target_network_cidr    = each.value
  access_group_id        = each.key
  description            = "Serving-store access for tenant access group ${each.key}."
}

resource "aws_ec2_client_vpn_route" "serving_store" {
  count = var.enabled ? length(var.private_subnet_ids) : 0

  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.serving_store[0].id
  destination_cidr_block = var.private_subnet_cidrs[count.index]
  target_vpc_subnet_id   = var.private_subnet_ids[count.index]
  description            = "Route to the private subnet holding the serving store."
}


resource "aws_cloudwatch_metric_alarm" "certificate_expiry" {
  count = var.enabled && var.alarm_sns_topic_arn != "" ? 1 : 0

  alarm_name          = "${var.name_prefix}-vpn-certificate-expiry-${var.environment}"
  namespace           = "AWS/CertificateManager"
  metric_name         = "DaysToExpiry"
  statistic           = "Minimum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = var.certificate_expiry_warning_days
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    CertificateArn = var.server_certificate_arn
  }

  alarm_description = join(" ", [
    "The Client VPN server certificate expires within ${var.certificate_expiry_warning_days}",
    "days. Every tenant's BI access stops when it does, so renew before the window closes.",
  ])

  alarm_actions = [var.alarm_sns_topic_arn]

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "concurrent_connections" {
  count = var.enabled && var.alarm_sns_topic_arn != "" ? 1 : 0

  alarm_name          = "${var.name_prefix}-vpn-concurrent-connections-${var.environment}"
  namespace           = "AWS/ClientVPN"
  metric_name         = "ActiveConnectionsCount"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.concurrent_connection_warning
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    Endpoint = aws_ec2_client_vpn_endpoint.serving_store[0].id
  }

  alarm_description = join(" ", [
    "Concurrent VPN connections exceeded ${var.concurrent_connection_warning}. §11 forbids",
    "throttling included capabilities, so this is a signal to size up rather than to limit.",
  ])

  alarm_actions = [var.alarm_sns_topic_arn]

  tags = local.common_tags
}
