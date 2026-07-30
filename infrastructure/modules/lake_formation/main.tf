
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

locals {
  common_tags = merge(var.tags, {
    Module    = "lake_formation"
    Component = "query-layer-governance"
  })
}


resource "aws_lakeformation_lf_tag" "tenant" {
  key    = "tenant-${var.environment}"
  values = var.tenant_codes
}

resource "aws_lakeformation_lf_tag" "scope_unit" {
  key    = "scope_unit-${var.environment}"
  values = length(var.scope_unit_ids) > 0 ? var.scope_unit_ids : ["__tenant__"]
}

resource "aws_lakeformation_lf_tag" "department" {
  key    = "department-${var.environment}"
  values = ["finance", "operations", "sales_marketing", "shared"]
}


resource "aws_lakeformation_resource_lf_tags" "tenant_database" {
  for_each = toset(var.tenant_codes)

  database {
    name = var.glue_database_name
  }

  lf_tag {
    key   = aws_lakeformation_lf_tag.tenant.key
    value = each.value
  }

  depends_on = [aws_lakeformation_lf_tag.tenant]
}


resource "aws_lakeformation_permissions" "tenant_scoped_read" {
  for_each = var.tenant_scoped_principals

  principal   = each.value.principal_arn
  permissions = ["SELECT", "DESCRIBE"]

  lf_tag_policy {
    resource_type = "TABLE"

    expression {
      key    = aws_lakeformation_lf_tag.tenant.key
      values = [each.value.tenant_code]
    }

    dynamic "expression" {
      for_each = each.value.department == "" ? [] : [each.value.department]
      content {
        key    = aws_lakeformation_lf_tag.department.key
        values = [expression.value, "shared"]
      }
    }
  }

  depends_on = [
    aws_lakeformation_lf_tag.tenant,
    aws_lakeformation_lf_tag.scope_unit,
    aws_lakeformation_lf_tag.department,
  ]
}


resource "aws_lakeformation_data_cells_filter" "scope_unit_rows" {
  for_each = var.scope_unit_row_filters

  table_data {
    database_name    = var.glue_database_name
    name             = replace(each.key, ":", "_")
    table_catalog_id = each.value.catalog_id
    table_name       = each.value.table_name

    row_filter {
      filter_expression = "scope_unit_id = '${each.value.scope_unit_id}'"
    }

    column_wildcard {}
  }
}

resource "aws_lakeformation_permissions" "scope_unit_scoped_read" {
  for_each = var.scope_unit_grants

  principal   = each.value.principal_arn
  permissions = ["SELECT"]

  data_cells_filter {
    database_name    = var.glue_database_name
    table_catalog_id = each.value.catalog_id
    table_name       = each.value.table_name
    name             = replace(each.value.filter_key, ":", "_")
  }

  depends_on = [aws_lakeformation_data_cells_filter.scope_unit_rows]
}


resource "aws_lakeformation_data_lake_settings" "governance" {
  count = length(var.data_lake_admin_arns) == 0 ? 0 : 1

  admins = var.data_lake_admin_arns

  create_database_default_permissions {
    permissions = []
    principal   = "IAM_ALLOWED_PRINCIPALS"
  }

  create_table_default_permissions {
    permissions = []
    principal   = "IAM_ALLOWED_PRINCIPALS"
  }
}
