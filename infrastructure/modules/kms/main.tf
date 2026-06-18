terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# ---------------------------------------------------------------------------
# KMS key
# ---------------------------------------------------------------------------

resource "aws_kms_key" "this" {
  description             = var.description
  deletion_window_in_days = var.deletion_window_in_days

  # Mandatory: annual automatic rotation — never disable
  enable_key_rotation = true

  # Stay single-region unless explicitly needed (multi_region adds cost and complexity)
  multi_region = false

  policy = var.key_policy != null ? var.key_policy : data.aws_iam_policy_document.default_key_policy.json

  tags = merge(var.tags, {
    Name        = "${var.environment}-${var.capability}-kms-key"
    Environment = var.environment
    Capability  = var.capability
    ManagedBy   = "terraform"
  })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.environment}-${var.capability}"
  target_key_id = aws_kms_key.this.key_id
}

# ---------------------------------------------------------------------------
# Default key policy
# Principle of least privilege: only account root and the listed service
# principals can use this key. No wildcard resources or actions.
# ---------------------------------------------------------------------------

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "default_key_policy" {
  # Allow account root full access — required for key management
  statement {
    sid    = "AllowAccountRoot"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
    actions   = ["kms:*"]
    resources = ["*"]
  }

  # Allow specified IAM roles to use the key (encrypt/decrypt only — no admin)
  dynamic "statement" {
    for_each = length(var.key_user_role_arns) > 0 ? [1] : []
    content {
      sid    = "AllowKeyUsers"
      effect = "Allow"
      principals {
        type        = "AWS"
        identifiers = var.key_user_role_arns
      }
      actions = [
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:DescribeKey",
        "kms:ReEncryptFrom",
        "kms:ReEncryptTo",
      ]
      resources = ["*"]
    }
  }

  # Allow CloudWatch Logs service to use the key for log group encryption
  dynamic "statement" {
    for_each = var.allow_cloudwatch_logs ? [1] : []
    content {
      sid    = "AllowCloudWatchLogs"
      effect = "Allow"
      principals {
        type        = "Service"
        identifiers = ["logs.${var.aws_region}.amazonaws.com"]
      }
      actions = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:ReEncryptFrom",
        "kms:ReEncryptTo",
        "kms:GenerateDataKey",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:DescribeKey",
      ]
      resources = ["*"]
      condition {
        test     = "ArnLike"
        variable = "kms:EncryptionContext:aws:logs:arn"
        values   = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
      }
    }
  }

  # Allow SNS service to use the key for topic encryption
  dynamic "statement" {
    for_each = var.allow_sns ? [1] : []
    content {
      sid    = "AllowSNS"
      effect = "Allow"
      principals {
        type        = "Service"
        identifiers = ["sns.amazonaws.com"]
      }
      actions = [
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "kms:DescribeKey",
      ]
      resources = ["*"]
    }
  }
}
