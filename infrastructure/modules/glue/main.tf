locals {
  curated_db_name   = "${replace(var.name_prefix, "-", "_")}_curated_${var.environment}"
  analytics_db_name = "${replace(var.name_prefix, "-", "_")}_analytics_${var.environment}"
}


resource "aws_glue_catalog_database" "curated" {
  name        = local.curated_db_name
  description = "AWS Glue Data Catalog database for curated domain datasets."

  create_table_default_permission {
    permissions = ["SELECT"]
    principal {
      data_lake_principal_identifier = "IAM_ALLOWED_PRINCIPALS"
    }
  }

  tags = var.tags
}


resource "aws_glue_catalog_database" "analytics" {
  name        = local.analytics_db_name
  description = "AWS Glue Data Catalog database for analytics consumption datasets."

  create_table_default_permission {
    permissions = ["SELECT"]
    principal {
      data_lake_principal_identifier = "IAM_ALLOWED_PRINCIPALS"
    }
  }

  tags = var.tags
}


resource "aws_lakeformation_permissions" "curated_readers" {
  for_each = toset(var.analytics_reader_principals)

  principal   = each.value
  permissions = ["SELECT", "DESCRIBE"]

  table {
    database_name = aws_glue_catalog_database.curated.name
    wildcard      = true
  }
}

resource "aws_lakeformation_permissions" "analytics_readers" {
  for_each = toset(var.analytics_reader_principals)

  principal   = each.value
  permissions = ["SELECT", "DESCRIBE"]

  table {
    database_name = aws_glue_catalog_database.analytics.name
    wildcard      = true
  }
}


data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_glue_resource_policy" "catalog_account_isolation" {
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyExternalCatalogAccess"
        Effect    = "Deny"
        Principal = "*"
        Action    = "glue:*"
        Resource  = "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:catalog"
        Condition = {
          StringNotEquals = {
            "aws:PrincipalAccount" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })
}


resource "aws_athena_workgroup" "analytics" {
  name        = "${var.name_prefix}-analytics-${var.environment}"
  description = "Athena workgroup for analytics layer queries."
  state       = "ENABLED"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = 10 * 1024 * 1024 * 1024 # 10 GB guard rail

    result_configuration {
      output_location = "s3://${var.athena_results_bucket_id}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key_arn       = var.kms_key_arn
      }
    }

    engine_version {
      selected_engine_version = "Athena engine version 3"
    }
  }

  tags = var.tags
}
