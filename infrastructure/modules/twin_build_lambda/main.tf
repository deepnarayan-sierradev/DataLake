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
    Module      = "twin_build_lambda"
  })
  function_name = "EdlTwinBuilder"
}

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
# Lambda Function — Twin Builder (BuildTwin Step Functions stage)
#
# Reads analytics-layer golden records and relationship-rules config from S3,
# resolves edges set-based, and upserts the twin index in DynamoDB. Runs
# outside the VPC: it needs only S3 (HTTPS) and DynamoDB, not the serving-store
# RDS, so no security group / subnet wiring is required.
#
# Environment variables:
#   PLATFORM_ENVIRONMENT          — "dev" | "staging" | "prod"
#   ANALYTICS_S3_BUCKET           — analytics layer bucket (read golden, write edges)
#   RELATIONSHIP_RULES_S3_BUCKET  — bucket holding relationship-rules config JSON
#   AWS_REGION                    — injected automatically by the Lambda runtime
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "twin_builder" {
  function_name = local.function_name
  description   = "Twin builder invoked by Step Functions. Connects analytics golden records into the tenant-scoped twin index."

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  runtime     = "python3.13"
  handler     = "knowledge.twin_build_handler.lambda_handler"
  role        = var.execution_role_arn
  memory_size = var.memory_size_mb
  timeout     = var.timeout_seconds

  reserved_concurrent_executions = var.reserved_concurrent_executions

  kms_key_arn = var.kms_key_arn

  environment {
    variables = {
      PLATFORM_ENVIRONMENT         = var.environment
      ANALYTICS_S3_BUCKET          = var.analytics_s3_bucket_name
      RELATIONSHIP_RULES_S3_BUCKET = var.relationship_rules_s3_bucket_name
    }
  }

  tracing_config {
    mode = var.enable_xray_tracing ? "Active" : "PassThrough"
  }

  depends_on = [aws_cloudwatch_log_group.lambda_execution]

  tags = merge(local.common_tags, {
    Name = local.function_name
  })
}

resource "aws_lambda_permission" "allow_step_functions" {
  statement_id  = "AllowStepFunctionsInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.twin_builder.function_name
  principal     = "states.amazonaws.com"
  source_arn    = "arn:aws:states:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:stateMachine:EdlExtractionPipeline"
}
