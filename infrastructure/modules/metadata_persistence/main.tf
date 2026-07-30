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


resource "aws_dynamodb_table" "watermark_repository" {
  name         = "${var.name_prefix}-watermark-${var.environment}"
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

  global_secondary_index {
    name            = "environment-watermark-index"
    hash_key        = "environment"
    range_key       = "last_successful_watermark"
    projection_type = "ALL"
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
    Name    = "${var.name_prefix}-watermark-${var.environment}"
    Purpose = "watermark-state"
  })
}


resource "aws_dynamodb_table" "run_audit_log" {
  name         = "${var.name_prefix}-run-audit-log-${var.environment}"
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

  global_secondary_index {
    name            = "source-entity-time-index"
    hash_key        = "source_entity_key"
    range_key       = "started_at"
    projection_type = "ALL"
  }

  attribute {
    name = "tenant_code"
    type = "S"
  }

  global_secondary_index {
    name            = "tenant-started-index"
    hash_key        = "tenant_code"
    range_key       = "started_at"
    projection_type = "ALL"
  }

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

  lifecycle {
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name    = "${var.name_prefix}-run-audit-log-${var.environment}"
    Purpose = "pipeline-audit-trail"
  })
}


resource "aws_dynamodb_table" "entity_extraction_config" {
  name         = "${var.name_prefix}-entity-extraction-config-${var.environment}"
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

  attribute {
    name = "tenant_code"
    type = "S"
  }

  global_secondary_index {
    name            = "tenant-entity-index"
    hash_key        = "tenant_code"
    range_key       = "entity_id"
    projection_type = "ALL"
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
    Name    = "${var.name_prefix}-entity-extraction-config-${var.environment}"
    Purpose = "entity-extraction-config"
  })
}


resource "aws_dynamodb_table" "entity_type_registry" {
  name         = "${var.name_prefix}-entity-type-registry-${var.environment}"
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
    Name    = "${var.name_prefix}-entity-type-registry-${var.environment}"
    Purpose = "entity-type-registry"
  })
}


resource "aws_dynamodb_table" "serving_store_config" {
  name         = "${var.name_prefix}-serving-store-config-${var.environment}"
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
    Name    = "${var.name_prefix}-serving-store-config-${var.environment}"
    Purpose = "serving-store-config"
  })
}


resource "aws_sqs_queue" "extraction_failure_dlq" {
  name = "${var.name_prefix}-extraction-failure-dlq-${var.environment}"

  message_retention_seconds = 1209600

  kms_master_key_id                 = var.database_kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  visibility_timeout_seconds = 300

  tags = merge(local.common_tags, {
    Name    = "${var.name_prefix}-extraction-failure-dlq-${var.environment}"
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


resource "aws_dynamodb_table" "source_onboarding_registry" {
  name         = "${var.name_prefix}-source-onboarding-registry-${var.environment}"
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


resource "aws_dynamodb_table" "twin_index" {
  name         = "${var.name_prefix}-twin-index-${var.environment}"
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
    Name    = "${var.name_prefix}-twin-index-${var.environment}"
    Purpose = "digital-twin-index"
  })
}


resource "aws_dynamodb_table" "semantic_model" {
  name         = "${var.name_prefix}-semantic-model-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "tenant_code"
  range_key = "model_version"

  attribute {
    name = "tenant_code"
    type = "S"
  }

  attribute {
    name = "model_version"
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
    Name    = "${var.name_prefix}-semantic-model-${var.environment}"
    Purpose = "semantic-model"
  })
}


resource "aws_dynamodb_table" "saved_query" {
  name         = "${var.name_prefix}-saved-query-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "tenant_code"
  range_key = "query_id"

  attribute {
    name = "tenant_code"
    type = "S"
  }

  attribute {
    name = "query_id"
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
    Name    = "${var.name_prefix}-saved-query-${var.environment}"
    Purpose = "saved-query"
  })
}
