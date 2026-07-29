# ---------------------------------------------------------------------------
# AWS Client VPN for BI access to the serving store (DL-SERV-01).
#
# The recommendation on file: Client VPN with **per-tenant client certificates**, paired with
# Power BI On-premises Data Gateway or Tableau Bridge running as the VPN client. It keeps the
# database fully private, matches how those tools already reach private data, and supports
# self-service onboarding.
#
# Rejected alternatives, recorded here so the decision is not re-litigated from scratch:
#   - site-to-site VPN / Direct Connect: too heavy for self-service; viable only for a large
#     tenant that already runs one.
#   - PrivateLink alone: good for an AWS-native tenant, useless for a laptop running BI Desktop.
#     May be added later as a complement, not a substitute.
#   - public instance with IP allowlists: fastest, but widens the attack surface and is fragile
#     against BI vendors' dynamic egress ranges.
#
# `enabled = false` by default: DL-SERV-01 needs a **customer decision** on topology, and
# provisioning a VPN endpoint before that decision bills hourly for something nobody connects
# to. The module is complete and validated so the decision is the only remaining blocker.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Endpoint. Mutual (certificate) authentication, so a tenant's access is revoked by revoking
# its certificate — per-tenant and individually revocable (OWASP A07).
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "vpn" {
  count = local.enabled

  name              = "/aws/clientvpn/${var.environment}-edl"
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

  name        = "${var.environment}-edl-vpn-clients"
  description = "Client VPN association SG; the only non-SG source the serving store accepts."
  vpc_id      = var.vpc_id

  # No inbound rules: a VPN client initiates outbound connections to the database, and the
  # database's own SG references this group. An inbound rule here would be unused surface.
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

  tags = merge(local.common_tags, { Name = "${var.environment}-edl-vpn-clients" })
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

  # Mutual authentication only. A federated or AD option would move revocation out of this
  # account, which is the opposite of what per-tenant revocability requires.
  authentication_options {
    type                       = "certificate-authentication"
    root_certificate_chain_arn = var.client_root_certificate_arn
  }

  connection_log_options {
    enabled              = true
    cloudwatch_log_group = aws_cloudwatch_log_group.vpn[0].name
    # Connection logs are the audit trail for who reached the serving store and when.
    cloudwatch_log_stream = aws_cloudwatch_log_stream.vpn[0].name
  }

  dns_servers = var.dns_servers

  tags = merge(local.common_tags, { Name = "${var.environment}-edl-serving-store-vpn" })
}

resource "aws_ec2_client_vpn_network_association" "private" {
  count = var.enabled ? length(var.private_subnet_ids) : 0

  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.serving_store[0].id
  subnet_id              = var.private_subnet_ids[count.index]
}

# ---------------------------------------------------------------------------
# Authorization. Per-tenant certificate common names map to access-group ids, so a tenant's
# rule can be removed without touching any other tenant's access.
#
# Deliberately NOT `authorize_all_groups = true`: that would make one issued certificate
# sufficient to reach the whole VPC, which is exactly the cross-tenant reach the serving
# store's database-per-tenant model exists to prevent.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Certificate-expiry monitoring (DL-SERV-01's observability clause). A tenant whose
# certificate silently expires loses BI access with no explanation, so expiry is watched
# rather than discovered.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "certificate_expiry" {
  count = var.enabled && var.alarm_sns_topic_arn != "" ? 1 : 0

  alarm_name          = "${var.environment}-edl-vpn-certificate-expiry"
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

  alarm_name          = "${var.environment}-edl-vpn-concurrent-connections"
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
