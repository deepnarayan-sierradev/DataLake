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
    Module      = "transformation_lambda"
  })
  function_name = "${var.environment}-transformation-pipeline"
}

# ---------------------------------------------------------------------------
# CloudWatch Log Group for Lambda execution logs
#
# Created before the Lambda so Terraform manages retention and encryption.
# If Lambda creates the log group automatically it inherits no retention
# and no KMS encryption.
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
# Transformation reads raw Parquet from S3 and writes to the curated layer —
# all via VPC endpoints.  Only HTTPS (443) egress is required.
# No ingress — Lambda is invoked by Step Functions only.
# ---------------------------------------------------------------------------

data "aws_vpc" "selected" {
  filter {
    name   = "tag:Environment"
    values = [var.environment]
  }
}

resource "aws_security_group" "transformation_lambda" {
  name        = "${local.function_name}-sg"
  description = "Security group for the transformation pipeline Lambda. HTTPS egress to AWS service endpoints only."
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description = "HTTPS egress - AWS VPC endpoints (S3, Glue, CloudWatch)."
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
# Lambda Function — Transformation Pipeline
#
# Handler: transformation.transformation_pipeline_handler.lambda_handler
#
# Runtime: python3.13 (closest GA runtime to 3.14 at time of writing;
# update to python3.14 when AWS adds it to Lambda runtimes).
#
# VPC: deployed into private subnets so S3 traffic routes through the VPC
# S3 gateway endpoint rather than the public internet.  Glue and CloudWatch
# API calls go through the corresponding interface VPC endpoints.
#
# Environment variables:
#   PLATFORM_ENVIRONMENT      — "dev" | "staging" | "prod"
#   RAW_S3_BUCKET             — raw layer bucket name (read-only)
#   CURATED_S3_BUCKET         — curated layer bucket name (read + write)
#   FIELD_MAPPING_S3_BUCKET   — bucket storing field mapping JSON files
#   GOVERNANCE_S3_BUCKET      — lineage bucket (optional; empty = disabled)
#   GLUE_CATALOG_DATABASE     — Glue database (optional; empty = disabled)
#   AWS_REGION                — injected automatically by Lambda runtime
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "transformation_pipeline" {
  function_name = local.function_name
  description   = "Transformation pipeline handler invoked by Step Functions. Applies field mappings, evaluates quality, and writes to the curated layer."

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  runtime     = "python3.13"
  handler     = "transformation.transformation_pipeline_handler.lambda_handler"
  role        = var.execution_role_arn
  memory_size = var.memory_size_mb
  timeout     = var.timeout_seconds

  reserved_concurrent_executions = var.reserved_concurrent_executions

  kms_key_arn = var.kms_key_arn

  environment {
    variables = {
      PLATFORM_ENVIRONMENT      = var.environment
      RAW_S3_BUCKET             = var.raw_s3_bucket_name
      CURATED_S3_BUCKET         = var.curated_s3_bucket_name
      FIELD_MAPPING_S3_BUCKET   = var.field_mapping_s3_bucket_name
      GOVERNANCE_S3_BUCKET      = var.governance_s3_bucket_name
      GLUE_CATALOG_DATABASE     = var.glue_catalog_database
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = concat(var.security_group_ids, [aws_security_group.transformation_lambda.id])
  }

  tracing_config {
    mode = var.enable_xray_tracing ? "Active" : "PassThrough"
  }

  # Ensure log group exists and is owned by Terraform before Lambda starts.
  depends_on = [aws_cloudwatch_log_group.lambda_execution]

  tags = merge(local.common_tags, {
    Name = local.function_name
  })

  lifecycle {
    # Prevent accidental destruction of the function in staging/prod.
    # destroy is still possible via targeted apply.
    ignore_changes = []
  }
}

# ---------------------------------------------------------------------------
# Lambda Permission — allow Step Functions to invoke the Lambda
#
# Step Functions assumes the orchestration role (configured in the
# orchestration module) which has lambda:InvokeFunction permission.
# This resource-based policy is defence-in-depth: rejects invocations from
# principals other than the Step Functions service in this account.
# ---------------------------------------------------------------------------

resource "aws_lambda_permission" "allow_step_functions" {
  statement_id  = "AllowStepFunctionsInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.transformation_pipeline.function_name
  principal     = "states.amazonaws.com"
  source_arn    = "arn:aws:states:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:stateMachine:${var.environment}-extraction-pipeline"
}
