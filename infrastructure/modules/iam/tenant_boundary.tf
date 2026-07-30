
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
  tenant_prefix_pattern = "$${aws:PrincipalTag/tenant_code}/*"

  tenant_scoped_role_names = {
    extraction          = aws_iam_role.extraction_runtime.name
    transformation      = aws_iam_role.transformation_runtime.name
    entity_resolution   = aws_iam_role.entity_resolution_runtime.name
    analytics_publisher = aws_iam_role.analytics_publisher_runtime.name
  }

  tenant_scoped_role_arns = {
    extraction          = aws_iam_role.extraction_runtime.arn
    transformation      = aws_iam_role.transformation_runtime.arn
    entity_resolution   = aws_iam_role.entity_resolution_runtime.arn
    analytics_publisher = aws_iam_role.analytics_publisher_runtime.arn
  }

  tenant_boundary_enforced = var.tenant_boundary_mode == "enforce" && var.tenant_session_tagging_adopted
}


data "aws_iam_policy_document" "tenant_boundary" {
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

  statement {
    sid    = "DenyDynamoDbScanOnTenantScopedTables"
    effect = "Deny"

    actions   = ["dynamodb:Scan"]
    resources = var.tenant_scoped_table_arns
  }

  statement {
    sid    = "DenySecretsOutsideTenantPath"
    effect = "Deny"

    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
      "secretsmanager:PutSecretValue",
    ]

    resources = [
      "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:${var.name_prefix}/${var.environment}/*",
    ]

    condition {
      test     = "StringNotLike"
      variable = "secretsmanager:ResourceTag/tenant_code"
      values   = ["$${aws:PrincipalTag/tenant_code}"]
    }
  }
}

resource "aws_iam_policy" "tenant_boundary" {
  name        = "${var.name_prefix}-tenant-boundary-${var.environment}"
  description = <<-EOT
    Tenant isolation boundary (DL-SEC-01). Deny-based so it cannot be widened by another
    attached policy. Attached to the runtime roles only when tenant_boundary_mode is
    "enforce"; in "audit" mode it exists unattached so CloudTrail can be reviewed first.
  EOT
  policy      = data.aws_iam_policy_document.tenant_boundary.json

  tags = merge(var.tags, {
    Name    = "${var.name_prefix}-tenant-boundary-${var.environment}"
    Purpose = "tenant-isolation-boundary"
    Mode    = var.tenant_boundary_mode
  })
}

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


resource "aws_cloudwatch_log_metric_filter" "cross_tenant_access_attempts" {
  count = var.cloudtrail_log_group_name == "" ? 0 : 1

  name           = "${var.name_prefix}-cross-tenant-access-attempts-${var.environment}"
  log_group_name = var.cloudtrail_log_group_name

  pattern = "{ ($.errorCode = \"AccessDenied\") && ($.requestParameters.bucketName = \"${var.name_prefix}-*\" || $.requestParameters.tableName = \"${var.name_prefix}-*\") }"

  metric_transformation {
    name          = "IamBoundaryAccessDenied"
    namespace     = "EnterpriseDatalake"
    value         = "1"
    unit          = "Count"
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

    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/tenant_code"
      values   = ["*"]
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "sts:TransitiveTagKeys"
      values   = ["tenant_code"]
    }
  }
}

resource "aws_iam_role" "tenant_data" {
  for_each = local.tenant_scoped_role_arns

  name               = "${var.name_prefix}-tenant-data-${replace(each.key, "_", "-")}-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.tenant_data_assume_role[each.key].json
  description        = "Tenant-tagged data role for the ${each.key} stage; the boundary attaches here."
  tags               = merge(var.tags, { Purpose = "tenant-tagged-data-access", Stage = each.key })
}

resource "aws_iam_role_policy_attachment" "tenant_data_boundary" {
  for_each = aws_iam_role.tenant_data

  role       = each.value.name
  policy_arn = aws_iam_policy.tenant_boundary.arn
}

output "tenant_data_role_arns" {
  description = "Stage -> tenant-tagged data role ARN, for each stage Lambda's TENANT_DATA_ROLE_ARN."
  value       = { for stage, role in aws_iam_role.tenant_data : stage => role.arn }
}
