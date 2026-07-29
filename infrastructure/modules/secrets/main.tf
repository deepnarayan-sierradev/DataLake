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

# ---------------------------------------------------------------------------
# Source credentials secrets
# Each source gets a dedicated secret. Runtime retrieves via GetSecretValue.
# Secret values are NEVER set here — populated by the secrets onboarding runbook.
# Rotation: configured at secret level; Lambda rotation function wired in Phase 10.
# ---------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "salesforce_credentials" {
  name        = "edl/sources/salesforce/credentials"
  description = "Salesforce OAuth 2.0 client credentials for the extraction runtime."
  kms_key_id  = var.secrets_kms_key_arn

  # Rotation schedule: configure after onboarding the rotation Lambda
  # rotation_lambda_arn = var.salesforce_rotation_lambda_arn
  # rotation_rules { automatically_after_days = 90 }

  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(local.common_tags, {
    Name   = "EdlSalesforceCredentials"
    Source = "salesforce"
  })
}

resource "aws_secretsmanager_secret" "netsuite_credentials" {
  name        = "edl/sources/netsuite/credentials"
  description = "NetSuite OAuth 2.0 / TBA credentials for the extraction runtime."
  kms_key_id  = var.secrets_kms_key_arn

  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(local.common_tags, {
    Name   = "EdlNetsuiteCredentials"
    Source = "netsuite"
  })
}

resource "aws_secretsmanager_secret" "mysql_rds_credentials" {
  name        = "edl/sources/mysql-rds/credentials"
  description = "MySQL RDS read-only credentials for the extraction runtime."
  kms_key_id  = var.secrets_kms_key_arn

  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(local.common_tags, {
    Name   = "EdlMysqlRdsCredentials"
    Source = "mysql-rds"
  })
}

# SEC-3: Sage credentials were previously created out-of-band (no Terraform
# resource, no DenyAllOtherPrincipals policy) — sage_credential_manager.py
# reads {environment}/sources/sage/{product}/credentials for both supported
# products (see connector_runtime/adapters/sage/common/sage_product_registry.py
# SUPPORTED_SAGE_PRODUCTS). Bringing these under Terraform closes that gap.

resource "aws_secretsmanager_secret" "sage_intacct_credentials" {
  name        = "edl/sources/sage/intacct/credentials"
  description = "Sage Intacct web services credentials for the extraction runtime."
  kms_key_id  = var.secrets_kms_key_arn

  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(local.common_tags, {
    Name    = "EdlSageIntacctCredentials"
    Source  = "sage"
    Product = "intacct"
  })
}

resource "aws_secretsmanager_secret" "sage_x3_credentials" {
  name        = "edl/sources/sage/x3/credentials"
  description = "Sage X3 API credentials for the extraction runtime."
  kms_key_id  = var.secrets_kms_key_arn

  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(local.common_tags, {
    Name    = "EdlSageX3Credentials"
    Source  = "sage"
    Product = "x3"
  })
}

# ---------------------------------------------------------------------------
# Resource-based policies on secrets
# Only the extraction runtime role is permitted to read secret values.
# The Secrets Manager console and root can manage; runtime cannot rotate.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Automatic secret rotation — activated when rotation Lambda ARNs are provided.
# Rotation Lambdas are deployed via the Phase 10 runbook.  Until then, manual
# rotation must be performed on a quarterly schedule per the operations runbook.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Credential Expiry Notifier (SEC-6)
#
# None of the three rotation_lambda_arn variables above are set in any
# environment today, so automatic rotation is inert everywhere. Until a
# per-connector rotation Lambda is built (separate integration work per
# source auth system), this Lambda closes the observability half of the gap:
# a daily check of every source-credential secret's age, alerting via SNS
# when a secret is approaching or past its rotation window.
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "credential_expiry_notifier" {
  function_name = "EdlCredentialExpiryNotifier"
  description   = "Daily check of source-credential secret age; publishes an SNS alert when rotation is overdue (SEC-6)."

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  handler     = "connector_runtime.credential_rotation.credential_expiry_notifier_handler.lambda_handler"
  runtime     = "python3.13"
  timeout     = 60
  memory_size = 256

  # A daily expiry sweep needs almost no concurrency; a ceiling of 2 leaves headroom for a
  # retry without letting a schedule storm consume the pool.
  reserved_concurrent_executions = var.reserved_concurrent_executions

  role = var.credential_expiry_notifier_role_arn

  environment {
    variables = {
      PLATFORM_ENVIRONMENT = var.environment
      # AWS_REGION is a reserved Lambda environment variable injected
      # automatically by the runtime — setting it explicitly is rejected by
      # CreateFunction with InvalidParameterValueException.
      SOURCE_CREDENTIAL_SECRET_ARNS = join(",", local.all_source_credential_secret_arns)
      ALERT_SNS_TOPIC_ARN           = var.alert_topic_arn
      ROTATION_WARNING_DAYS         = tostring(var.rotation_warning_days)
      SECRET_ROTATION_DAYS          = tostring(var.secret_rotation_days)
    }
  }

  # Uses the logs KMS key (allow_cloudwatch_logs enabled), not
  # secrets_kms_key_arn — the secrets key's policy only covers the 5 source
  # credential secrets themselves, not this Lambda's own env var encryption.
  kms_key_arn = var.logs_kms_key_arn

  tracing_config {
    mode = var.enable_xray_tracing ? "Active" : "PassThrough"
  }

  tags = merge(local.common_tags, {
    Name = "EdlCredentialExpiryNotifier"
  })
}

resource "aws_cloudwatch_log_group" "credential_expiry_notifier" {
  name              = "/aws/lambda/EdlCredentialExpiryNotifier"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn

  tags = merge(local.common_tags, {
    Name = "/aws/lambda/EdlCredentialExpiryNotifier"
  })
}

resource "aws_scheduler_schedule" "credential_expiry_notifier_daily" {
  name       = "EdlCredentialExpiryCheck"
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
