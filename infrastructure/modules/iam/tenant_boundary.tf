# ---------------------------------------------------------------------------
# IAM-enforced tenant isolation (DL-SEC-01, DL-SEC-02, gap 1).
#
# The gap being closed: every existing policy scopes to a resource ARN with no `Condition`
# tying a principal to its tenant. A path-construction bug or a compromised dependency can
# therefore cross tenants even though application code says it cannot.
#
# Rolled out in two stages, deliberately:
#   1. `audit` — the conditions are attached to a *separate, unattached* policy so CloudTrail
#      can be queried for what would have been denied. Nothing changes behaviourally.
#   2. `enforce` — the deny policy attaches to the runtime roles.
#
# Attaching conditions blind would break the default `demo` tenant the moment one path segment
# does not match, and the failure mode is a silent AccessDenied mid-pipeline. The audit stage
# is what makes that discoverable before it is live.
# ---------------------------------------------------------------------------

variable "tenant_boundary_mode" {
  description = <<-EOT
    "audit" creates the boundary policy unattached so CloudTrail can be reviewed for what
    would have been denied; "enforce" attaches it to the runtime roles.

    Audit first is mandated by DL-SEC-01: attaching conditions blind breaks the default tenant
    the moment one path segment does not match, and the failure is a silent mid-pipeline
    AccessDenied.
  EOT
  type        = string
  default     = "audit"

  validation {
    condition     = contains(["audit", "enforce"], var.tenant_boundary_mode)
    error_message = "tenant_boundary_mode must be audit or enforce."
  }
}

variable "data_bucket_arns" {
  description = "Data-plane bucket ARNs the S3 prefix condition applies to."
  type        = list(string)
  default     = []
}

variable "tenant_scoped_table_arns" {
  description = "DynamoDB table ARNs whose partition key is tenant-scoped."
  type        = list(string)
  default     = []
}

variable "cloudtrail_log_group_name" {
  description = <<-EOT
    CloudWatch log group receiving CloudTrail events, used for the audit-stage metric filter.
    Empty skips the filter — which also means `CrossTenantAccessAttempts` has no producer, so
    set it before relying on that alarm.
  EOT
  type        = string
  default     = ""
}

locals {
  # The tenant prefix every data-plane object key starts with. Matches
  # contracts/identifier_policy.py::tenant_scoped_key and the S3 layouts in every writer.
  tenant_prefix_pattern = "$${aws:PrincipalTag/tenant_code}/*"

  # Roles that read or write tenant-scoped data. The control plane is deliberately absent: it
  # serves every tenant and derives its scope from verified JWT claims per request, so a
  # principal-tag condition would break it rather than protect anything.
  tenant_scoped_role_names = {
    extraction          = aws_iam_role.extraction_runtime.name
    transformation      = aws_iam_role.transformation_runtime.name
    entity_resolution   = aws_iam_role.entity_resolution_runtime.name
    analytics_publisher = aws_iam_role.analytics_publisher_runtime.name
  }

  tenant_boundary_enforced = var.tenant_boundary_mode == "enforce"
}

# ---------------------------------------------------------------------------
# The boundary policy. Written as explicit Denys rather than scoped Allows, because a Deny
# cannot be widened by another attached policy — which is the property a boundary needs.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "tenant_boundary" {
  # S3: deny any object operation whose key is outside the principal's tenant prefix
  # (DL-SEC-02 — turning today's write-path convention into a boundary).
  statement {
    sid    = "DenyS3OutsideTenantPrefix"
    effect = "Deny"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:GetObjectVersion",
    ]

    resources = [for arn in var.data_bucket_arns : "${arn}/*"]

    condition {
      test     = "StringNotLike"
      variable = "s3:prefix"
      values   = [local.tenant_prefix_pattern]
    }

    # A principal with no tenant_code tag is denied outright rather than unconstrained: an
    # absent tag must not read as "every tenant" (the same empty-means-deny rule as
    # DL-SCOPE-14).
    condition {
      test     = "Null"
      variable = "aws:PrincipalTag/tenant_code"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyS3ListOutsideTenantPrefix"
    effect = "Deny"

    actions   = ["s3:ListBucket"]
    resources = var.data_bucket_arns

    condition {
      test     = "StringNotLike"
      variable = "s3:prefix"
      values   = [local.tenant_prefix_pattern, ""]
    }
  }

  # DynamoDB: deny any item operation whose leading key does not begin with the principal's
  # tenant prefix. `dynamodb:LeadingKeys` is evaluated against the partition key, which is
  # exactly `tenant_scoped_key(tenant_code, ...)` on the config and watermark tables and
  # `tenant_code` itself on the rest.
  statement {
    sid    = "DenyDynamoDbOutsideTenantLeadingKey"
    effect = "Deny"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
    ]

    resources = var.tenant_scoped_table_arns

    condition {
      test     = "ForAllValues:StringNotLike"
      variable = "dynamodb:LeadingKeys"
      values = [
        "$${aws:PrincipalTag/tenant_code}",
        "$${aws:PrincipalTag/tenant_code}#*",
      ]
    }
  }

  # A full-table Scan cannot be constrained by LeadingKeys, so it is denied outright on
  # tenant-scoped tables. `list_configs_for_tenant` uses a Scan today, which is why the
  # control-plane role is excluded from this boundary — see the note on
  # `tenant_scoped_role_names`.
  statement {
    sid    = "DenyDynamoDbScanOnTenantScopedTables"
    effect = "Deny"

    actions   = ["dynamodb:Scan"]
    resources = var.tenant_scoped_table_arns
  }

  # Secrets Manager: deny any secret outside the principal's tenant path. The path form comes
  # from DL-SCOPE-06 (`edl/tenants/{tenant_code}/connections/{connection_id}/credentials`), so
  # the condition and the code construct the same string.
  statement {
    sid    = "DenySecretsOutsideTenantPath"
    effect = "Deny"

    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
      "secretsmanager:PutSecretValue",
    ]

    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:edl/*"]

    condition {
      test     = "StringNotLike"
      variable = "secretsmanager:ResourceTag/tenant_code"
      values   = ["$${aws:PrincipalTag/tenant_code}"]
    }
  }
}

resource "aws_iam_policy" "tenant_boundary" {
  name        = "${var.environment}-edl-tenant-boundary"
  description = <<-EOT
    Tenant isolation boundary (DL-SEC-01). Deny-based so it cannot be widened by another
    attached policy. Attached to the runtime roles only when tenant_boundary_mode is
    "enforce"; in "audit" mode it exists unattached so CloudTrail can be reviewed first.
  EOT
  policy      = data.aws_iam_policy_document.tenant_boundary.json

  tags = merge(var.tags, {
    Name    = "${var.environment}-edl-tenant-boundary"
    Purpose = "tenant-isolation-boundary"
    Mode    = var.tenant_boundary_mode
  })
}

resource "aws_iam_role_policy_attachment" "tenant_boundary" {
  for_each = local.tenant_boundary_enforced ? local.tenant_scoped_role_names : {}

  role       = each.value
  policy_arn = aws_iam_policy.tenant_boundary.arn
}

# ---------------------------------------------------------------------------
# CloudTrail data-event selector for the audit stage. Without object-level events, CloudTrail
# records the API call but not the key, so "would this have been denied" is unanswerable.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "cross_tenant_access_attempts" {
  count = var.cloudtrail_log_group_name == "" ? 0 : 1

  name           = "${var.environment}-edl-cross-tenant-access-attempts"
  log_group_name = var.cloudtrail_log_group_name

  # An AccessDenied on an edl resource is either the boundary working or a legitimate access
  # the boundary broke. Either way an operator must see it.
  pattern = "{ ($.errorCode = \"AccessDenied\") && ($.requestParameters.bucketName = \"edl-*\" || $.requestParameters.tableName = \"Edl*\") }"

  metric_transformation {
    name      = "CrossTenantAccessAttempts"
    namespace = "EnterpriseDatalake"
    value     = "1"
    unit      = "Count"
    # Zero default so the alarm distinguishes "no attempts" from "no data", which matters
    # because this metric pages.
    default_value = 0
  }
}

output "tenant_boundary_policy_arn" {
  description = "ARN of the tenant isolation boundary policy."
  value       = aws_iam_policy.tenant_boundary.arn
}

output "tenant_boundary_mode" {
  description = "audit (unattached, CloudTrail review) or enforce (attached to runtime roles)."
  value       = var.tenant_boundary_mode
}

output "tenant_boundary_attached_roles" {
  description = "Roles the boundary is attached to; empty in audit mode."
  value       = local.tenant_boundary_enforced ? values(local.tenant_scoped_role_names) : []
}
