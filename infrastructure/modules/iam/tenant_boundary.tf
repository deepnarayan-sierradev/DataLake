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

variable "tenant_session_tagging_adopted" {
  description = <<-EOT
    Whether every call site that touches tenant data builds its client from a tenant-tagged session
    (`tenancy/tenant_session.py`). `enforce` is refused unless this is true, and that refusal is the
    point.

    The boundary conditions on `aws:PrincipalTag/tenant_code`. Attaching it to a shared Lambda
    execution role — which is what the four runtime roles are — cannot work: a role tag holds one
    value and each role serves every tenant. Enforcing in that state does not half-protect, it
    breaks asymmetrically. The S3 statements are guarded by `Null ... = false`, so for an untagged
    principal they never apply and S3 stays open; the DynamoDB and Secrets Manager statements
    compare against an unresolvable variable and a missing resource tag, so they deny everything.
    The pipeline would stop while the data it was protecting stayed reachable.

    So this is not a feature flag, it is an interlock: the unsafe combination is unreachable.
  EOT
  type        = bool
  default     = false
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

  # The same four stages by ARN, for the data roles' trust policies.
  tenant_scoped_role_arns = {
    extraction          = aws_iam_role.extraction_runtime.arn
    transformation      = aws_iam_role.transformation_runtime.arn
    entity_resolution   = aws_iam_role.entity_resolution_runtime.arn
    analytics_publisher = aws_iam_role.analytics_publisher_runtime.arn
  }

  # Both conditions, deliberately. See `tenant_session_tagging_adopted` for why enforcing without
  # the tagged-session path is worse than not enforcing at all.
  tenant_boundary_enforced = var.tenant_boundary_mode == "enforce" && var.tenant_session_tagging_adopted
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

# The boundary is only meaningful over real resources. Every environment left
# `data_bucket_arns` and `tenant_scoped_table_arns` unset, so the S3 and DynamoDB statements had an
# empty `resources` list — a statement with no Resource, which IAM rejects outright. The policy
# therefore could not have been applied successfully in any environment, while `terraform validate`
# stayed green because validate does not call IAM. This check makes that a plan-time failure.
resource "terraform_data" "tenant_boundary_covers_resources" {
  lifecycle {
    precondition {
      condition     = length(var.data_bucket_arns) > 0
      error_message = "tenant_boundary: data_bucket_arns is empty, so the S3 Deny statements would carry no Resource and IAM would reject the policy. Pass the data-plane bucket ARNs."
    }
    precondition {
      condition     = length(var.tenant_scoped_table_arns) > 0
      error_message = "tenant_boundary: tenant_scoped_table_arns is empty, so the DynamoDB Deny statement would carry no Resource. Pass the tenant-scoped table ARNs."
    }
    precondition {
      condition     = !local.tenant_boundary_enforced || var.cloudtrail_log_group_name != ""
      error_message = "tenant_boundary: enforce mode requires cloudtrail_log_group_name, or CrossTenantAccessAttempts has no producer and the observation window that gates the flip measures nothing."
    }
  }
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
    # Deliberately NOT `CrossTenantAccessAttempts`. That name is already emitted by the control
    # plane's own claim check, so one metric carried two unrelated events — and the go/no-go gate for
    # `enforce` ("sustained zero") was therefore satisfiable by an API surface nobody was calling,
    # while saying nothing about IAM. A separate name makes the observation window mean what the
    # runbook claims it means.
    name      = "IamBoundaryAccessDenied"
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

# ---------------------------------------------------------------------------
# Per-stage tenant data roles (DL-SEC-01).
#
# The boundary attaches here, not to the stage execution roles, because these are the only
# principals that can carry `tenant_code`: a stage role assumes one of these per invocation with
# `Tags=[{tenant_code}]` (see `tenancy/tenant_session.py`), and `sts:TagSession` in the trust policy
# is what makes the tag authoritative — a caller cannot assert a tag the trust policy forbids.
#
# One data role per stage rather than one shared role, so each stays as narrow as the stage role it
# serves. A single shared data role would widen every stage to the union of all of them.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "tenant_data_assume_role" {
  for_each = local.tenant_scoped_role_arns

  statement {
    sid     = "StageRoleMayAssumeWithTenantTag"
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "AWS"
      identifiers = [each.value]
    }

    # The tag must be present. Without this a caller could assume the role with no tag at all, which
    # is the untagged state the boundary cannot constrain — the same "absent means everything"
    # failure mode the scope predicate refuses.
    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/tenant_code"
      values   = ["*"]
    }

    # Only this tag may be set, and it is transitive so a further hop cannot drop it.
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "sts:TransitiveTagKeys"
      values   = ["tenant_code"]
    }
  }
}

resource "aws_iam_role" "tenant_data" {
  for_each = local.tenant_scoped_role_arns

  name               = "EdlTenantData${replace(title(replace(each.key, "_", " ")), " ", "")}Role"
  assume_role_policy = data.aws_iam_policy_document.tenant_data_assume_role[each.key].json
  description        = "Tenant-tagged data role for the ${each.key} stage; the boundary attaches here."
  tags               = merge(var.tags, { Purpose = "tenant-tagged-data-access", Stage = each.key })
}

# Attached unconditionally: these roles exist *for* the boundary, so one without it would be a
# strictly wider principal than the stage role that assumes it.
resource "aws_iam_role_policy_attachment" "tenant_data_boundary" {
  for_each = aws_iam_role.tenant_data

  role       = each.value.name
  policy_arn = aws_iam_policy.tenant_boundary.arn
}

output "tenant_data_role_arns" {
  description = "Stage -> tenant-tagged data role ARN, for each stage Lambda's TENANT_DATA_ROLE_ARN."
  value       = { for stage, role in aws_iam_role.tenant_data : stage => role.arn }
}
