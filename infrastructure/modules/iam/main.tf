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
  name               = "EdlExtractionRuntimeRole"
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

  # Read and write schema snapshots
  statement {
    sid     = "ReadWriteSchemaSnapshots"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.schema_snapshots_bucket_arn,
      "${var.schema_snapshots_bucket_arn}/*",
    ]
  }

  # Entity extraction config — read-only
  statement {
    sid       = "ReadEntityConfig"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
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
    sid       = "WriteRunAuditLog"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [var.run_audit_log_table_arn]
  }

  # Secrets Manager — read extraction credentials only
  #
  # SEC-2: this remains a wildcard across all connectors and cannot be
  # narrowed to a single tenant with a static Terraform policy alone, given
  # today's architecture: every tenant currently shares the SAME per-source
  # connector credentials (one Salesforce/NetSuite/MySQL/Sage secret per
  # environment, not per-tenant) via a single shared extraction_runtime role.
  #
  # Closing this for real requires a product decision this module cannot
  # make unilaterally — either:
  #   (a) per-tenant credentials: extend the secret path to
  #       {environment}/{tenant_code}/sources/{source_id}/credentials
  #       (mirroring the S3 tenant-prefix convention already used by
  #       transformation/curated_layer_writer.py) and scope this resource
  #       pattern to match, or
  #   (b) ABAC: a role assumed per-invocation with a TenantCode session tag,
  #       with resource ARNs parameterized on ${aws:PrincipalTag/TenantCode}
  #       — requires every handler to assume a scoped role before touching
  #       S3/Secrets, not just a Terraform change.
  # Building either speculatively, before tenant credential-sharing is
  # decided, would be unused infrastructure with no real security benefit.
  # Tracked in architecture/MULTI_TENANT_ROLLOUT_PLAN.md Phase 6/7.
  statement {
    sid     = "ReadSourceCredentials"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:edl/sources/*",
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
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/edl/connector-runtime",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/edl/connector-runtime:log-stream:*",
    ]
  }

  # CloudWatch Metrics
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
  name   = "EdlExtractionRuntimePolicy"
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
  name               = "EdlTransformationRuntimeRole"
  assume_role_policy = data.aws_iam_policy_document.transformation_runtime_assume_role.json
  description        = "Role assumed by the transformation pipeline Lambda for curated layer processing."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "transformation_runtime_permissions" {
  # DynamoDB — read entity extraction config to determine merge behaviour.
  # Scoped to the single entity-extraction-config table for this environment.
  # GetItem only — transformation never writes configuration records.
  statement {
    sid     = "ReadEntityExtractionConfig"
    effect  = "Allow"
    actions = ["dynamodb:GetItem"]
    resources = [
      var.entity_config_table_arn,
    ]
  }

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
    sid     = "ReadWriteCuratedLayer"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.curated_layer_bucket_arn,
      "${var.curated_layer_bucket_arn}/*",
    ]
  }

  # KMS: decrypt raw data keys (written by extraction role) and generate new
  # data keys for curated layer writes.
  statement {
    sid       = "KmsDecryptEncrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  # CloudWatch Logs — write Lambda execution logs.
  # CreateLogGroup intentionally excluded: the log group is pre-created by the
  # transformation_lambda Terraform module with correct retention and encryption.
  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlTransformationPipeline",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlTransformationPipeline:log-stream:*",
    ]
  }

  # CloudWatch Metrics — emit transformation pipeline metrics.
  # Namespace-scoped condition prevents emission to unrelated namespaces.
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
      "arn:aws:glue:${local.region}:${local.account_id}:database/edl_*",
      "arn:aws:glue:${local.region}:${local.account_id}:table/edl_*/*",
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
  name   = "EdlTransformationRuntimePolicy"
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
  name               = "EdlEntityResolutionRuntimeRole"
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

  # Entity type registry — read-only (ARCH-2). Registration (PutItem) is an
  # onboarding/admin operation, not a runtime one — not granted here.
  statement {
    sid       = "ReadEntityTypeRegistry"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [var.entity_type_registry_table_arn]
  }

  # KMS: decrypt curated data keys and generate new data keys for analytics writes.
  # Reuses the same storage KMS key used by transformation (curated + analytics
  # buckets share the storage key).
  statement {
    sid       = "KmsDecryptEncrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  # CloudWatch Logs — write Lambda execution logs.
  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlEntityResolutionPipeline",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlEntityResolutionPipeline:log-stream:*",
    ]
  }

  # CloudWatch Metrics — emit entity resolution pipeline metrics.
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
  name   = "EdlEntityResolutionRuntimePolicy"
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
  name               = "EdlAnalyticsPublisherRuntimeRole"
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

  # Entity type registry — read-only (ARCH-2).
  statement {
    sid       = "ReadEntityTypeRegistry"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [var.entity_type_registry_table_arn]
  }

  # KMS: decrypt golden record data keys and generate new keys for BI writes.
  statement {
    sid       = "KmsDecryptEncrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_transformation
  }

  # CloudWatch Logs — write Lambda execution logs.
  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlAnalyticsLayerPublisher",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlAnalyticsLayerPublisher:log-stream:*",
    ]
  }

  # CloudWatch Metrics — emit analytics publisher pipeline metrics.
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
      "arn:aws:glue:${local.region}:${local.account_id}:database/edl_analytics",
      "arn:aws:glue:${local.region}:${local.account_id}:table/edl_analytics/*",
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
  name   = "EdlAnalyticsPublisherRuntimePolicy"
  role   = aws_iam_role.analytics_publisher_runtime.id
  policy = data.aws_iam_policy_document.analytics_publisher_runtime_permissions.json
}

# ---------------------------------------------------------------------------
# Serving Store Loader Role
# Assumed by the serving store loader Lambda (LoadServingStore state).
# ---------------------------------------------------------------------------

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
  name               = "EdlServingStoreLoaderRuntimeRole"
  assume_role_policy = data.aws_iam_policy_document.serving_store_loader_runtime_assume_role.json
  description        = "Role assumed by the serving store loader Lambda for relational BI-store loads."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "serving_store_loader_runtime_permissions" {
  # Read analytics layer Parquet — the loader never writes to this bucket.
  statement {
    sid       = "ReadAnalyticsLayer"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.analytics_layer_bucket_arn, "${var.analytics_layer_bucket_arn}/*"]
  }

  # Serving store config — read-only; onboarding writes go through the control plane, not this role.
  statement {
    sid       = "ReadServingStoreConfig"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [var.serving_store_config_table_arn]
  }

  # Writer credential(s) plus the edl/serving-store/* reader-credential prefix this
  # role provisions per tenant (CreateSecret is scoped to that same name prefix —
  # it can never create a secret named outside edl/serving-store/*, OWASP A05).
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

  # KMS — decrypt the CMK-encrypted writer credential secret (the AWS-managed RDS
  # master secret is encrypted with the secrets KMS key). Without this, GetSecretValue
  # fails with an AccessDenied on kms:Decrypt. Empty list → no statement emitted.
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
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlServingStoreLoader",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlServingStoreLoader:log-stream:*",
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
  name   = "EdlServingStoreLoaderRuntimePolicy"
  role   = aws_iam_role.serving_store_loader_runtime.id
  policy = data.aws_iam_policy_document.serving_store_loader_runtime_permissions.json
}

# ---------------------------------------------------------------------------
# Twin Builder Runtime Role (BuildTwin Step Functions stage)
# ---------------------------------------------------------------------------

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
  name               = "EdlTwinBuilderRuntimeRole"
  assume_role_policy = data.aws_iam_policy_document.twin_build_runtime_assume_role.json
  description        = "Role assumed by the twin builder Lambda to read analytics golden records and write the twin index."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "twin_build_runtime_permissions" {
  # Read analytics golden records; write intermediate edge Parquet back to the same bucket.
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

  # Relationship-rules config lives alongside the entity-resolution config in the curated bucket.
  statement {
    sid       = "ReadRelationshipRulesConfig"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.curated_layer_bucket_arn, "${var.curated_layer_bucket_arn}/*"]
  }

  # Write the twin index; read the entity-type registry to resolve entity_id → entity_type.
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
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlTwinBuilder",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlTwinBuilder:log-stream:*",
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
  name   = "EdlTwinBuilderRuntimePolicy"
  role   = aws_iam_role.twin_build_runtime.id
  policy = data.aws_iam_policy_document.twin_build_runtime_permissions.json
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
  name               = "EdlTransformationJobRole"
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
    sid    = "WriteTransformationLogs"
    effect = "Allow"
    # logs:CreateLogGroup intentionally excluded — the log group is pre-created
    # by the Terraform observability module.  Granting CreateLogGroup would allow
    # the job to create arbitrary log groups in this account.
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/edl/transformation",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/edl/transformation:log-stream:*",
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
      "arn:aws:glue:${local.region}:${local.account_id}:database/edl_*",
      "arn:aws:glue:${local.region}:${local.account_id}:table/edl_*/*",
    ]
  }
}

resource "aws_iam_role_policy" "transformation_job" {
  name   = "EdlTransformationJobPolicy"
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
  name               = "EdlExtractionOrchestrationWorkflowRole"
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
      "arn:aws:lambda:${local.region}:${local.account_id}:function:EdlExtractionPipeline",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:EdlTransformationPipeline",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:EdlEntityResolutionPipeline",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:EdlAnalyticsLayerPublisher",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:EdlTwinBuilder",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:EdlServingStoreLoader",
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
  name   = "EdlExtractionOrchestrationWorkflowPolicy"
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
  name               = "EdlExtractionScheduleTriggerRole"
  assume_role_policy = data.aws_iam_policy_document.eventbridge_scheduler_assume_role.json
  description        = "Role assumed by EventBridge Scheduler to start extraction Step Functions workflows."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "eventbridge_scheduler_permissions" {
  # Allow sending to the SQS FIFO pipeline trigger queue (new burst-buffer architecture)
  statement {
    sid     = "SendToPipelineTriggerQueue"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]
    resources = [
      "arn:aws:sqs:${local.region}:${local.account_id}:EdlPipelineTrigger.fifo",
    ]
  }
  # Keep direct Step Functions access as fallback for manual / replay triggers
  statement {
    sid     = "StartExtractionWorkflows"
    effect  = "Allow"
    actions = ["states:StartExecution"]
    resources = [
      "arn:aws:states:${local.region}:${local.account_id}:stateMachine:EdlExtractionPipeline",
    ]
  }
}

resource "aws_iam_role_policy" "eventbridge_scheduler" {
  name   = "EdlExtractionScheduleTriggerPolicy"
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
  name               = "EdlCicdDeploymentRole"
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

# ---------------------------------------------------------------------------
# Pipeline Trigger Lambda Role (§1.6)
# Assumed by the pipeline_trigger Lambda to:
#   - Read + delete from the SQS FIFO pipeline trigger queue
#   - Start Step Functions executions (scoped to extraction pipeline only)
# ---------------------------------------------------------------------------

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
  name               = "EdlPipelineTriggerRole"
  assume_role_policy = data.aws_iam_policy_document.pipeline_trigger_assume_role.json
  description        = "Role assumed by the pipeline trigger Lambda to drain SQS and start Step Functions executions."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "pipeline_trigger_permissions" {
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
      "arn:aws:sqs:${local.region}:${local.account_id}:EdlPipelineTrigger.fifo",
    ]
  }

  statement {
    sid     = "StartExtractionPipelineExecution"
    effect  = "Allow"
    actions = ["states:StartExecution"]
    resources = [
      "arn:aws:states:${local.region}:${local.account_id}:stateMachine:EdlExtractionPipeline",
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
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlPipelineTrigger",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlPipelineTrigger:log-stream:*",
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
  name   = "EdlPipelineTriggerPolicy"
  role   = aws_iam_role.pipeline_trigger.id
  policy = data.aws_iam_policy_document.pipeline_trigger_permissions.json
}

# ---------------------------------------------------------------------------
# DLQ Processor Lambda Role (§4.4)
# ---------------------------------------------------------------------------

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
  name               = "EdlDlqProcessorRole"
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
    resources = [var.dlq_arn]
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
    resources = ["arn:aws:sns:${local.region}:${local.account_id}:EdlPlatformAlerts"]
  }

  statement {
    sid     = "ReplayFailedRuns"
    effect  = "Allow"
    actions = ["states:StartExecution"]
    resources = [
      "arn:aws:states:${local.region}:${local.account_id}:stateMachine:EdlExtractionPipeline",
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
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlDlqProcessor",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlDlqProcessor:log-stream:*",
    ]
  }
}

resource "aws_iam_role_policy" "dlq_processor" {
  name   = "EdlDlqProcessorPolicy"
  role   = aws_iam_role.dlq_processor.id
  policy = data.aws_iam_policy_document.dlq_processor_permissions.json
}

# ---------------------------------------------------------------------------
# Credential Expiry Notifier Lambda role (SEC-6)
# Checks source-credential secret age on a daily schedule and publishes an
# SNS alert when a secret is approaching or past its rotation window.
# DescribeSecret only — never GetSecretValue; this role cannot read secret
# values, only Secrets Manager metadata (CreatedDate / LastRotatedDate).
# ---------------------------------------------------------------------------

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
  name               = "EdlCredentialExpiryNotifierRole"
  assume_role_policy = data.aws_iam_policy_document.credential_expiry_notifier_assume_role.json
  description        = "Role assumed by the credential expiry notifier Lambda (SEC-6). Read-only secret metadata + SNS publish."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "credential_expiry_notifier_permissions" {
  statement {
    sid       = "DescribeSourceCredentialSecrets"
    effect    = "Allow"
    actions   = ["secretsmanager:DescribeSecret"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:edl/sources/*"]
  }

  statement {
    sid       = "PublishAlertNotification"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = ["arn:aws:sns:${local.region}:${local.account_id}:EdlPlatformAlerts"]
  }

  statement {
    sid     = "WriteLambdaExecutionLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlCredentialExpiryNotifier",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlCredentialExpiryNotifier:log-stream:*",
    ]
  }

  # Required for Lambda to decrypt its own KMS-encrypted environment variables
  # at invocation time — without this the function fails to start.
  statement {
    sid       = "KmsDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = var.kms_key_arns_for_credential_expiry_notifier
  }
}

resource "aws_iam_role_policy" "credential_expiry_notifier" {
  name   = "EdlCredentialExpiryNotifierPolicy"
  role   = aws_iam_role.credential_expiry_notifier.id
  policy = data.aws_iam_policy_document.credential_expiry_notifier_permissions.json
}

# EventBridge Scheduler role to invoke the notifier Lambda on its daily
# schedule. Separate from `eventbridge_scheduler` above (which is scoped to
# extraction workflow triggering) — this one may only invoke this one Lambda.
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
  name               = "EdlCredentialExpirySchedulerRole"
  assume_role_policy = data.aws_iam_policy_document.credential_expiry_scheduler_assume_role.json
  description        = "Role assumed by EventBridge Scheduler to invoke the credential expiry notifier Lambda (SEC-6)."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "credential_expiry_scheduler_permissions" {
  statement {
    sid       = "InvokeCredentialExpiryNotifier"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${local.region}:${local.account_id}:function:EdlCredentialExpiryNotifier"]
  }
}

resource "aws_iam_role_policy" "credential_expiry_scheduler" {
  name   = "EdlCredentialExpirySchedulerPolicy"
  role   = aws_iam_role.credential_expiry_scheduler.id
  policy = data.aws_iam_policy_document.credential_expiry_scheduler_permissions.json
}

# ---------------------------------------------------------------------------
# Control-Plane API Lambda Role
# Assumed by the control-plane API Lambda (connector_runtime/api) to:
#   - provision tenants and register entity configs (read/write)
#   - trigger pipeline runs by enqueueing to the SAME pipeline-trigger FIFO
#     queue that pipeline_trigger_handler.py consumes (no parallel
#     states:StartExecution path)
#   - read run status/history from the run audit log
# Read-heavy: PutItem is only granted on the entity-config and
# entity-type-registry tables (tenant provisioning / entity registration);
# the run-audit-log table is read-only from this role — audit records are
# written exclusively by RunCoordinator in the pipeline runtime.
# ---------------------------------------------------------------------------

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
  name               = "EdlControlPlaneRole"
  assume_role_policy = data.aws_iam_policy_document.control_plane_assume_role.json
  description        = "Role assumed by the control-plane API Lambda for tenant provisioning, entity registration, pipeline triggering, and run status queries."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "control_plane_permissions" {
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

  # Intelligence layer: read twins and semantic models; read/write saved queries.
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

  # Read analytics golden records to execute semantic queries.
  statement {
    sid       = "ReadAnalyticsLayer"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.analytics_layer_bucket_arn, "${var.analytics_layer_bucket_arn}/*"]
  }

  # Enqueue to the same SQS FIFO queue pipeline_trigger_handler.py consumes —
  # constructed by naming convention (matches the pattern already used by
  # the eventbridge_scheduler and pipeline_trigger roles above) rather than
  # threaded through as a variable, to avoid a module dependency cycle
  # between iam and orchestration.
  statement {
    sid     = "SendToPipelineTriggerQueue"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]
    resources = [
      "arn:aws:sqs:${local.region}:${local.account_id}:EdlPipelineTrigger.fifo",
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
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlControlPlane",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlControlPlane:log-stream:*",
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
  name   = "EdlControlPlanePolicy"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.control_plane_permissions.json
}
