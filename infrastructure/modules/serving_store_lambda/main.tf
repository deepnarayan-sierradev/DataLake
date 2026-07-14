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
    Module      = "serving_store_lambda"
  })
  function_name = "EdlServingStoreLoader"
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
# Lambda Security Group
#
# Reads analytics Parquet from S3 (HTTPS via VPC endpoint). MySQL egress
# (3306) to the serving store RDS instance is wired by the caller as a
# standalone aws_security_group_rule once the database module's SG also
# exists — see infrastructure/modules/serving_store_database/main.tf's note
# on why that cross-reference can't live inside either module.
# ---------------------------------------------------------------------------

data "aws_vpc" "selected" {
  filter {
    name   = "tag:Environment"
    values = [var.environment]
  }
}

resource "aws_security_group" "serving_store_lambda" {
  name        = "${local.function_name}Sg"
  description = "Security group for the serving store loader Lambda. HTTPS to AWS VPC endpoints; 3306 egress wired by the caller."
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description = "HTTPS egress - AWS VPC endpoints (S3, Secrets Manager, DynamoDB, CloudWatch)."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${local.function_name}Sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------------------
# Lambda Function — Serving Store Loader
#
# Handler: serving_store.serving_store_loader_handler.lambda_handler
# Runtime: python3.13
#
# Environment variables:
#   PLATFORM_ENVIRONMENT  — "dev" | "staging" | "prod"
#   ANALYTICS_S3_BUCKET   — analytics layer bucket (read-only)
#   GOVERNANCE_S3_BUCKET  — lineage bucket (optional; empty = disabled)
#   AWS_REGION            — injected automatically by Lambda runtime
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "serving_store_loader" {
  function_name = local.function_name
  description   = "Serving store load invoked by Step Functions. Loads analytics Parquet into a tenant-scoped relational database for direct BI tool access."

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  runtime     = "python3.13"
  handler     = "serving_store.serving_store_loader_handler.lambda_handler"
  role        = var.execution_role_arn
  memory_size = var.memory_size_mb
  timeout     = var.timeout_seconds

  reserved_concurrent_executions = var.reserved_concurrent_executions

  kms_key_arn = var.kms_key_arn

  environment {
    variables = {
      PLATFORM_ENVIRONMENT = var.environment
      ANALYTICS_S3_BUCKET  = var.analytics_s3_bucket_name
      GOVERNANCE_S3_BUCKET = var.governance_s3_bucket_name
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = concat(var.security_group_ids, [aws_security_group.serving_store_lambda.id])
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
  function_name = aws_lambda_function.serving_store_loader.function_name
  principal     = "states.amazonaws.com"
  source_arn    = "arn:aws:states:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:stateMachine:EdlExtractionPipeline"
}
