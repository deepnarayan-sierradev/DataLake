terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

locals {
  common_tags = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "metadata-persistence"
  })
}

# ---------------------------------------------------------------------------
# Watermark Repository — DynamoDB
# Stores last successful watermark per source/entity/environment.
# Optimistic concurrency: all writes use condition expressions.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "watermark_repository" {
  name         = "EdlWatermarkRepository"
  billing_mode = "PAY_PER_REQUEST" # Auto-scales; no capacity planning for control plane data

  hash_key  = "source_id"
  range_key = "entity_id"

  attribute {
    name = "source_id"
    type = "S"
  }

  attribute {
    name = "entity_id"
    type = "S"
  }

  attribute {
    name = "environment"
    type = "S"
  }

  attribute {
    name = "last_successful_watermark"
    type = "S"
  }

  # GSI: query all watermarks for a given environment (for operational dashboards)
  global_secondary_index {
    name            = "environment-watermark-index"
    hash_key        = "environment"
    range_key       = "last_successful_watermark"
    projection_type = "ALL"
  }

  # Encryption at rest with customer-managed KMS key
  server_side_encryption {
    enabled     = true
    kms_key_arn = var.database_kms_key_arn
  }

  # Point-in-time recovery — enables restoration to any second in the past 35 days
  point_in_time_recovery {
    enabled = true
  }

  # DynamoDB Streams: disabled for watermark table (not needed for this use case)
  stream_enabled = false

  # Prevent accidental destruction — watermark state is irreplaceable in production.
  lifecycle {
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name    = "EdlWatermarkRepository"
    Purpose = "watermark-state"
  })
}

# ---------------------------------------------------------------------------
# Run Audit Log — DynamoDB
# Immutable record of every pipeline run stage boundary.
# TTL enabled for cost-controlled retention (configurable per environment).
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "run_audit_log" {
  name         = "EdlRunAuditLog"
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "run_id"
  range_key = "stage"

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "stage"
    type = "S"
  }

  attribute {
    name = "source_entity_key" # Composite: source_id#entity_id
    type = "S"
  }

  attribute {
    name = "started_at"
    type = "S"
  }

  # GSI: query run history for a source+entity pair ordered by time
  global_secondary_index {
    name            = "source-entity-time-index"
    hash_key        = "source_entity_key"
    range_key       = "started_at"
    projection_type = "ALL"
  }

  # TTL: automatically expire old audit records (archival occurs before TTL if needed)
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.database_kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }

  stream_enabled = false

  # Prevent accidental destruction — run audit log is an immutable compliance record.
  lifecycle {
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name    = "EdlRunAuditLog"
    Purpose = "pipeline-audit-trail"
  })
}

# ---------------------------------------------------------------------------
# Entity Extraction Config — DynamoDB
# Configuration records for each source entity (load type, watermark field,
# field mappings, etc.). Read-only by the extraction and transformation runtimes.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "entity_extraction_config" {
  name         = "EdlEntityExtractionConfig"
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "source_id"
  range_key = "entity_id"

  attribute {
    name = "source_id"
    type = "S"
  }

  attribute {
    name = "entity_id"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.database_kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }

  stream_enabled = false

  # Prevent accidental destruction — entity extraction config is the source of
  # truth for all pipeline behaviour; recreating it requires manual re-seeding.
  lifecycle {
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name    = "EdlEntityExtractionConfig"
    Purpose = "entity-extraction-config"
  })
}

# ---------------------------------------------------------------------------
# Entity Type Registry (ARCH-2)
#
# Single-table design, PK=tenant_code:
#   - Per-entity_id item:   sk = "entity_id#{entity_id}"     -> {entity_type}
#   - Per-entity_type item: sk = "entity_type#{entity_type}" -> {pk_field, contributing_sources}
#
# Replaces the hardcoded ENTITY_ID_TO_TYPE / ENTITY_TYPE_PK_FIELD /
# ENTITY_TYPE_SOURCES dicts in entity_resolution/entity_type_registry.py —
# those constants remain as seed data / fallback for entities not yet
# migrated to this table (see EntityTypeRegistryClient's docstring).
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "entity_type_registry" {
  name         = "EdlEntityTypeRegistry"
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "tenant_code"
  range_key = "sk"

  attribute {
    name = "tenant_code"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.database_kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }

  stream_enabled = false

  lifecycle {
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name    = "EdlEntityTypeRegistry"
    Purpose = "entity-type-registry"
  })
}

# ---------------------------------------------------------------------------
# Serving Store Config — DynamoDB
# Which tenant/entity pairs load into a serving store, and into which engine.
# Tenant-partitioned from creation (PK=tenant_code), unlike the legacy tables
# above — no tenant_scoped_key() composite-key workaround needed here.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "serving_store_config" {
  name         = "EdlServingStoreConfig"
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "tenant_code"
  range_key = "entity_type"

  attribute {
    name = "tenant_code"
    type = "S"
  }

  attribute {
    name = "entity_type"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.database_kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }

  stream_enabled = false

  lifecycle {
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name    = "EdlServingStoreConfig"
    Purpose = "serving-store-config"
  })
}

# ---------------------------------------------------------------------------
# Dead-Letter Queue — SQS
# Receives terminal pipeline failures for manual review and replay.
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "extraction_failure_dlq" {
  name = "EdlExtractionFailureDlq"

  # Message retention: 14 days (maximum) — gives operations team time to investigate
  message_retention_seconds = 1209600

  # KMS encryption at rest
  kms_master_key_id                 = var.database_kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  # Must exceed the DLQ processor Lambda's timeout (60s, orchestration module)
  # or CreateEventSourceMapping is rejected; sized with margin for retries.
  visibility_timeout_seconds = 300

  tags = merge(local.common_tags, {
    Name    = "EdlExtractionFailureDlq"
    Purpose = "pipeline-failure-replay"
  })
}

data "aws_iam_policy_document" "dlq_policy" {
  statement {
    sid    = "AllowOrchestrationSendMessage"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = var.orchestration_role_arns
    }
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.extraction_failure_dlq.arn]
  }

  # SQS rejects a statement with an empty principal list ("No principals were
  # found"), so this statement is omitted entirely when no replay operator
  # roles are configured (e.g. dev, where replay_operator_role_arns defaults
  # to []) rather than emitting principals = [].
  dynamic "statement" {
    for_each = length(var.replay_operator_role_arns) > 0 ? [1] : []
    content {
      sid    = "AllowReplayOperatorReceive"
      effect = "Allow"
      principals {
        type        = "AWS"
        identifiers = var.replay_operator_role_arns
      }
      actions = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
      ]
      resources = [aws_sqs_queue.extraction_failure_dlq.arn]
    }
  }

  # Deny non-TLS access
  statement {
    sid    = "DenyNonTLS"
    effect = "Deny"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.extraction_failure_dlq.arn]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "extraction_failure_dlq" {
  queue_url = aws_sqs_queue.extraction_failure_dlq.id
  policy    = data.aws_iam_policy_document.dlq_policy.json
}

# ---------------------------------------------------------------------------
# Source Onboarding Registry — DynamoDB
# Tracks gate-by-gate onboarding state per source_id (spec §10.2).
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "source_onboarding_registry" {
  name         = "EdlSourceOnboardingRegistry"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "source_id"

  attribute {
    name = "source_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.database_kms_key_arn
  }

  tags = local.common_tags
}
