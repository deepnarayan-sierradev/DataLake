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
    Module      = "iam"
  })
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
}


data "aws_iam_policy_document" "extraction_runtime_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com", "lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "extraction_runtime" {
  name               = "${var.name_prefix}-extraction-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.extraction_runtime_assume_role.json
  description        = "Role assumed by the connector runtime for entity extraction runs."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "extraction_runtime_permissions" {
  #checkov:skip=CKV_AWS_111:AWS defines no resource-level permission for these actions.
  #checkov:skip=CKV_AWS_356:Wildcard confined to actions that admit no ARN; all others are scoped.

  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid    = "WriteRawLayer"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject", # Needed for multipart upload completion
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${var.raw_layer_bucket_arn}/*"]
  }

  statement {
    sid       = "ListRawLayerBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.raw_layer_bucket_arn]
  }

  statement {
    sid     = "ReadWriteSchemaSnapshots"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.schema_snapshots_bucket_arn,
      "${var.schema_snapshots_bucket_arn}/*",
    ]
  }

  statement {
    sid       = "ReadEntityConfig"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [var.entity_config_table_arn]
  }

  statement {
    sid    = "WatermarkRepositoryAccess"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:ConditionCheckItem",
    ]
    resources = [var.watermark_table_arn]
  }

  statement {
    sid       = "WriteRunAuditLog"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [var.run_audit_log_table_arn]
  }

  statement {
    sid     = "ReadSourceCredentials"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:datalake/<env>/sources/*",
    ]
  }

  statement {
    sid    = "KmsDecryptForStorageAndSecrets"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]
    resources = var.kms_key_arns_for_extraction
  }

  statement {
    sid    = "WriteExtractionLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/${var.name_prefix}/connector-runtime-${var.environment}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/${var.name_prefix}/connector-runtime-${var.environment}:log-stream:*",
    ]
  }

  statement {
    sid       = "PutExtractionMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }

  statement {
    sid    = "XRayTracing"
    effect = "Allow"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "VpcNetworkInterfaceAccess"
    effect = "Allow"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DeleteNetworkInterface",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "extraction_runtime" {
  name   = "${var.name_prefix}-extraction-${var.environment}-exec-policy"
  role   = aws_iam_role.extraction_runtime.id
  policy = data.aws_iam_policy_document.extraction_runtime_permissions.json
}


data "aws_iam_policy_document" "transformation_runtime_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "transformation_runtime" {
  name               = "${var.name_prefix}-transformation-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.transformation_runtime_assume_role.json
  description        = "Role assumed by the transformation pipeline Lambda for curated layer processing."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "transformation_runtime_permissions" {
  #checkov:skip=CKV_AWS_111:AWS defines no resource-level permission for these actions.
  #checkov:skip=CKV_AWS_356:Wildcard confined to actions that admit no ARN; all others are scoped.

  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid     = "ReadEntityExtractionConfig"
    effect  = "Allow"
    actions = ["dynamodb:GetItem"]
    resources = [
      var.entity_config_table_arn,
    ]
  }

  statement {
    sid     = "ReadRawLayer"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      var.raw_layer_bucket_arn,
      "${var.raw_layer_bucket_arn}/*",
    ]
  }

  statement {
    sid     = "ReadWriteCuratedLayer"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.curated_layer_bucket_arn,
      "${var.curated_layer_bucket_arn}/*",
    ]
  }

  statement {
    sid       = "KmsDecryptEncrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-transformation-${var.environment}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-transformation-${var.environment}:log-stream:*",
    ]
  }

  statement {
    sid       = "PutTransformationMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }

  statement {
    sid    = "GlueCatalogAccess"
    effect = "Allow"
    actions = [
      "glue:GetDatabase",
      "glue:GetTable",
      "glue:GetPartition",
      "glue:CreateTable",
      "glue:UpdateTable",
      "glue:CreatePartition",
      "glue:BatchCreatePartition",
    ]
    resources = [
      "arn:aws:glue:${local.region}:${local.account_id}:catalog",
      "arn:aws:glue:${local.region}:${local.account_id}:database/${replace(var.name_prefix, "-", "_")}_*",
      "arn:aws:glue:${local.region}:${local.account_id}:table/${replace(var.name_prefix, "-", "_")}_*/*",
    ]
  }

  statement {
    sid    = "VpcNetworkInterfaceAccess"
    effect = "Allow"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DeleteNetworkInterface",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "transformation_runtime" {
  name   = "${var.name_prefix}-transformation-${var.environment}-exec-policy"
  role   = aws_iam_role.transformation_runtime.id
  policy = data.aws_iam_policy_document.transformation_runtime_permissions.json
}


data "aws_iam_policy_document" "entity_resolution_runtime_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "entity_resolution_runtime" {
  name               = "${var.name_prefix}-entity-resolution-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.entity_resolution_runtime_assume_role.json
  description        = "Role assumed by the entity resolution pipeline Lambda for golden record production."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "entity_resolution_runtime_permissions" {
  #checkov:skip=CKV_AWS_111:AWS defines no resource-level permission for these actions.
  #checkov:skip=CKV_AWS_356:Wildcard confined to actions that admit no ARN; all others are scoped.

  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid     = "ReadCuratedLayer"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      var.curated_layer_bucket_arn,
      "${var.curated_layer_bucket_arn}/*",
    ]
  }

  statement {
    sid     = "WriteAnalyticsLayer"
    effect  = "Allow"
    actions = ["s3:PutObject", "s3:ListBucket"]
    resources = [
      var.analytics_layer_bucket_arn,
      "${var.analytics_layer_bucket_arn}/*",
    ]
  }

  statement {
    sid       = "ReadEntityTypeRegistry"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [var.entity_type_registry_table_arn]
  }

  statement {
    sid       = "KmsDecryptEncrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-entity-resolution-${var.environment}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-entity-resolution-${var.environment}:log-stream:*",
    ]
  }

  statement {
    sid       = "PutEntityResolutionMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }

  statement {
    sid    = "VpcNetworkInterfaceAccess"
    effect = "Allow"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DeleteNetworkInterface",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "entity_resolution_runtime" {
  name   = "${var.name_prefix}-entity-resolution-${var.environment}-exec-policy"
  role   = aws_iam_role.entity_resolution_runtime.id
  policy = data.aws_iam_policy_document.entity_resolution_runtime_permissions.json
}


data "aws_iam_policy_document" "analytics_publisher_runtime_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "analytics_publisher_runtime" {
  name               = "${var.name_prefix}-analytics-publisher-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.analytics_publisher_runtime_assume_role.json
  description        = "Role assumed by the analytics publisher Lambda for BI Parquet production and Glue catalog registration."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "analytics_publisher_runtime_permissions" {
  #checkov:skip=CKV_AWS_111:AWS defines no resource-level permission for these actions.
  #checkov:skip=CKV_AWS_356:Wildcard confined to actions that admit no ARN; all others are scoped.

  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid     = "ReadWriteAnalyticsLayer"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.analytics_layer_bucket_arn,
      "${var.analytics_layer_bucket_arn}/*",
    ]
  }

  statement {
    sid       = "ReadEntityTypeRegistry"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [var.entity_type_registry_table_arn]
  }

  statement {
    sid       = "KmsDecryptEncrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-analytics-publisher-${var.environment}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-analytics-publisher-${var.environment}:log-stream:*",
    ]
  }

  statement {
    sid       = "PutAnalyticsPublisherMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }

  statement {
    sid    = "GlueCatalogAccess"
    effect = "Allow"
    actions = [
      "glue:GetDatabase",
      "glue:CreateDatabase",
      "glue:GetTable",
      "glue:CreateTable",
      "glue:UpdateTable",
      "glue:GetPartition",
      "glue:CreatePartition",
      "glue:BatchCreatePartition",
    ]
    resources = [
      "arn:aws:glue:${local.region}:${local.account_id}:catalog",
      "arn:aws:glue:${local.region}:${local.account_id}:database/${replace(var.name_prefix, "-", "_")}_analytics_${var.environment}",
      "arn:aws:glue:${local.region}:${local.account_id}:table/${replace(var.name_prefix, "-", "_")}_analytics_${var.environment}/*",
    ]
  }

  statement {
    sid    = "VpcNetworkInterfaceAccess"
    effect = "Allow"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DeleteNetworkInterface",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "analytics_publisher_runtime" {
  name   = "${var.name_prefix}-analytics-publisher-${var.environment}-exec-policy"
  role   = aws_iam_role.analytics_publisher_runtime.id
  policy = data.aws_iam_policy_document.analytics_publisher_runtime_permissions.json
}


data "aws_iam_policy_document" "serving_store_loader_runtime_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "serving_store_loader_runtime" {
  name               = "${var.name_prefix}-serving-store-loader-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.serving_store_loader_runtime_assume_role.json
  description        = "Role assumed by the serving store loader Lambda for relational BI-store loads."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "serving_store_loader_runtime_permissions" {
  #checkov:skip=CKV_AWS_111:AWS defines no resource-level permission for these actions.
  #checkov:skip=CKV_AWS_356:Wildcard confined to actions that admit no ARN; all others are scoped.

  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid       = "ReadAnalyticsLayer"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.analytics_layer_bucket_arn, "${var.analytics_layer_bucket_arn}/*"]
  }

  statement {
    sid       = "ReadServingStoreConfig"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [var.serving_store_config_table_arn]
  }

  statement {
    sid    = "ServingStoreCredentials"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:CreateSecret",
      "secretsmanager:PutSecretValue",
    ]
    resources = var.serving_store_secret_arns
  }

  dynamic "statement" {
    for_each = length(var.kms_key_arns_for_serving_store) > 0 ? [1] : []
    content {
      sid       = "ServingStoreKmsDecrypt"
      effect    = "Allow"
      actions   = ["kms:Decrypt", "kms:DescribeKey"]
      resources = var.kms_key_arns_for_serving_store
    }
  }

  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-serving-store-loader-${var.environment}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-serving-store-loader-${var.environment}:log-stream:*",
    ]
  }

  statement {
    sid       = "PutServingStoreMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }

  statement {
    sid    = "VpcNetworkInterfaceAccess"
    effect = "Allow"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DeleteNetworkInterface",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "serving_store_loader_runtime" {
  name   = "${var.name_prefix}-serving-store-loader-${var.environment}-exec-policy"
  role   = aws_iam_role.serving_store_loader_runtime.id
  policy = data.aws_iam_policy_document.serving_store_loader_runtime_permissions.json
}


data "aws_iam_policy_document" "twin_build_runtime_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "twin_build_runtime" {
  name               = "${var.name_prefix}-twin-builder-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.twin_build_runtime_assume_role.json
  description        = "Role assumed by the twin builder Lambda to read analytics golden records and write the twin index."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "twin_build_runtime_permissions" {

  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid       = "ReadAnalyticsLayer"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.analytics_layer_bucket_arn, "${var.analytics_layer_bucket_arn}/*"]
  }

  statement {
    sid       = "WriteRelationshipEdges"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${var.analytics_layer_bucket_arn}/*"]
  }

  statement {
    sid       = "ReadRelationshipRulesConfig"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.curated_layer_bucket_arn, "${var.curated_layer_bucket_arn}/*"]
  }

  statement {
    sid       = "WriteTwinIndex"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query"]
    resources = [var.twin_index_table_arn]
  }

  statement {
    sid       = "ReadEntityTypeRegistry"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [var.entity_type_registry_table_arn]
  }

  statement {
    sid       = "KmsDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-twin-builder-${var.environment}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-twin-builder-${var.environment}:log-stream:*",
    ]
  }

  statement {
    sid    = "XRayTracing"
    effect = "Allow"
    actions = [
      "xray:PutTraceSegments", "xray:PutTelemetryRecords",
      "xray:GetSamplingRules", "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "twin_build_runtime" {
  name   = "${var.name_prefix}-twin-builder-${var.environment}-exec-policy"
  role   = aws_iam_role.twin_build_runtime.id
  policy = data.aws_iam_policy_document.twin_build_runtime_permissions.json
}


data "aws_iam_policy_document" "transformation_job_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "transformation_job" {
  name               = "${var.name_prefix}-transformation-job-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.transformation_job_assume_role.json
  description        = "Role assumed by Glue transformation jobs for curated layer processing."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "transformation_job_permissions" {
  statement {
    sid     = "ReadRawLayer"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      var.raw_layer_bucket_arn,
      "${var.raw_layer_bucket_arn}/*",
    ]
  }

  statement {
    sid     = "ReadWriteCuratedLayer"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.curated_layer_bucket_arn,
      "${var.curated_layer_bucket_arn}/*",
    ]
  }

  statement {
    sid     = "ReadSchemaSnapshots"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      var.schema_snapshots_bucket_arn,
      "${var.schema_snapshots_bucket_arn}/*",
    ]
  }

  statement {
    sid       = "ReadWatermarkRepository"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [var.watermark_table_arn]
  }

  statement {
    sid       = "WriteTransformationAuditLog"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [var.run_audit_log_table_arn]
  }

  statement {
    sid       = "KmsDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  statement {
    sid     = "WriteTransformationLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/${var.name_prefix}/transformation-${var.environment}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/${var.name_prefix}/transformation-${var.environment}:log-stream:*",
    ]
  }

  statement {
    sid       = "PutTransformationMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }

  statement {
    sid    = "GlueCatalogAccess"
    effect = "Allow"
    actions = [
      "glue:GetDatabase", "glue:GetTable", "glue:GetPartition",
      "glue:CreateTable", "glue:UpdateTable", "glue:CreatePartition",
      "glue:BatchCreatePartition",
    ]
    resources = [
      "arn:aws:glue:${local.region}:${local.account_id}:catalog",
      "arn:aws:glue:${local.region}:${local.account_id}:database/${replace(var.name_prefix, "-", "_")}_*",
      "arn:aws:glue:${local.region}:${local.account_id}:table/${replace(var.name_prefix, "-", "_")}_*/*",
    ]
  }
}

resource "aws_iam_role_policy" "transformation_job" {
  name   = "${var.name_prefix}-transformation-job-${var.environment}-exec-policy"
  role   = aws_iam_role.transformation_job.id
  policy = data.aws_iam_policy_document.transformation_job_permissions.json
}


data "aws_iam_policy_document" "orchestration_sfn_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "orchestration_step_functions" {
  name               = "${var.name_prefix}-extraction-workflow-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.orchestration_sfn_assume_role.json
  description        = "Role assumed by Step Functions for extraction pipeline orchestration."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "orchestration_sfn_permissions" {
  statement {
    sid     = "InvokeLambdaOrEcs"
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    resources = [
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.name_prefix}-extraction-${var.environment}",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.name_prefix}-transformation-${var.environment}",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.name_prefix}-entity-resolution-${var.environment}",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.name_prefix}-analytics-publisher-${var.environment}",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.name_prefix}-twin-builder-${var.environment}",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.name_prefix}-serving-store-loader-${var.environment}",
    ]
  }

  statement {
    sid       = "SendToDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [var.dlq_arn]
  }

  statement {
    sid    = "WriteOrchestrationLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery", "logs:ListLogDeliveries",
      "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PutOrchestrationMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }
}

resource "aws_iam_role_policy" "orchestration_step_functions" {
  name   = "${var.name_prefix}-extraction-workflow-${var.environment}-exec-policy"
  role   = aws_iam_role.orchestration_step_functions.id
  policy = data.aws_iam_policy_document.orchestration_sfn_permissions.json
}


data "aws_iam_policy_document" "eventbridge_scheduler_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "eventbridge_scheduler" {
  name               = "${var.name_prefix}-extraction-scheduler-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.eventbridge_scheduler_assume_role.json
  description        = "Role assumed by EventBridge Scheduler to start extraction Step Functions workflows."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "eventbridge_scheduler_permissions" {
  statement {
    sid     = "SendToPipelineTriggerQueue"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]
    resources = [
      "arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-pipeline-trigger-${var.environment}.fifo",
    ]
  }
  statement {
    sid     = "StartExtractionWorkflows"
    effect  = "Allow"
    actions = ["states:StartExecution"]
    resources = [
      "arn:aws:states:${local.region}:${local.account_id}:stateMachine:${var.name_prefix}-extraction-workflow-${var.environment}",
    ]
  }
}

resource "aws_iam_role_policy" "eventbridge_scheduler" {
  name   = "${var.name_prefix}-extraction-scheduler-${var.environment}-exec-policy"
  role   = aws_iam_role.eventbridge_scheduler.id
  policy = data.aws_iam_policy_document.eventbridge_scheduler_permissions.json
}


data "aws_iam_policy_document" "cicd_deployment_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = ["arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_org}/${var.github_repo}:environment:${var.environment}"]
    }
  }
}

resource "aws_iam_role" "cicd_deployment" {
  name               = "${var.name_prefix}-deploy-${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.cicd_deployment_assume_role.json
  description        = "Role assumed by GitHub Actions OIDC for Terraform deployments to ${var.environment}."
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "cicd_deployment_terraform" {
  for_each   = toset(var.cicd_deployment_policy_arns)
  role       = aws_iam_role.cicd_deployment.name
  policy_arn = each.value
}


data "aws_iam_policy_document" "pipeline_trigger_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "pipeline_trigger" {
  name               = "${var.name_prefix}-pipeline-trigger-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.pipeline_trigger_assume_role.json
  description        = "Role assumed by the pipeline trigger Lambda to drain SQS and start Step Functions executions."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "pipeline_trigger_permissions" {

  statement {
    sid       = "AsyncInvocationDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid    = "ConsumePipelineTriggerQueue"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [
      "arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-pipeline-trigger-${var.environment}.fifo",
    ]
  }

  statement {
    sid     = "StartExtractionPipelineExecution"
    effect  = "Allow"
    actions = ["states:StartExecution"]
    resources = [
      "arn:aws:states:${local.region}:${local.account_id}:stateMachine:${var.name_prefix}-extraction-workflow-${var.environment}",
    ]
  }

  statement {
    sid       = "KmsDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_extraction
  }

  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-pipeline-trigger-${var.environment}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-pipeline-trigger-${var.environment}:log-stream:*",
    ]
  }

  statement {
    sid       = "XRayTracing"
    effect    = "Allow"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "pipeline_trigger" {
  name   = "${var.name_prefix}-pipeline-trigger-${var.environment}-exec-policy"
  role   = aws_iam_role.pipeline_trigger.id
  policy = data.aws_iam_policy_document.pipeline_trigger_permissions.json
}


data "aws_iam_policy_document" "dlq_processor_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "dlq_processor" {
  name               = "${var.name_prefix}-dlq-processor-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.dlq_processor_assume_role.json
  description        = "Role assumed by the DLQ processor Lambda to read, audit, and optionally replay failed runs."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "dlq_processor_permissions" {
  statement {
    sid    = "ConsumeDLQ"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = concat(
      [var.dlq_arn],
      [
        "arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}",
        "arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-replay-exhausted-${var.environment}",
      ],
    )
  }

  statement {
    sid       = "WriteAuditLog"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [var.run_audit_log_table_arn]
  }

  statement {
    sid       = "PublishAlertNotification"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = ["arn:aws:sns:${local.region}:${local.account_id}:${var.name_prefix}-platform-alerts-${var.environment}"]
  }

  statement {
    sid     = "ReplayFailedRuns"
    effect  = "Allow"
    actions = ["states:StartExecution"]
    resources = [
      "arn:aws:states:${local.region}:${local.account_id}:stateMachine:${var.name_prefix}-extraction-workflow-${var.environment}",
    ]
  }

  statement {
    sid       = "KmsDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_extraction
  }

  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-dlq-processor-${var.environment}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-dlq-processor-${var.environment}:log-stream:*",
    ]
  }
}

resource "aws_iam_role_policy" "dlq_processor" {
  name   = "${var.name_prefix}-dlq-processor-${var.environment}-exec-policy"
  role   = aws_iam_role.dlq_processor.id
  policy = data.aws_iam_policy_document.dlq_processor_permissions.json
}


data "aws_iam_policy_document" "credential_expiry_notifier_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "credential_expiry_notifier" {
  name               = "${var.name_prefix}-credential-expiry-notifier-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.credential_expiry_notifier_assume_role.json
  description        = "Role assumed by the credential expiry notifier Lambda (SEC-6). Read-only secret metadata + SNS publish."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "credential_expiry_notifier_permissions" {

  statement {
    sid       = "AsyncInvocationDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid       = "DescribeSourceCredentialSecrets"
    effect    = "Allow"
    actions   = ["secretsmanager:DescribeSecret"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:datalake/<env>/sources/*"]
  }

  statement {
    sid       = "PublishAlertNotification"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = ["arn:aws:sns:${local.region}:${local.account_id}:${var.name_prefix}-platform-alerts-${var.environment}"]
  }

  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-credential-expiry-notifier-${var.environment}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-credential-expiry-notifier-${var.environment}:log-stream:*",
    ]
  }

  statement {
    sid       = "KmsDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_credential_expiry_notifier
  }
}

resource "aws_iam_role_policy" "credential_expiry_notifier" {
  name   = "${var.name_prefix}-credential-expiry-notifier-${var.environment}-exec-policy"
  role   = aws_iam_role.credential_expiry_notifier.id
  policy = data.aws_iam_policy_document.credential_expiry_notifier_permissions.json
}

data "aws_iam_policy_document" "credential_expiry_scheduler_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "credential_expiry_scheduler" {
  name               = "${var.name_prefix}-credential-expiry-scheduler-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.credential_expiry_scheduler_assume_role.json
  description        = "Role assumed by EventBridge Scheduler to invoke the credential expiry notifier Lambda (SEC-6)."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "credential_expiry_scheduler_permissions" {
  statement {
    sid       = "InvokeCredentialExpiryNotifier"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${local.region}:${local.account_id}:function:${var.name_prefix}-credential-expiry-notifier-${var.environment}"]
  }
}

resource "aws_iam_role_policy" "credential_expiry_scheduler" {
  name   = "${var.name_prefix}-credential-expiry-scheduler-${var.environment}-exec-policy"
  role   = aws_iam_role.credential_expiry_scheduler.id
  policy = data.aws_iam_policy_document.credential_expiry_scheduler_permissions.json
}


data "aws_iam_policy_document" "control_plane_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "control_plane" {
  name               = "${var.name_prefix}-control-plane-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.control_plane_assume_role.json
  description        = "Role assumed by the control-plane API Lambda for tenant provisioning, entity registration, pipeline triggering, and run status queries."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "control_plane_permissions" {

  statement {
    sid       = "AsyncInvocationDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid       = "ReadWriteEntityConfig"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [var.entity_config_table_arn]
  }

  statement {
    sid       = "ReadWriteEntityTypeRegistry"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [var.entity_type_registry_table_arn]
  }

  statement {
    sid       = "ReadRunAuditLog"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [var.run_audit_log_table_arn]
  }

  statement {
    sid       = "ReadTwinIndex"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [var.twin_index_table_arn]
  }

  statement {
    sid       = "ReadSemanticModel"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [var.semantic_model_table_arn]
  }

  statement {
    sid       = "ReadWriteSavedQuery"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
    resources = [var.saved_query_table_arn]
  }

  statement {
    sid       = "ReadAnalyticsLayer"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.analytics_layer_bucket_arn, "${var.analytics_layer_bucket_arn}/*"]
  }

  statement {
    sid     = "SendToPipelineTriggerQueue"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]
    resources = [
      "arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-pipeline-trigger-${var.environment}.fifo",
    ]
  }

  statement {
    sid       = "KmsDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_extraction
  }

  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-control-plane-${var.environment}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-control-plane-${var.environment}:log-stream:*",
    ]
  }

  statement {
    sid       = "XRayTracing"
    effect    = "Allow"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "control_plane" {
  name   = "${var.name_prefix}-control-plane-${var.environment}-exec-policy"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.control_plane_permissions.json
}
