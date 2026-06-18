terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  common_tags = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "networking"
  })
}

# ---------------------------------------------------------------------------
# VPC
# ---------------------------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge(local.common_tags, {
    Name = "${var.environment}-data-lake-vpc"
  })
}

# ---------------------------------------------------------------------------
# Internet Gateway (for NAT Gateway egress; no public compute allowed)
# ---------------------------------------------------------------------------

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "${var.environment}-data-lake-igw"
  })
}

# ---------------------------------------------------------------------------
# Subnets
# Private: all data lake compute. Public: NAT gateways only.
# ---------------------------------------------------------------------------

resource "aws_subnet" "private" {
  count = length(var.private_subnet_cidrs)

  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.private_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = false # Never assign public IPs in private subnets

  tags = merge(local.common_tags, {
    Name = "${var.environment}-data-lake-private-${var.availability_zones[count.index]}"
    Tier = "private"
  })
}

resource "aws_subnet" "public" {
  count = length(var.public_subnet_cidrs)

  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = false # NAT GW EIPs are explicit; no auto-assignment

  tags = merge(local.common_tags, {
    Name = "${var.environment}-data-lake-public-${var.availability_zones[count.index]}"
    Tier = "public"
  })
}

# ---------------------------------------------------------------------------
# Elastic IPs and NAT Gateways
# dev: 1 NAT GW (cost optimised). staging/prod: 1 per AZ (HA).
# ---------------------------------------------------------------------------

resource "aws_eip" "nat" {
  count  = var.single_nat_gateway ? 1 : length(var.availability_zones)
  domain = "vpc"

  tags = merge(local.common_tags, {
    Name = "${var.environment}-data-lake-nat-eip-${count.index + 1}"
  })

  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  count = var.single_nat_gateway ? 1 : length(var.availability_zones)

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(local.common_tags, {
    Name = "${var.environment}-data-lake-nat-gw-${count.index + 1}"
  })

  depends_on = [aws_internet_gateway.this]
}

# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

# Public route table — routes to IGW
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(local.common_tags, {
    Name = "${var.environment}-data-lake-public-rt"
  })
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private route tables — route to NAT GW (one per AZ in HA mode)
resource "aws_route_table" "private" {
  count  = var.single_nat_gateway ? 1 : length(var.availability_zones)
  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[var.single_nat_gateway ? 0 : count.index].id
  }

  tags = merge(local.common_tags, {
    Name = "${var.environment}-data-lake-private-rt-${count.index + 1}"
  })
}

resource "aws_route_table_association" "private" {
  count     = length(aws_subnet.private)
  subnet_id = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[
    var.single_nat_gateway ? 0 : count.index
  ].id
}

# ---------------------------------------------------------------------------
# VPC Flow Logs — mandatory for security monitoring and forensics
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "vpc_flow_logs" {
  name              = "/aws/vpc/flowlogs/${var.environment}-data-lake"
  retention_in_days = var.flow_log_retention_days
  kms_key_id        = var.flow_logs_kms_key_arn

  tags = local.common_tags
}

data "aws_iam_policy_document" "vpc_flow_logs_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "vpc_flow_logs" {
  name               = "${var.environment}-vpc-flow-logs-delivery-role"
  assume_role_policy = data.aws_iam_policy_document.vpc_flow_logs_assume_role.json

  tags = local.common_tags
}

data "aws_iam_policy_document" "vpc_flow_logs_delivery" {
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
    ]
    resources = [
      "${aws_cloudwatch_log_group.vpc_flow_logs.arn}",
      "${aws_cloudwatch_log_group.vpc_flow_logs.arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "vpc_flow_logs_delivery" {
  name   = "vpc-flow-logs-delivery-policy"
  role   = aws_iam_role.vpc_flow_logs.id
  policy = data.aws_iam_policy_document.vpc_flow_logs_delivery.json
}

resource "aws_flow_log" "this" {
  iam_role_arn    = aws_iam_role.vpc_flow_logs.arn
  log_destination = aws_cloudwatch_log_group.vpc_flow_logs.arn
  traffic_type    = "ALL" # Capture ACCEPT, REJECT, and all traffic
  vpc_id          = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "${var.environment}-data-lake-vpc-flow-log"
  })
}

# ---------------------------------------------------------------------------
# Security Group for VPC Interface Endpoints
# Allow only HTTPS (443) inbound from within the VPC CIDR
# ---------------------------------------------------------------------------

resource "aws_security_group" "vpc_endpoints" {
  name        = "${var.environment}-vpc-endpoint-sg"
  description = "Allow HTTPS inbound from VPC CIDR for interface VPC endpoints"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS from VPC CIDR only"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # Egress: allow HTTPS outbound to VPC CIDR only.
  # Interface endpoints respond on port 443 back into the VPC — the SG
  # must allow this return traffic outbound. Limiting to VPC CIDR prevents
  # the endpoint SG from being used to reach the public internet.
  egress {
    description = "HTTPS outbound to VPC CIDR (endpoint responses)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = merge(local.common_tags, {
    Name = "${var.environment}-vpc-endpoint-sg"
  })
}

# ---------------------------------------------------------------------------
# VPC Gateway Endpoints (free — no hourly charge)
# S3 and DynamoDB use Gateway endpoints for private routing
# ---------------------------------------------------------------------------

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = concat(
    aws_route_table.private[*].id,
    [aws_route_table.public.id]
  )

  tags = merge(local.common_tags, {
    Name = "${var.environment}-s3-gateway-endpoint"
  })
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = merge(local.common_tags, {
    Name = "${var.environment}-dynamodb-gateway-endpoint"
  })
}

# ---------------------------------------------------------------------------
# VPC Interface Endpoints (charged — conditional on var flags for cost control)
# ---------------------------------------------------------------------------

locals {
  interface_endpoints = {
    secretsmanager = {
      service      = "com.amazonaws.${data.aws_region.current.name}.secretsmanager"
      enabled      = var.enable_secrets_manager_endpoint
      private_dns  = true
    }
    logs = {
      service     = "com.amazonaws.${data.aws_region.current.name}.logs"
      enabled     = var.enable_cloudwatch_logs_endpoint
      private_dns = true
    }
    monitoring = {
      service     = "com.amazonaws.${data.aws_region.current.name}.monitoring"
      enabled     = var.enable_cloudwatch_monitoring_endpoint
      private_dns = true
    }
    states = {
      service     = "com.amazonaws.${data.aws_region.current.name}.states"
      enabled     = var.enable_step_functions_endpoint
      private_dns = true
    }
    glue = {
      service     = "com.amazonaws.${data.aws_region.current.name}.glue"
      enabled     = var.enable_glue_endpoint
      private_dns = true
    }
    kms = {
      service     = "com.amazonaws.${data.aws_region.current.name}.kms"
      enabled     = var.enable_kms_endpoint
      private_dns = true
    }
  }

  enabled_interface_endpoints = {
    for name, cfg in local.interface_endpoints : name => cfg if cfg.enabled
  }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.enabled_interface_endpoints

  vpc_id              = aws_vpc.this.id
  service_name        = each.value.service
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = each.value.private_dns

  tags = merge(local.common_tags, {
    Name = "${var.environment}-${each.key}-interface-endpoint"
  })
}
