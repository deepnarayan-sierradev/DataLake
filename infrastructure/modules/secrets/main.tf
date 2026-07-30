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
    Module      = "secrets"
  })
}


resource "aws_secretsmanager_secret" "salesforce_credentials" {
  name        = "${var.name_prefix}/${var.environment}/sources/salesforce/credentials"
  description = "Salesforce OAuth 2.0 client credentials for the extraction runtime."
  kms_key_id  = var.secrets_kms_key_arn


  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(local.common_tags, {
    Name   = "${var.name_prefix}-salesforce-credentials-${var.environment}"
    Source = "salesforce"
  })
}

resource "aws_secretsmanager_secret" "netsuite_credentials" {
  name        = "${var.name_prefix}/${var.environment}/sources/netsuite/credentials"
  description = "NetSuite OAuth 2.0 / TBA credentials for the extraction runtime."
  kms_key_id  = var.secrets_kms_key_arn

  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(local.common_tags, {
    Name   = "${var.name_prefix}-netsuite-credentials-${var.environment}"
    Source = "netsuite"
  })
}

resource "aws_secretsmanager_secret" "mysql_rds_credentials" {
  name        = "${var.name_prefix}/${var.environment}/sources/mysql-rds/credentials"
  description = "MySQL RDS read-only credentials for the extraction runtime."
  kms_key_id  = var.secrets_kms_key_arn

  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(local.common_tags, {
    Name   = "${var.name_prefix}-mysql-rds-credentials-${var.environment}"
    Source = "mysql-rds"
  })
}


resource "aws_secretsmanager_secret" "sage_intacct_credentials" {
  #checkov:skip=CKV2_AWS_57:Vendor-issued credential; no programmatic rotation exists. See expiry notifier.
  name        = "${var.name_prefix}/${var.environment}/sources/sage/intacct/credentials"
  description = "Sage Intacct web services credentials for the extraction runtime."
  kms_key_id  = var.secrets_kms_key_arn

  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(local.common_tags, {
    Name    = "${var.name_prefix}-sage-intacct-credentials-${var.environment}"
    Source  = "sage"
    Product = "intacct"
  })
}

resource "aws_secretsmanager_secret" "sage_x3_credentials" {
  #checkov:skip=CKV2_AWS_57:Vendor-issued credential; no programmatic rotation exists. See expiry notifier.
  name        = "${var.name_prefix}/${var.environment}/sources/sage/x3/credentials"
  description = "Sage X3 API credentials for the extraction runtime."
  kms_key_id  = var.secrets_kms_key_arn

  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(local.common_tags, {
    Name    = "${var.name_prefix}-sage-x3-credentials-${var.environment}"
    Source  = "sage"
    Product = "x3"
  })
}


data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "source_credential_secret_policy" {
  statement {
    sid    = "AllowExtractionRuntimeReadOnly"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = var.extraction_runtime_role_arns
    }
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["*"]
  }

  statement {
    sid    = "DenyAllOtherPrincipals"
    effect = "Deny"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["*"]
    condition {
      test     = "StringNotLike"
      variable = "aws:PrincipalArn"
      values = concat(
        var.extraction_runtime_role_arns,
        ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"],
      )
    }
  }
}

resource "aws_secretsmanager_secret_policy" "salesforce" {
  secret_arn = aws_secretsmanager_secret.salesforce_credentials.arn
  policy     = data.aws_iam_policy_document.source_credential_secret_policy.json
}

resource "aws_secretsmanager_secret_policy" "netsuite" {
  secret_arn = aws_secretsmanager_secret.netsuite_credentials.arn
  policy     = data.aws_iam_policy_document.source_credential_secret_policy.json
}

resource "aws_secretsmanager_secret_policy" "mysql_rds" {
  secret_arn = aws_secretsmanager_secret.mysql_rds_credentials.arn
  policy     = data.aws_iam_policy_document.source_credential_secret_policy.json
}

resource "aws_secretsmanager_secret_policy" "sage_intacct" {
  secret_arn = aws_secretsmanager_secret.sage_intacct_credentials.arn
  policy     = data.aws_iam_policy_document.source_credential_secret_policy.json
}

resource "aws_secretsmanager_secret_policy" "sage_x3" {
  secret_arn = aws_secretsmanager_secret.sage_x3_credentials.arn
  policy     = data.aws_iam_policy_document.source_credential_secret_policy.json
}


resource "aws_secretsmanager_secret_rotation" "salesforce" {
  count               = var.salesforce_rotation_lambda_arn != null ? 1 : 0
  secret_id           = aws_secretsmanager_secret.salesforce_credentials.id
  rotation_lambda_arn = var.salesforce_rotation_lambda_arn
  rotation_rules {
    automatically_after_days = var.secret_rotation_days
  }
}

resource "aws_secretsmanager_secret_rotation" "netsuite" {
  count               = var.netsuite_rotation_lambda_arn != null ? 1 : 0
  secret_id           = aws_secretsmanager_secret.netsuite_credentials.id
  rotation_lambda_arn = var.netsuite_rotation_lambda_arn
  rotation_rules {
    automatically_after_days = var.secret_rotation_days
  }
}

resource "aws_secretsmanager_secret_rotation" "mysql_rds" {
  count               = var.mysql_rds_rotation_lambda_arn != null ? 1 : 0
  secret_id           = aws_secretsmanager_secret.mysql_rds_credentials.id
  rotation_lambda_arn = var.mysql_rds_rotation_lambda_arn
  rotation_rules {
    automatically_after_days = var.secret_rotation_days
  }
}


resource "aws_lambda_function" "credential_expiry_notifier" {
  function_name = "${var.name_prefix}-credential-expiry-notifier-${var.environment}"
  description   = "Daily check of source-credential secret age; publishes an SNS alert when rotation is overdue (SEC-6)."

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  handler                 = "connector_runtime.credential_rotation.credential_expiry_notifier_handler.lambda_handler"
  runtime                 = "python3.13"
  code_signing_config_arn = var.code_signing_config_arn

  dead_letter_config {
    target_arn = aws_sqs_queue.async_dlq.arn
  }


  dynamic "vpc_config" {

    for_each = var.vpc_id == null ? [] : [1]

    content {

      subnet_ids = var.subnet_ids

      security_group_ids = concat(var.security_group_ids, aws_security_group.secrets_lambda[*].id)

    }

  }
  timeout     = 60
  memory_size = 256

  reserved_concurrent_executions = var.reserved_concurrent_executions

  role = var.credential_expiry_notifier_role_arn

  environment {
    variables = {
      PLATFORM_ENVIRONMENT          = var.environment
      SOURCE_CREDENTIAL_SECRET_ARNS = join(",", local.all_source_credential_secret_arns)
      ALERT_SNS_TOPIC_ARN           = var.alert_topic_arn
      ROTATION_WARNING_DAYS         = tostring(var.rotation_warning_days)
      SECRET_ROTATION_DAYS          = tostring(var.secret_rotation_days)
    }
  }

  kms_key_arn = var.logs_kms_key_arn

  tracing_config {
    mode = var.enable_xray_tracing ? "Active" : "PassThrough"
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-credential-expiry-notifier-${var.environment}"
  })
}

resource "aws_cloudwatch_log_group" "credential_expiry_notifier" {
  name              = "/aws/lambda/${var.name_prefix}-credential-expiry-notifier-${var.environment}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn

  tags = merge(local.common_tags, {
    Name = "/aws/lambda/${var.name_prefix}-credential-expiry-notifier-${var.environment}"
  })
}

resource "aws_scheduler_schedule" "credential_expiry_notifier_daily" {
  name       = "${var.name_prefix}-credential-expiry-check-${var.environment}"
  group_name = "default"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = "rate(1 day)"
  kms_key_arn         = var.secrets_kms_key_arn

  target {
    arn      = aws_lambda_function.credential_expiry_notifier.arn
    role_arn = var.credential_expiry_scheduler_role_arn

    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}

locals {
  all_source_credential_secret_arns = [
    aws_secretsmanager_secret.salesforce_credentials.arn,
    aws_secretsmanager_secret.netsuite_credentials.arn,
    aws_secretsmanager_secret.mysql_rds_credentials.arn,
    aws_secretsmanager_secret.sage_intacct_credentials.arn,
    aws_secretsmanager_secret.sage_x3_credentials.arn,
  ]
}


resource "aws_sqs_queue" "async_dlq" {
  name                      = "${var.name_prefix}-credential-expiry-async-dlq-${var.environment}"
  message_retention_seconds = 1209600 # 14 days, the maximum — a DLQ that expires loses the evidence
  sqs_managed_sse_enabled   = true

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-credential-expiry-async-dlq-${var.environment}" })
}


resource "aws_security_group" "secrets_lambda" {
  #checkov:skip=CKV2_AWS_5:Attached via dynamic vpc_config in this module.
  count = var.vpc_id == null ? 0 : 1

  name        = "${var.name_prefix}-credential-expiry-notifier-${var.environment}-sg"
  description = "HTTPS egress only for the CredentialExpiryNotifier Lambda function(s)."
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS egress to AWS service endpoints."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-credential-expiry-notifier-${var.environment}-sg" })

  lifecycle {
    create_before_destroy = true
  }
}
