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
  common_tags   = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "entity_resolution_lambda"
  })
  function_name = "${var.environment}-entity-resolution-pipeline"
}

# ---------------------------------------------------------------------------
# CloudWatch Log Group for Lambda execution logs
#
# Created before the Lambda so Terraform manages retention and encryption.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda_execution" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = merge(local.common_tags, {
    Name    = "/aws/lambda/${local.function_name}"
    Service = "lambda"
  })
}

# ---------------------------------------------------------------------------
# Lambda Security Group
#
# Entity resolution reads curated Parquet from S3 and writes golden records
# to the analytics S3 layer — all via VPC endpoints.
# Only HTTPS (443) egress required. No ingress — invoked by Step Functions only.
# ---------------------------------------------------------------------------

data "aws_vpc" "selected" {
  filter {
    name   = "tag:Environment"
    values = [var.environment]
  }
}

resource "aws_security_group" "entity_resolution_lambda" {
  name        = "${local.function_name}-sg"
  description = "Security group for the entity resolution pipeline Lambda. HTTPS egress to AWS VPC endpoints only."
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description = "HTTPS egress - AWS VPC endpoints (S3, CloudWatch)."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${local.function_name}-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------------------
# Lambda Function — Entity Resolution Pipeline
#
# Handler: entity_resolution.entity_resolution_pipeline_handler.lambda_handler
#
# Runtime: python3.13
#
# VPC: deployed into private subnets so S3 traffic routes through the VPC
# S3 gateway endpoint rather than the public internet.
#
# Environment variables:
#   PLATFORM_ENVIRONMENT  — "dev" | "staging" | "prod"
#   CURATED_S3_BUCKET     — curated layer bucket (read: canonical Parquet + configs)
#   ANALYTICS_S3_BUCKET   — analytics layer bucket (write: golden records)
#   GOVERNANCE_S3_BUCKET  — lineage bucket (optional; empty = disabled)
#   AWS_REGION            — injected automatically by Lambda runtime
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "entity_resolution_pipeline" {
  function_name = local.function_name
  description   = "Entity resolution pipeline handler invoked by Step Functions. Loads curated records from all sources, runs match clustering and survivorship, and writes golden records to the analytics layer."

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  runtime     = "python3.13"
  handler     = "entity_resolution.entity_resolution_pipeline_handler.lambda_handler"
  role        = var.execution_role_arn
  memory_size = var.memory_size_mb
  timeout     = var.timeout_seconds

  reserved_concurrent_executions = var.reserved_concurrent_executions

  kms_key_arn = var.kms_key_arn

  environment {
    variables = {
      PLATFORM_ENVIRONMENT = var.environment
      CURATED_S3_BUCKET    = var.curated_s3_bucket_name
      ANALYTICS_S3_BUCKET  = var.analytics_s3_bucket_name
      GOVERNANCE_S3_BUCKET = var.governance_s3_bucket_name
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = concat(var.security_group_ids, [aws_security_group.entity_resolution_lambda.id])
  }

  tracing_config {
    mode = var.enable_xray_tracing ? "Active" : "PassThrough"
  }

  depends_on = [aws_cloudwatch_log_group.lambda_execution]

  tags = merge(local.common_tags, {
    Name = local.function_name
  })
}

# ---------------------------------------------------------------------------
# Lambda Permission — allow Step Functions to invoke the Lambda
#
# Rejects invocations from principals other than Step Functions in this
# account (defence-in-depth — OWASP A01).
# ---------------------------------------------------------------------------

resource "aws_lambda_permission" "allow_step_functions" {
  statement_id  = "AllowStepFunctionsInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.entity_resolution_pipeline.function_name
  principal     = "states.amazonaws.com"
  source_arn    = "arn:aws:states:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:stateMachine:${var.environment}-extraction-pipeline"
}
