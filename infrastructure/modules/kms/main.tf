terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}


resource "aws_kms_key" "this" {
  description             = var.description
  deletion_window_in_days = var.deletion_window_in_days

  enable_key_rotation = true

  multi_region = false

  policy = var.key_policy != null ? var.key_policy : data.aws_iam_policy_document.default_key_policy.json

  tags = merge(var.tags, {
    Name        = "${var.name_prefix}-${var.capability}-${var.environment}-kms-key"
    Environment = var.environment
    Capability  = var.capability
    ManagedBy   = "terraform"
  })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name_prefix}-${var.capability}-${var.environment}"
  target_key_id = aws_kms_key.this.key_id
}


data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "default_key_policy" {
  #checkov:skip=CKV_AWS_109:Key policy resource is the key itself.
  #checkov:skip=CKV_AWS_111:Key policy resource is the key itself; principals are enumerated.
  #checkov:skip=CKV_AWS_356:A key policy cannot name its own ARN as a resource.
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
