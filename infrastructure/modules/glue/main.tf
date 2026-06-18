locals {
  curated_db_name   = "${var.environment}_edl_curated"
  analytics_db_name = "${var.environment}_edl_analytics"
}

# ---------------------------------------------------------------------------
# Glue Data Catalog — curated layer database
# ---------------------------------------------------------------------------

resource "aws_glue_catalog_database" "curated" {
  name        = local.curated_db_name
  description = "AWS Glue Data Catalog database for ${var.environment} curated domain datasets."

  create_table_default_permission {
    permissions = ["SELECT"]
    principal {
      data_lake_principal_identifier = "IAM_ALLOWED_PRINCIPALS"
    }
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# Glue Data Catalog — analytics layer database
# ---------------------------------------------------------------------------

resource "aws_glue_catalog_database" "analytics" {
  name        = local.analytics_db_name
  description = "AWS Glue Data Catalog database for ${var.environment} analytics consumption datasets."

  create_table_default_permission {
    permissions = ["SELECT"]
    principal {
      data_lake_principal_identifier = "IAM_ALLOWED_PRINCIPALS"
    }
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# Glue resource policy — deny catalog access from outside the account
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Athena Workgroup — per-environment, query results encrypted with KMS
# ---------------------------------------------------------------------------

resource "aws_athena_workgroup" "analytics" {
  name        = "${var.environment}-edl-analytics"
  description = "Athena workgroup for ${var.environment} analytics layer queries."
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
