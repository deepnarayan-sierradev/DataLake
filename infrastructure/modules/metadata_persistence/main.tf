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
  name         = "${var.environment}-edl-watermark-repository"
  billing_mode = "PAY_PER_REQUEST" # Auto-scales; no capacity planning for control plane data

  hash_key  = "source_id"
  range_key = "entity_id_env" # Composite: entity_id#environment

  attribute {
    name = "source_id"
    type = "S"
  }

  attribute {
    name = "entity_id_env"
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
    name               = "environment-watermark-index"
    hash_key           = "environment"
    range_key          = "last_successful_watermark"
    projection_type    = "ALL"
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

  tags = merge(local.common_tags, {
    Name       = "${var.environment}-edl-watermark-repository"
    Purpose    = "watermark-state"
  })
}

# ---------------------------------------------------------------------------
# Run Audit Log — DynamoDB
# Immutable record of every pipeline run stage boundary.
# TTL enabled for cost-controlled retention (configurable per environment).
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "run_audit_log" {
  name         = "${var.environment}-edl-run-audit-log"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "run_id"

  attribute {
    name = "run_id"
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

  tags = merge(local.common_tags, {
    Name    = "${var.environment}-edl-run-audit-log"
    Purpose = "pipeline-audit-trail"
  })
}

# ---------------------------------------------------------------------------
# Dead-Letter Queue — SQS
# Receives terminal pipeline failures for manual review and replay.
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "extraction_failure_dlq" {
  name = "${var.environment}-edl-extraction-failure-dlq"

  # Message retention: 14 days (maximum) — gives operations team time to investigate
  message_retention_seconds = 1209600

  # KMS encryption at rest
  kms_master_key_id                 = var.database_kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  visibility_timeout_seconds = 30

  tags = merge(local.common_tags, {
    Name    = "${var.environment}-edl-extraction-failure-dlq"
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

  statement {
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
  name         = "${var.environment}-source-onboarding-registry"
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
