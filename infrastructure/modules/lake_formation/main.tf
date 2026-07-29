# ---------------------------------------------------------------------------
# Lake Formation LF-Tags replacing the Athena/Glue wildcard grant (DL-SERV-07, gap 5).
#
# The gap being closed: three configured principals hold a grant across *every* tenant's data.
# That is fine with one tenant and a latent breach with two, which is why DL-SERV-07 requires
# this before a second tenant's data lands in a shared environment.
#
# The mechanism is LF-Tags rather than per-table grants: a new tenant's tables inherit their
# grant from the tag applied at registration, so onboarding does not require a Terraform change
# to every principal's policy. That is the whole reason tags exist.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# The tag vocabulary. Two dimensions, deliberately:
#   `tenant`     — who owns the data. The isolation boundary.
#   `department` — which department may read it, mapping to the semantic layer's access tags
#                  so Athena and the semantic compiler enforce the same taxonomy (DL-SEC-10)
#                  rather than two vocabularies that drift.
# ---------------------------------------------------------------------------

resource "aws_lakeformation_lf_tag" "tenant" {
  key    = "tenant"
  values = var.tenant_codes
}

# DL-SCOPE-14 / ConsumptionSurface.ATHENA: an analyst queries Athena directly, so the only
# boundary available *below* tenant level is an LF-Tag — the Python scope predicate never runs on
# that path. Without this key, Athena access is isolated per tenant but every franchisee's rows
# are visible to any analyst granted the tenant tag.
#
# Values are the registered scope unit ids plus the implicit sentinel for single-partition
# tenants, so a table belonging to a non-franchise tenant is still taggable.
resource "aws_lakeformation_lf_tag" "scope_unit" {
  key    = "scope_unit"
  values = length(var.scope_unit_ids) > 0 ? var.scope_unit_ids : ["__tenant__"]
}

resource "aws_lakeformation_lf_tag" "department" {
  key = "department"
  # Matches semantic/enterprise_model.py's department tags, minus the `dept_` prefix.
  values = ["finance", "operations", "sales_marketing", "shared"]
}

# ---------------------------------------------------------------------------
# Tag assignment per tenant database. Registration-time tagging is what makes a new tenant's
# tables governed on creation rather than after someone remembers to grant them.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Grants. One tag-based grant per (principal, tenant) pair, replacing the wildcard.
#
# `SELECT` and `DESCRIBE` only: no principal in this grant set writes through Athena, and a
# read-only grant means a compromised BI credential cannot mutate curated data.
# ---------------------------------------------------------------------------

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
      # A principal scoped to a department gets both conditions; one scoped only to a tenant
      # gets the tenant condition alone, which is correct for a tenant-admin role.
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

# ---------------------------------------------------------------------------
# Data-lake administrators. Kept explicit and small: an admin bypasses every tag grant, so a
# long admin list silently undoes the isolation above.
# ---------------------------------------------------------------------------

resource "aws_lakeformation_data_lake_settings" "governance" {
  count = length(var.data_lake_admin_arns) == 0 ? 0 : 1

  admins = var.data_lake_admin_arns

  # Both default permission sets are emptied. Leaving IAMAllowedPrincipals in place is the
  # single most common reason LF-Tag grants appear to be ignored: it grants every IAM
  # principal full access to every new table, which is the wildcard grant under another name.
  create_database_default_permissions {
    permissions = []
    principal   = "IAM_ALLOWED_PRINCIPALS"
  }

  create_table_default_permissions {
    permissions = []
    principal   = "IAM_ALLOWED_PRINCIPALS"
  }
}
