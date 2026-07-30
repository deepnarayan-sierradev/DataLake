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
    Module      = "serving_store_redshift"
  })
  identifier = "${var.name_prefix}-serving-store-redshift-${var.environment}"
}


data "aws_iam_policy_document" "copy_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["redshift.amazonaws.com", "redshift-serverless.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "copy_s3_read" {
  statement {
    sid       = "AnalyticsBucketList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.analytics_bucket_arn]
  }
  statement {
    sid       = "AnalyticsObjectRead"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.analytics_bucket_arn}/*"]
  }
  statement {
    sid       = "AnalyticsKmsDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [var.analytics_kms_key_arn]
  }
}

resource "aws_iam_role" "copy" {
  name               = "${local.identifier}-copy-role"
  assume_role_policy = data.aws_iam_policy_document.copy_assume.json
  tags               = merge(local.common_tags, { Name = "${local.identifier}-copy-role" })
}

resource "aws_iam_role_policy" "copy_s3_read" {
  name   = "${local.identifier}-copy-s3-read"
  role   = aws_iam_role.copy.id
  policy = data.aws_iam_policy_document.copy_s3_read.json
}


resource "aws_security_group" "redshift" {
  #checkov:skip=CKV2_AWS_5:Attached by the caller to the Redshift workgroup.
  name        = "${local.identifier}-sg"
  description = "Serving store Redshift Serverless. Ingress wired by the caller from the loader Lambda's SG."
  vpc_id      = var.vpc_id

  tags = merge(local.common_tags, { Name = "${local.identifier}-sg" })

  lifecycle {
    create_before_destroy = true
  }
}


resource "aws_redshiftserverless_namespace" "serving_store" {
  namespace_name = local.identifier
  db_name        = var.database_name

  kms_key_id                       = var.storage_kms_key_arn
  manage_admin_password            = true
  admin_password_secret_kms_key_id = var.secrets_kms_key_arn

  default_iam_role_arn = aws_iam_role.copy.arn
  iam_roles            = [aws_iam_role.copy.arn]

  log_exports = ["userlog", "connectionlog", "useractivitylog"]

  tags = merge(local.common_tags, { Name = local.identifier })
}

resource "aws_redshiftserverless_workgroup" "serving_store" {
  namespace_name = aws_redshiftserverless_namespace.serving_store.namespace_name
  workgroup_name = local.identifier

  base_capacity        = var.base_capacity_rpu
  publicly_accessible  = false
  enhanced_vpc_routing = true

  subnet_ids         = var.subnet_ids
  security_group_ids = [aws_security_group.redshift.id]

  tags = merge(local.common_tags, { Name = local.identifier })
}


resource "aws_secretsmanager_secret" "connection" {
  #checkov:skip=CKV2_AWS_57:Credential for externally-operated Redshift; rotation is the operator's action.
  name       = "${var.name_prefix}/${var.environment}/serving-store/redshift/connection"
  kms_key_id = var.secrets_kms_key_arn
  tags       = merge(local.common_tags, { Name = "${local.identifier}-connection" })
}

resource "aws_secretsmanager_secret_version" "connection" {
  secret_id = aws_secretsmanager_secret.connection.id
  secret_string = jsonencode({
    host          = aws_redshiftserverless_workgroup.serving_store.endpoint[0].address
    port          = tostring(aws_redshiftserverless_workgroup.serving_store.endpoint[0].port)
    workgroup     = aws_redshiftserverless_workgroup.serving_store.workgroup_name
    database      = var.database_name
    region        = data.aws_region.current.name
    copy_iam_role = aws_iam_role.copy.arn
  })
}

data "aws_region" "current" {}
