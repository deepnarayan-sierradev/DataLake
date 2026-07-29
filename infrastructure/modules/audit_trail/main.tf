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

data "aws_partition" "current" {}

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

    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

# Who read the audit archive is itself audit evidence (CKV_AWS_18).
resource "aws_s3_bucket_logging" "trail" {
  count = var.access_log_bucket_id == null ? 0 : 1

  bucket        = aws_s3_bucket.trail.id
  target_bucket = var.access_log_bucket_id
  target_prefix = "audit-trail/"
}

# Object events to EventBridge rather than a hardwired consumer (CKV2_AWS_62).
resource "aws_s3_bucket_notification" "trail" {
  bucket      = aws_s3_bucket.trail.id
  eventbridge = true
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

# CloudTrail publishes here on every new log file, so a consumer can react to trail delivery
# instead of polling the bucket (CKV_AWS_252).
resource "aws_sns_topic" "trail_delivery" {
  name              = "${local.trail_name}-delivery"
  kms_master_key_id = var.kms_key_arn
  tags              = merge(var.tags, { Purpose = "cloudtrail-delivery-notifications" })
}

data "aws_iam_policy_document" "trail_delivery" {
  statement {
    sid     = "AWSCloudTrailSNSPolicy"
    effect  = "Allow"
    actions = ["SNS:Publish"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    resources = [aws_sns_topic.trail_delivery.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:cloudtrail:${var.region}:${var.account_id}:trail/${local.trail_name}"]
    }
  }
}

resource "aws_sns_topic_policy" "trail_delivery" {
  arn    = aws_sns_topic.trail_delivery.arn
  policy = data.aws_iam_policy_document.trail_delivery.json
}

resource "aws_cloudtrail" "platform" {
  name                          = local.trail_name
  s3_bucket_name                = aws_s3_bucket.trail.id
  sns_topic_name                = aws_sns_topic.trail_delivery.arn
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

# ---------------------------------------------------------------------------
# Cross-region replication of the audit archive (CKV_AWS_144). This bucket is the SOC 2
# evidence, so a single-region loss is the one that matters most — the events it holds cannot
# be regenerated. Versioning is enabled here because replication requires it.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "trail_replica" {
  provider = aws.replica

  bucket = "${aws_s3_bucket.trail.id}-replica"

  # The replication target does not replicate onward, and access logging and notifications
  # belong on the primary that receives the traffic.
  #checkov:skip=CKV_AWS_144:This is the replication target.
  #checkov:skip=CKV_AWS_18:Access logging is on the primary.
  #checkov:skip=CKV2_AWS_62:Event notifications are on the primary.

  tags = merge(var.tags, { Purpose = "cloudtrail-archive-replica" })
}

resource "aws_s3_bucket_versioning" "trail_replica" {
  provider = aws.replica

  bucket = aws_s3_bucket.trail_replica.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_kms_key" "trail_replica" {
  provider = aws.replica

  description             = "Encrypts the replicated audit archive (${var.environment})."
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.trail_replica_key.json
  tags                    = var.tags
}

data "aws_iam_policy_document" "trail_replica_key" {
  # A key policy's resource is the key itself; scoping is by principal.
  #checkov:skip=CKV_AWS_109:Key policy resource is the key itself.
  #checkov:skip=CKV_AWS_111:Key policy resource is the key itself; principals are enumerated.
  #checkov:skip=CKV_AWS_356:A key policy cannot name its own ARN as a resource.
  statement {
    sid       = "AccountRootAdministration"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${var.account_id}:root"]
    }
  }

  statement {
    sid       = "ReplicationRoleEncrypt"
    effect    = "Allow"
    actions   = ["kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.trail_replication.arn]
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "trail_replica" {
  provider = aws.replica

  bucket = aws_s3_bucket.trail_replica.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.trail_replica.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "trail_replica" {
  provider = aws.replica

  bucket                  = aws_s3_bucket.trail_replica.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "trail_replica" {
  provider = aws.replica

  bucket = aws_s3_bucket.trail_replica.id
  rule {
    id     = "retain-then-expire"
    status = "Enabled"
    filter {}
    expiration { days = var.retention_days }
    noncurrent_version_expiration { noncurrent_days = 90 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

data "aws_iam_policy_document" "trail_replication_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "trail_replication" {
  statement {
    sid       = "ReadSourceForReplication"
    effect    = "Allow"
    actions   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
    resources = [aws_s3_bucket.trail.arn]
  }

  statement {
    sid    = "ReadSourceObjects"
    effect = "Allow"
    actions = [
      "s3:GetObjectVersionForReplication",
      "s3:GetObjectVersionAcl",
      "s3:GetObjectVersionTagging",
    ]
    resources = ["${aws_s3_bucket.trail.arn}/*"]
  }

  statement {
    sid       = "WriteReplicaObjects"
    effect    = "Allow"
    actions   = ["s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags"]
    resources = ["${aws_s3_bucket.trail_replica.arn}/*"]
  }

  statement {
    sid       = "DecryptSourceObjects"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }

  statement {
    sid       = "EncryptReplicaObjects"
    effect    = "Allow"
    actions   = ["kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.trail_replica.arn]
  }
}

resource "aws_iam_role" "trail_replication" {
  name               = "${local.trail_name}-replication"
  assume_role_policy = data.aws_iam_policy_document.trail_replication_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "trail_replication" {
  name   = "replication"
  role   = aws_iam_role.trail_replication.id
  policy = data.aws_iam_policy_document.trail_replication.json
}

resource "aws_s3_bucket_replication_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  role   = aws_iam_role.trail_replication.arn

  rule {
    id     = "replicate-audit-archive"
    status = "Enabled"

    filter {}

    delete_marker_replication { status = "Enabled" }

    source_selection_criteria {
      sse_kms_encrypted_objects { status = "Enabled" }
    }

    destination {
      bucket        = aws_s3_bucket.trail_replica.arn
      storage_class = "STANDARD_IA"

      encryption_configuration {
        replica_kms_key_id = aws_kms_key.trail_replica.arn
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.trail, aws_s3_bucket_versioning.trail_replica]
}
