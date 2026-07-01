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

# ---------------------------------------------------------------------------
# Extraction Runtime Role
# Assumed by ECS tasks / Lambda executing the connector runtime.
# Permissions: write raw S3, write schema snapshots, read/write watermark
# DynamoDB, write run audit DynamoDB, read secrets, emit CloudWatch metrics.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "extraction_runtime_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com", "lambda.amazonaws.com"]
    }
    # Restrict role assumption to this account only — prevents cross-account
    # confusion-deputy attacks if the role ARN is ever exposed externally.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "extraction_runtime" {
  name               = "${var.environment}-extraction-runtime-role"
  assume_role_policy = data.aws_iam_policy_document.extraction_runtime_assume_role.json
  description        = "Role assumed by the connector runtime for entity extraction runs."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "extraction_runtime_permissions" {
  # Write to raw layer — scoped to raw bucket prefix only
  statement {
    sid    = "WriteRawLayer"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",           # Needed for multipart upload completion
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${var.raw_layer_bucket_arn}/*"]
  }

  statement {
    sid     = "ListRawLayerBucket"
    effect  = "Allow"
    actions = ["s3:ListBucket"]
    resources = [var.raw_layer_bucket_arn]
  }

  # Read and write schema snapshots
  statement {
    sid    = "ReadWriteSchemaSnapshots"
    effect = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.schema_snapshots_bucket_arn,
      "${var.schema_snapshots_bucket_arn}/*",
    ]
  }

  # Entity extraction config — read-only
  statement {
    sid     = "ReadEntityConfig"
    effect  = "Allow"
    actions = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [var.entity_config_table_arn]
  }

  # Watermark repository — conditional write (optimistic concurrency)
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

  # Run audit log — write only
  statement {
    sid     = "WriteRunAuditLog"
    effect  = "Allow"
    actions = ["dynamodb:PutItem"]
    resources = [var.run_audit_log_table_arn]
  }

  # Secrets Manager — read extraction credentials only
  statement {
    sid     = "ReadSourceCredentials"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:${var.environment}/sources/*",
    ]
  }

  # KMS — decrypt for storage and secrets
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

  # CloudWatch Logs — scoped to the extraction runtime log group.
  # logs:CreateLogStream and PutLogEvents only: the log group is created by
  # Terraform (observability module), so the runtime does not need CreateLogGroup.
  # Granting CreateLogGroup would allow the runtime to create arbitrary log groups.
  statement {
    sid    = "WriteExtractionLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/edl/${var.environment}/connector-runtime",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/edl/${var.environment}/connector-runtime:log-stream:*",
    ]
  }

  # CloudWatch Metrics
  statement {
    sid     = "PutExtractionMetrics"
    effect  = "Allow"
    actions = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }

  # X-Ray tracing
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

  # VPC access — required for Lambda to create/manage ENIs in the VPC.
  # These three actions cannot be scoped to a specific resource ARN.
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
  name   = "${var.environment}-extraction-runtime-policy"
  role   = aws_iam_role.extraction_runtime.id
  policy = data.aws_iam_policy_document.extraction_runtime_permissions.json
}

# ---------------------------------------------------------------------------
# Transformation Runtime Role
# Assumed by the transformation pipeline Lambda function.
# Permissions: read raw S3, read/write curated S3, KMS decrypt/encrypt,
# emit CloudWatch metrics, register Glue catalog partitions, write Lambda
# execution logs, create VPC network interfaces.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "transformation_runtime_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    # Restrict role assumption to this account only — prevents confused-deputy
    # attacks if the role ARN is exposed externally (OWASP A01).
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "transformation_runtime" {
  name               = "${var.environment}-transformation-runtime-role"
  assume_role_policy = data.aws_iam_policy_document.transformation_runtime_assume_role.json
  description        = "Role assumed by the transformation pipeline Lambda for curated layer processing."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "transformation_runtime_permissions" {
  # Read raw layer (source data for transformation) — no write permission
  statement {
    sid     = "ReadRawLayer"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      var.raw_layer_bucket_arn,
      "${var.raw_layer_bucket_arn}/*",
    ]
  }

  # Read and write curated layer — field mappings + quality reports are read,
  # canonical Parquet output is written.
  statement {
    sid    = "ReadWriteCuratedLayer"
    effect = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.curated_layer_bucket_arn,
      "${var.curated_layer_bucket_arn}/*",
    ]
  }

  # KMS: decrypt raw data keys (written by extraction role) and generate new
  # data keys for curated layer writes.
  statement {
    sid    = "KmsDecryptEncrypt"
    effect = "Allow"
    actions = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  # CloudWatch Logs — write Lambda execution logs.
  # CreateLogGroup intentionally excluded: the log group is pre-created by the
  # transformation_lambda Terraform module with correct retention and encryption.
  statement {
    sid    = "WriteLambdaExecutionLogs"
    effect = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.environment}-transformation-pipeline",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.environment}-transformation-pipeline:log-stream:*",
    ]
  }

  # CloudWatch Metrics — emit transformation pipeline metrics.
  # Namespace-scoped condition prevents emission to unrelated namespaces.
  statement {
    sid     = "PutTransformationMetrics"
    effect  = "Allow"
    actions = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }

  # Glue Data Catalog — register curated partitions so Athena can query them.
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
      "arn:aws:glue:${local.region}:${local.account_id}:database/${var.environment}_*",
      "arn:aws:glue:${local.region}:${local.account_id}:table/${var.environment}_*/*",
    ]
  }

  # VPC — create and destroy elastic network interfaces for VPC-deployed Lambda.
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
  name   = "${var.environment}-transformation-runtime-policy"
  role   = aws_iam_role.transformation_runtime.id
  policy = data.aws_iam_policy_document.transformation_runtime_permissions.json
}

# ---------------------------------------------------------------------------
# Entity Resolution Runtime Role
# Assumed by the entity resolution pipeline Lambda.  Reads curated layer
# (canonical Parquet + resolution configs), writes golden records to the
# analytics layer.  No raw layer access — entity resolution operates only
# on already-transformed curated data.
# ---------------------------------------------------------------------------

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
  name               = "${var.environment}-entity-resolution-runtime-role"
  assume_role_policy = data.aws_iam_policy_document.entity_resolution_runtime_assume_role.json
  description        = "Role assumed by the entity resolution pipeline Lambda for golden record production."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "entity_resolution_runtime_permissions" {
  # Read curated layer — canonical Parquet + entity resolution config JSON files.
  statement {
    sid     = "ReadCuratedLayer"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      var.curated_layer_bucket_arn,
      "${var.curated_layer_bucket_arn}/*",
    ]
  }

  # Write analytics layer — golden records Parquet + match decision audit trail.
  statement {
    sid     = "WriteAnalyticsLayer"
    effect  = "Allow"
    actions = ["s3:PutObject", "s3:ListBucket"]
    resources = [
      var.analytics_layer_bucket_arn,
      "${var.analytics_layer_bucket_arn}/*",
    ]
  }

  # KMS: decrypt curated data keys and generate new data keys for analytics writes.
  # Reuses the same storage KMS key used by transformation (curated + analytics
  # buckets share the storage key).
  statement {
    sid     = "KmsDecryptEncrypt"
    effect  = "Allow"
    actions = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  # CloudWatch Logs — write Lambda execution logs.
  statement {
    sid    = "WriteLambdaExecutionLogs"
    effect = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.environment}-entity-resolution-pipeline",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.environment}-entity-resolution-pipeline:log-stream:*",
    ]
  }

  # CloudWatch Metrics — emit entity resolution pipeline metrics.
  statement {
    sid     = "PutEntityResolutionMetrics"
    effect  = "Allow"
    actions = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }

  # VPC — create and destroy elastic network interfaces for VPC-deployed Lambda.
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
  name   = "${var.environment}-entity-resolution-runtime-policy"
  role   = aws_iam_role.entity_resolution_runtime.id
  policy = data.aws_iam_policy_document.entity_resolution_runtime_permissions.json
}

# ---------------------------------------------------------------------------
# Analytics Publisher Runtime Role
# Assumed by the analytics publisher Lambda.  Reads golden records from the
# analytics S3 layer, writes BI-ready Parquet to the same layer, and
# registers Glue catalog tables.  No raw or curated layer write access.
# ---------------------------------------------------------------------------

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
  name               = "${var.environment}-analytics-publisher-runtime-role"
  assume_role_policy = data.aws_iam_policy_document.analytics_publisher_runtime_assume_role.json
  description        = "Role assumed by the analytics publisher Lambda for BI Parquet production and Glue catalog registration."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "analytics_publisher_runtime_permissions" {
  # Read and write analytics layer — golden records are read, BI Parquet is written.
  statement {
    sid     = "ReadWriteAnalyticsLayer"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.analytics_layer_bucket_arn,
      "${var.analytics_layer_bucket_arn}/*",
    ]
  }

  # KMS: decrypt golden record data keys and generate new keys for BI writes.
  statement {
    sid     = "KmsDecryptEncrypt"
    effect  = "Allow"
    actions = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  # CloudWatch Logs — write Lambda execution logs.
  statement {
    sid    = "WriteLambdaExecutionLogs"
    effect = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.environment}-analytics-layer-publisher",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.environment}-analytics-layer-publisher:log-stream:*",
    ]
  }

  # CloudWatch Metrics — emit analytics publisher pipeline metrics.
  statement {
    sid     = "PutAnalyticsPublisherMetrics"
    effect  = "Allow"
    actions = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }

  # Glue Data Catalog — register and update analytics layer tables.
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
      "arn:aws:glue:${local.region}:${local.account_id}:database/${var.environment}_edl_analytics",
      "arn:aws:glue:${local.region}:${local.account_id}:table/${var.environment}_edl_analytics/*",
    ]
  }

  # VPC — create and destroy elastic network interfaces for VPC-deployed Lambda.
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
  name   = "${var.environment}-analytics-publisher-runtime-policy"
  role   = aws_iam_role.analytics_publisher_runtime.id
  policy = data.aws_iam_policy_document.analytics_publisher_runtime_permissions.json
}

# ---------------------------------------------------------------------------
# Transformation Job Role
# Assumed by AWS Glue jobs for curated layer processing.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "transformation_job_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }
    # Restrict role assumption to this account only — prevents confused-deputy
    # attacks where a Glue job in another account assumes this role (OWASP A01).
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "transformation_job" {
  name               = "${var.environment}-transformation-job-role"
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
    sid    = "ReadWriteCuratedLayer"
    effect = "Allow"
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
    sid     = "ReadWatermarkRepository"
    effect  = "Allow"
    actions = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [var.watermark_table_arn]
  }

  statement {
    sid     = "WriteTransformationAuditLog"
    effect  = "Allow"
    actions = ["dynamodb:PutItem"]
    resources = [var.run_audit_log_table_arn]
  }

  statement {
    sid    = "KmsDecrypt"
    effect = "Allow"
    actions = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  statement {
    sid    = "WriteTransformationLogs"
    effect = "Allow"
    # logs:CreateLogGroup intentionally excluded — the log group is pre-created
    # by the Terraform observability module.  Granting CreateLogGroup would allow
    # the job to create arbitrary log groups in this account.
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/edl/${var.environment}/transformation",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/edl/${var.environment}/transformation:log-stream:*",
    ]
  }

  statement {
    sid     = "PutTransformationMetrics"
    effect  = "Allow"
    actions = ["cloudwatch:PutMetricData"]
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
      "arn:aws:glue:${local.region}:${local.account_id}:database/${var.environment}_*",
      "arn:aws:glue:${local.region}:${local.account_id}:table/${var.environment}_*/*",
    ]
  }
}

resource "aws_iam_role_policy" "transformation_job" {
  name   = "${var.environment}-transformation-job-policy"
  role   = aws_iam_role.transformation_job.id
  policy = data.aws_iam_policy_document.transformation_job_permissions.json
}

# ---------------------------------------------------------------------------
# Orchestration Step Functions Role
# ---------------------------------------------------------------------------

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
  name               = "${var.environment}-extraction-orchestration-workflow-role"
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
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.environment}-extraction-pipeline",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.environment}-transformation-pipeline",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.environment}-entity-resolution",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.environment}-analytics-publisher",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.environment}-serving-store-loader",
    ]
  }

  statement {
    sid     = "SendToDlq"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]
    resources = [var.dlq_arn]
  }

  statement {
    sid    = "WriteOrchestrationLogs"
    effect = "Allow"
    # logs:PutResourcePolicy is required by Step Functions to register its log
    # delivery configuration with CloudWatch. Without it, CreateStateMachine
    # fails with AccessDeniedException on the log destination.
    # It cannot be scoped below "Resource": "*" per AWS IAM rules.
    actions = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery", "logs:ListLogDeliveries",
      "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }

  statement {
    sid     = "PutOrchestrationMetrics"
    effect  = "Allow"
    actions = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["EnterpriseDatalake"]
    }
  }
}

resource "aws_iam_role_policy" "orchestration_step_functions" {
  name   = "${var.environment}-extraction-orchestration-workflow-policy"
  role   = aws_iam_role.orchestration_step_functions.id
  policy = data.aws_iam_policy_document.orchestration_sfn_permissions.json
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler Role
# ---------------------------------------------------------------------------

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
  name               = "${var.environment}-extraction-schedule-trigger-role"
  assume_role_policy = data.aws_iam_policy_document.eventbridge_scheduler_assume_role.json
  description        = "Role assumed by EventBridge Scheduler to start extraction Step Functions workflows."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "eventbridge_scheduler_permissions" {
  statement {
    sid     = "StartExtractionWorkflows"
    effect  = "Allow"
    actions = ["states:StartExecution"]
    resources = [
      "arn:aws:states:${local.region}:${local.account_id}:stateMachine:${var.environment}-extraction-pipeline",
    ]
  }
}

resource "aws_iam_role_policy" "eventbridge_scheduler" {
  name   = "${var.environment}-extraction-schedule-trigger-policy"
  role   = aws_iam_role.eventbridge_scheduler.id
  policy = data.aws_iam_policy_document.eventbridge_scheduler_permissions.json
}

# ---------------------------------------------------------------------------
# CI/CD Deployment Role (GitHub Actions OIDC)
# Scoped to Terraform deployment actions for this environment only.
# ---------------------------------------------------------------------------

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
      # Restrict to specific repo and environment branch — update to actual repo
      values = ["repo:${var.github_org}/${var.github_repo}:environment:${var.environment}"]
    }
  }
}

resource "aws_iam_role" "cicd_deployment" {
  name               = "${var.environment}-edl-cicd-deployment-role"
  assume_role_policy = data.aws_iam_policy_document.cicd_deployment_assume_role.json
  description        = "Role assumed by GitHub Actions OIDC for Terraform deployments to ${var.environment}."
  tags               = local.common_tags
}

# Attach AWS managed policies for Terraform deployment scope
# In production: replace with a tightly scoped custom policy enumerating exact resources
resource "aws_iam_role_policy_attachment" "cicd_deployment_terraform" {
  for_each   = toset(var.cicd_deployment_policy_arns)
  role       = aws_iam_role.cicd_deployment.name
  policy_arn = each.value
}
