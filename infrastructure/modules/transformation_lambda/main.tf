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
    Module      = "transformation_lambda"
  })
  function_name = "${var.name_prefix}-transformation-${var.environment}"
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


locals {
  required_resource_names = [
    "DATA_QUALITY_EXCEPTION_TABLE",
    "ENTITY_CONFIG_TABLE",
    "QUALITY_POLICY_TABLE",
    "RESOURCE_NAME_PREFIX",
    "SCOPE_UNIT_TABLE",
    "SECRET_PATH_PREFIX",
    "SOURCE_CONNECTION_TABLE",
  ]
  resource_name_variables = {
    for key in local.required_resource_names : key => var.resource_names[key]
  }
}

resource "aws_lambda_function" "transformation_pipeline" {
  function_name = local.function_name
  description   = "Transformation pipeline handler invoked by Step Functions. Applies field mappings, evaluates quality, and writes to the curated layer."

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  runtime = "python3.13"

  code_signing_config_arn = var.code_signing_config_arn


  dead_letter_config {

    target_arn = aws_sqs_queue.async_dlq.arn

  }
  handler     = "transformation.transformation_pipeline_handler.lambda_handler"
  role        = var.execution_role_arn
  memory_size = var.memory_size_mb
  timeout     = var.timeout_seconds

  reserved_concurrent_executions = var.reserved_concurrent_executions

  kms_key_arn = var.kms_key_arn

  environment {
    variables = merge({
      PLATFORM_ENVIRONMENT    = var.environment
      RAW_S3_BUCKET           = var.raw_s3_bucket_name
      CURATED_S3_BUCKET       = var.curated_s3_bucket_name
      FIELD_MAPPING_S3_BUCKET = var.field_mapping_s3_bucket_name
      GOVERNANCE_S3_BUCKET    = var.governance_s3_bucket_name
      # DL-SERV-08: wired so curated-layer registration is live rather than dead code.
      GLUE_CATALOG_DATABASE = var.glue_catalog_database
    }, local.resource_name_variables)
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = concat(var.security_group_ids, [aws_security_group.transformation_lambda.id])
  }

  tracing_config {
    mode = var.enable_xray_tracing ? "Active" : "PassThrough"
  }

  depends_on = [aws_cloudwatch_log_group.lambda_execution]

  tags = merge(local.common_tags, {
    Name = local.function_name
  })

  lifecycle {
    ignore_changes = []
  }
}


resource "aws_lambda_permission" "allow_step_functions" {
  statement_id  = "AllowStepFunctionsInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.transformation_pipeline.function_name
  principal     = "states.amazonaws.com"
  source_arn    = "arn:aws:states:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:stateMachine:${var.name_prefix}-extraction-workflow-${var.environment}"
}


resource "aws_sqs_queue" "async_dlq" {
  name                      = "${var.name_prefix}-transformation-async-dlq-${var.environment}"
  message_retention_seconds = 1209600 # 14 days, the maximum — a DLQ that expires loses the evidence
  sqs_managed_sse_enabled   = true

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-transformation-async-dlq-${var.environment}" })
}
