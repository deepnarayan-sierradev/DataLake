# ---------------------------------------------------------------------------
# CloudTrail (SOW §23.4 security monitoring and logging, DL-SEC-13, DL-SEC-17).
#
# There was no `aws_cloudtrail` resource anywhere in this repository, which had two consequences
# beyond the obvious compliance one:
#
#   1. The IAM tenant boundary ships in `audit` mode whose entire purpose is to answer "what would
#      have been denied". That answer comes from a CloudTrail metric filter, and the filter was
#      created only when `cloudtrail_log_group_name` was non-empty — which no environment set,
#      because there was no trail to name. So the observation window that gates the flip to
#      `enforce` measured nothing, and `CrossTenantAccessAttempts` sitting at zero proved only that
#      nothing was producing it.
#   2. `CrossTenantAccessAttempts` *did* have a producer — the control plane's own claim check — so
#      the alarm/emitter reconciliation was satisfied and the metric read as wired. Two different
#      events sharing one metric name is how a sustained zero came to look like evidence.
#
# The IAM-denial metric is therefore deliberately named separately (see the metric filter in
# `iam/tenant_boundary.tf`), and this module exists so it has something to read.
#
# Object-level data events are enabled for the data-plane buckets specifically: without them
# CloudTrail records that `GetObject` happened but not on which key, and a tenant-prefix boundary is
# a statement about keys. They are the expensive part of a trail, which is why the selector is
# scoped to these buckets rather than left at "all S3".
# ---------------------------------------------------------------------------

locals {
  trail_name = "${var.environment}-edl-audit-trail"
}

resource "aws_s3_bucket" "trail" {
  bucket        = "edl-audit-trail-${var.environment}-${var.account_id}"
  force_destroy = false

  tags = merge(var.tags, {
    Name    = "edl-audit-trail-${var.environment}"
    Purpose = "cloudtrail-log-archive"
  })
}

resource "aws_s3_bucket_public_access_block" "trail" {
  bucket                  = aws_s3_bucket.trail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "trail" {
  bucket = aws_s3_bucket.trail.id
  versioning_configuration {
    status = "Enabled"
  }
}

# An audit trail that can be silently truncated is not an audit trail. Object Lock is deliberately
# not used (it cannot be enabled on an existing bucket and complicates lifecycle), but the retention
# floor is enforced and deletion is blocked by the bucket policy below.
resource "aws_s3_bucket_lifecycle_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id

  rule {
    id     = "retain-then-expire"
    status = "Enabled"

    filter {}

    transition {
      days          = 90
      storage_class = "GLACIER"
    }

    expiration {
      days = var.retention_days
    }
  }
}

data "aws_iam_policy_document" "trail_bucket" {
  statement {
    sid    = "AWSCloudTrailAclCheck"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.trail.arn]
  }

  statement {
    sid    = "AWSCloudTrailWrite"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.trail.arn}/AWSLogs/${var.account_id}/*"]

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }

  # Nobody deletes audit evidence, including this account's own administrators. The lifecycle rule
  # above is the only expiry path, and it is visible in code.
  statement {
    sid    = "DenyAuditEvidenceDeletion"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:DeleteBucket"]
    resources = [aws_s3_bucket.trail.arn, "${aws_s3_bucket.trail.arn}/*"]
  }
}

resource "aws_s3_bucket_policy" "trail" {
  bucket = aws_s3_bucket.trail.id
  policy = data.aws_iam_policy_document.trail_bucket.json
}

resource "aws_cloudwatch_log_group" "trail" {
  name              = "/aws/cloudtrail/${local.trail_name}"
  retention_in_days = var.log_group_retention_days
  kms_key_id        = var.logs_kms_key_arn

  tags = merge(var.tags, { Purpose = "cloudtrail-events" })
}

data "aws_iam_policy_document" "trail_to_logs_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "trail_to_logs" {
  name               = "${var.environment}-edl-cloudtrail-to-logs"
  assume_role_policy = data.aws_iam_policy_document.trail_to_logs_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "trail_to_logs" {
  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.trail.arn}:*"]
  }
}

resource "aws_iam_role_policy" "trail_to_logs" {
  name   = "${var.environment}-edl-cloudtrail-to-logs"
  role   = aws_iam_role.trail_to_logs.id
  policy = data.aws_iam_policy_document.trail_to_logs.json
}

resource "aws_cloudtrail" "platform" {
  name                          = local.trail_name
  s3_bucket_name                = aws_s3_bucket.trail.id
  kms_key_id                    = var.kms_key_arn
  include_global_service_events = true
  is_multi_region_trail         = true
  enable_log_file_validation    = true

  cloud_watch_logs_group_arn = "${aws_cloudwatch_log_group.trail.arn}:*"
  cloud_watch_logs_role_arn  = aws_iam_role.trail_to_logs.arn

  # Object-level events on the data-plane buckets only. A tenant-prefix boundary is a claim about
  # keys, and management events do not carry the key.
  dynamic "event_selector" {
    for_each = length(var.data_bucket_arns) > 0 ? [1] : []

    content {
      read_write_type           = "All"
      include_management_events = true

      data_resource {
        type   = "AWS::S3::Object"
        values = [for arn in var.data_bucket_arns : "${arn}/"]
      }
    }
  }

  depends_on = [aws_s3_bucket_policy.trail, aws_iam_role_policy.trail_to_logs]

  tags = merge(var.tags, { Purpose = "platform-audit-trail" })
}
