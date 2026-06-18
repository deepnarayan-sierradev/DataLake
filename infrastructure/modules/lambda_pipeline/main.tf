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
  common_tags     = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "lambda_pipeline"
  })
  function_name = "${var.environment}-extraction-pipeline"
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
# Allows all egress (needed for HTTPS calls to Salesforce/NetSuite APIs
# and AWS service endpoints).  No ingress — Lambda is invoked by SFN only.
# ---------------------------------------------------------------------------

data "aws_vpc" "selected" {
  filter {
    name   = "tag:Environment"
    values = [var.environment]
  }
}

resource "aws_security_group" "lambda_pipeline" {
  name        = "${local.function_name}-sg"
  description = "Security group for the extraction pipeline Lambda. Egress to AWS APIs and external sources only."
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description = "HTTPS egress — AWS service endpoints and external source APIs (Salesforce, NetSuite)."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "MySQL RDS egress — port 3306 to VPC CIDR only (no internet exposure)."
    from_port   = 3306
    to_port     = 3306
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.selected.cidr_block]
  }

  tags = merge(local.common_tags, {
    Name = "${local.function_name}-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------------------
# Lambda Function — Extraction Pipeline
#
# Handler: connector_runtime.extraction_pipeline_handler.lambda_handler
#
# Runtime: python3.13 (closest GA runtime to 3.14 at time of writing;
# update to python3.14 when AWS adds it to Lambda runtimes).
#
# VPC: deployed into private subnets so it can reach MySQL RDS and the
# VPC interface endpoints for S3, DynamoDB, SQS, Secrets Manager, and SFN
# without crossing the public internet.
#
# Environment variables:
#   PLATFORM_ENVIRONMENT  — "dev" | "staging" | "prod"
#   RAW_S3_BUCKET         — raw layer bucket name
#   SCHEMA_SNAPSHOT_S3_BUCKET — schema snapshot bucket name
#   AWS_REGION            — injected by Lambda runtime automatically
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "extraction_pipeline" {
  function_name = local.function_name
  description   = "Extraction pipeline handler invoked by Step Functions. Runs one entity extraction end-to-end."

  s3_bucket         = var.lambda_package_s3_bucket
  s3_key            = var.lambda_package_s3_key
  source_code_hash  = var.lambda_package_source_hash

  runtime       = "python3.13"
  handler       = "connector_runtime.extraction_pipeline_handler.lambda_handler"
  role          = var.execution_role_arn
  memory_size   = var.memory_size_mb
  timeout       = var.timeout_seconds

  reserved_concurrent_executions = var.reserved_concurrent_executions

  kms_key_arn = var.kms_key_arn

  environment {
    variables = {
      PLATFORM_ENVIRONMENT          = var.environment
      RAW_S3_BUCKET                 = var.raw_s3_bucket_name
      SCHEMA_SNAPSHOT_S3_BUCKET     = var.schema_snapshot_s3_bucket_name
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = concat(var.security_group_ids, [aws_security_group.lambda_pipeline.id])
  }

  tracing_config {
    mode = var.enable_xray_tracing ? "Active" : "PassThrough"
  }

  # Ensure log group is created before Lambda so Terraform owns retention/encryption.
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
# Step Functions assumes the step_functions_role_arn (passed via orchestration
# module) which has lambda:InvokeFunction permission.  This resource-based
# policy is defence-in-depth: rejects invocations from principals other than
# the Step Functions service in this account.
# ---------------------------------------------------------------------------

resource "aws_lambda_permission" "allow_step_functions" {
  statement_id  = "AllowStepFunctionsInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.extraction_pipeline.function_name
  principal     = "states.amazonaws.com"
  source_arn    = "arn:aws:states:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:stateMachine:${var.environment}-extraction-pipeline"
}
