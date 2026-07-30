terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = "~> 5.0"
      configuration_aliases = [aws.replica]
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  common_tags = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "storage"
  })
}


resource "aws_s3_bucket" "access_logs" {
  bucket = "${var.name_prefix}-access-logs-${var.environment}-${var.region_short}"

  tags = merge(local.common_tags, {
    Name      = "${var.name_prefix}-access-logs-${var.environment}-${var.region_short}"
    DataLayer = "access-logs"
  })
}

resource "aws_s3_bucket_versioning" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.storage_kms_key_arn
    }
    bucket_key_enabled = true # Reduces KMS API calls and cost
  }
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket                  = aws_s3_bucket.access_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  rule {
    id     = "expire-access-logs"
    status = "Enabled"
    filter { prefix = "" }
    expiration { days = var.access_logs_retention_days }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_s3_bucket_policy" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  policy = data.aws_iam_policy_document.enforce_tls["access_logs"].json
}


resource "aws_s3_bucket" "raw_layer" {
  bucket              = "${var.name_prefix}-raw-${var.environment}-${var.region_short}"
  object_lock_enabled = true # Must be set at creation time — cannot be added later

  tags = merge(local.common_tags, {
    Name      = "${var.name_prefix}-raw-${var.environment}-${var.region_short}"
    DataLayer = "raw"
  })

  lifecycle {
    ignore_changes = [object_lock_enabled]
  }
}

resource "aws_s3_bucket_object_lock_configuration" "raw_layer" {
  count  = aws_s3_bucket.raw_layer.object_lock_enabled ? 1 : 0
  bucket = aws_s3_bucket.raw_layer.id
  rule {
    default_retention {
      mode = "GOVERNANCE" # Allows authorised override; use COMPLIANCE for stricter immutability
      days = var.raw_object_lock_retention_days
    }
  }
}

resource "aws_s3_bucket_versioning" "raw_layer" {
  bucket = aws_s3_bucket.raw_layer.id
  versioning_configuration { status = "Enabled" } # Required for Object Lock
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw_layer" {
  bucket = aws_s3_bucket.raw_layer.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.storage_kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "raw_layer" {
  bucket                  = aws_s3_bucket.raw_layer.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "raw_layer" {
  bucket        = aws_s3_bucket.raw_layer.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "raw-layer/"
}

resource "aws_s3_bucket_lifecycle_configuration" "raw_layer" {
  bucket = aws_s3_bucket.raw_layer.id
  rule {
    id     = "transition-to-ia"
    status = "Enabled"
    filter { prefix = "" }
    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 365
      storage_class = "GLACIER"
    }
    noncurrent_version_expiration { noncurrent_days = var.raw_noncurrent_version_retention_days }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_s3_bucket_policy" "raw_layer" {
  bucket = aws_s3_bucket.raw_layer.id
  policy = data.aws_iam_policy_document.raw_layer_policy.json
}


resource "aws_s3_bucket" "curated_layer" {
  bucket = "${var.name_prefix}-curated-${var.environment}-${var.region_short}"
  tags = merge(local.common_tags, {
    Name      = "${var.name_prefix}-curated-${var.environment}-${var.region_short}"
    DataLayer = "curated"
  })
}

resource "aws_s3_bucket_versioning" "curated_layer" {
  bucket = aws_s3_bucket.curated_layer.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "curated_layer" {
  bucket = aws_s3_bucket.curated_layer.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.storage_kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "curated_layer" {
  bucket                  = aws_s3_bucket.curated_layer.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "curated_layer" {
  bucket        = aws_s3_bucket.curated_layer.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "curated-layer/"
}

resource "aws_s3_bucket_lifecycle_configuration" "curated_layer" {
  bucket = aws_s3_bucket.curated_layer.id
  rule {
    id     = "transition-curated"
    status = "Enabled"
    filter {
      prefix = ""
    }
    transition {
      days          = 180
      storage_class = "STANDARD_IA"
    }
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_s3_bucket_policy" "curated_layer" {
  bucket = aws_s3_bucket.curated_layer.id
  policy = data.aws_iam_policy_document.enforce_tls["curated_layer"].json
}


resource "aws_s3_bucket" "analytics_layer" {
  bucket = "${var.name_prefix}-analytics-${var.environment}-${var.region_short}"
  tags = merge(local.common_tags, {
    Name      = "${var.name_prefix}-analytics-${var.environment}-${var.region_short}"
    DataLayer = "analytics"
  })
}

resource "aws_s3_bucket_versioning" "analytics_layer" {
  bucket = aws_s3_bucket.analytics_layer.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "analytics_layer" {
  bucket = aws_s3_bucket.analytics_layer.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.storage_kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "analytics_layer" {
  bucket                  = aws_s3_bucket.analytics_layer.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "analytics_layer" {
  bucket        = aws_s3_bucket.analytics_layer.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "analytics-layer/"
}

resource "aws_s3_bucket_policy" "analytics_layer" {
  bucket = aws_s3_bucket.analytics_layer.id
  policy = data.aws_iam_policy_document.enforce_tls["analytics_layer"].json
}


resource "aws_s3_bucket" "schema_snapshots" {
  bucket = "${var.name_prefix}-schema-snapshots-${var.environment}-${var.region_short}"
  tags = merge(local.common_tags, {
    Name      = "${var.name_prefix}-schema-snapshots-${var.environment}-${var.region_short}"
    DataLayer = "schema-metadata"
  })
}

resource "aws_s3_bucket_versioning" "schema_snapshots" {
  bucket = aws_s3_bucket.schema_snapshots.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "schema_snapshots" {
  bucket = aws_s3_bucket.schema_snapshots.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.storage_kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "schema_snapshots" {
  bucket                  = aws_s3_bucket.schema_snapshots.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "schema_snapshots" {
  bucket        = aws_s3_bucket.schema_snapshots.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "schema-snapshots/"
}

resource "aws_s3_bucket_policy" "schema_snapshots" {
  bucket = aws_s3_bucket.schema_snapshots.id
  policy = data.aws_iam_policy_document.enforce_tls["schema_snapshots"].json
}


resource "aws_s3_bucket_lifecycle_configuration" "analytics_layer" {
  bucket = aws_s3_bucket.analytics_layer.id
  rule {
    id     = "transition-analytics"
    status = "Enabled"
    filter { prefix = "" }
    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
    noncurrent_version_expiration { noncurrent_days = 90 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "schema_snapshots" {
  bucket = aws_s3_bucket.schema_snapshots.id
  rule {
    id     = "expire-superseded-snapshots"
    status = "Enabled"
    filter { prefix = "" }
    noncurrent_version_expiration { noncurrent_days = 365 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}


resource "aws_s3_bucket_notification" "buckets" {
  for_each = {
    access_logs      = aws_s3_bucket.access_logs.id
    curated_layer    = aws_s3_bucket.curated_layer.id
    analytics_layer  = aws_s3_bucket.analytics_layer.id
    schema_snapshots = aws_s3_bucket.schema_snapshots.id
    raw_layer        = aws_s3_bucket.raw_layer.id
  }

  bucket      = each.value
  eventbridge = true
}


locals {
  _tls_buckets = {
    access_logs      = aws_s3_bucket.access_logs.arn
    curated_layer    = aws_s3_bucket.curated_layer.arn
    analytics_layer  = aws_s3_bucket.analytics_layer.arn
    schema_snapshots = aws_s3_bucket.schema_snapshots.arn
    raw_layer        = aws_s3_bucket.raw_layer.arn
  }
}

data "aws_iam_policy_document" "enforce_tls" {
  for_each = local._tls_buckets

  statement {
    sid       = "DenyNonTLSRequests"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [each.value, "${each.value}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "DenyOutdatedTLS"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [each.value, "${each.value}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }
}

data "aws_iam_policy_document" "raw_layer_policy" {
  source_policy_documents = [data.aws_iam_policy_document.enforce_tls["raw_layer"].json]

  statement {
    sid     = "RestrictRawWriteToExtractionRuntime"
    effect  = "Deny"
    actions = ["s3:PutObject", "s3:DeleteObject"]
    resources = [
      "${aws_s3_bucket.raw_layer.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
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


locals {
  replica_purpose = {
    raw_layer        = "raw"
    curated_layer    = "curated"
    analytics_layer  = "analytics"
    schema_snapshots = "schema-snapshots"
    access_logs      = "access-logs"
  }

  replicated_buckets = {
    raw_layer        = aws_s3_bucket.raw_layer.id
    curated_layer    = aws_s3_bucket.curated_layer.id
    analytics_layer  = aws_s3_bucket.analytics_layer.id
    schema_snapshots = aws_s3_bucket.schema_snapshots.id
    access_logs      = aws_s3_bucket.access_logs.id
  }
  replicated_bucket_arns = {
    raw_layer        = aws_s3_bucket.raw_layer.arn
    curated_layer    = aws_s3_bucket.curated_layer.arn
    analytics_layer  = aws_s3_bucket.analytics_layer.arn
    schema_snapshots = aws_s3_bucket.schema_snapshots.arn
    access_logs      = aws_s3_bucket.access_logs.arn
  }
}

resource "aws_s3_bucket" "replica" {
  provider = aws.replica
  for_each = local.replicated_buckets

  #checkov:skip=CKV_AWS_144:This is the replication target; it does not replicate onward.
  #checkov:skip=CKV_AWS_18:Access logging is on the primary; the replica takes no direct traffic.
  #checkov:skip=CKV2_AWS_62:Event notifications are on the primary.

  bucket = "${var.name_prefix}-${local.replica_purpose[each.key]}-${var.environment}-${var.replica_region_short}"

  tags = merge(local.common_tags, {
    Name    = "${var.name_prefix}-${local.replica_purpose[each.key]}-${var.environment}-${var.replica_region_short}"
    Purpose = "cross-region-replica"
  })
}

resource "aws_s3_bucket_versioning" "replica" {
  provider = aws.replica
  for_each = local.replicated_buckets

  bucket = aws_s3_bucket.replica[each.key].id
  versioning_configuration { status = "Enabled" }
}

data "aws_iam_policy_document" "replica_key" {
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
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid    = "ReplicationRoleEncrypt"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.replication.arn]
    }
  }
}

resource "aws_kms_key" "replica" {
  provider = aws.replica

  description             = "Encrypts the cross-region S3 replicas (${var.environment})."
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.replica_key.json
  tags                    = local.common_tags
}

resource "aws_kms_alias" "replica" {
  provider = aws.replica

  name          = "alias/${var.name_prefix}-storage-replica-${var.environment}"
  target_key_id = aws_kms_key.replica.key_id
}

resource "aws_s3_bucket_server_side_encryption_configuration" "replica" {
  provider = aws.replica
  for_each = local.replicated_buckets

  bucket = aws_s3_bucket.replica[each.key].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.replica.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "replica" {
  provider = aws.replica
  for_each = local.replicated_buckets

  bucket                  = aws_s3_bucket.replica[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "replica" {
  provider = aws.replica
  for_each = local.replicated_buckets

  bucket = aws_s3_bucket.replica[each.key].id
  rule {
    id     = "expire-noncurrent-and-abort-multipart"
    status = "Enabled"
    filter { prefix = "" }
    noncurrent_version_expiration { noncurrent_days = 90 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

data "aws_iam_policy_document" "replication_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "replication" {
  statement {
    sid    = "ReadSourceForReplication"
    effect = "Allow"
    actions = [
      "s3:GetReplicationConfiguration",
      "s3:ListBucket",
    ]
    resources = values(local.replicated_bucket_arns)
  }

  statement {
    sid    = "ReadSourceObjects"
    effect = "Allow"
    actions = [
      "s3:GetObjectVersionForReplication",
      "s3:GetObjectVersionAcl",
      "s3:GetObjectVersionTagging",
    ]
    resources = [for arn in values(local.replicated_bucket_arns) : "${arn}/*"]
  }

  statement {
    sid    = "WriteReplicaObjects"
    effect = "Allow"
    actions = [
      "s3:ReplicateObject",
      "s3:ReplicateDelete",
      "s3:ReplicateTags",
    ]
    resources = [for b in aws_s3_bucket.replica : "${b.arn}/*"]
  }

  statement {
    sid       = "DecryptSourceObjects"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [var.storage_kms_key_arn]
  }

  statement {
    sid       = "EncryptReplicaObjects"
    effect    = "Allow"
    actions   = ["kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.replica.arn]
  }
}

resource "aws_iam_role" "replication" {
  name               = "${var.name_prefix}-s3-replication-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.replication_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy" "replication" {
  name   = "replication"
  role   = aws_iam_role.replication.id
  policy = data.aws_iam_policy_document.replication.json
}

resource "aws_s3_bucket_replication_configuration" "buckets" {
  for_each = local.replicated_buckets

  bucket = each.value
  role   = aws_iam_role.replication.arn

  rule {
    id     = "replicate-all"
    status = "Enabled"

    filter {}

    delete_marker_replication { status = "Enabled" }

    destination {
      bucket        = aws_s3_bucket.replica[each.key].arn
      storage_class = "STANDARD_IA"

      encryption_configuration {
        replica_kms_key_id = aws_kms_key.replica.arn
      }
    }

    source_selection_criteria {
      sse_kms_encrypted_objects { status = "Enabled" }
    }
  }

  depends_on = [
    aws_s3_bucket_versioning.raw_layer,
    aws_s3_bucket_versioning.curated_layer,
    aws_s3_bucket_versioning.analytics_layer,
    aws_s3_bucket_versioning.schema_snapshots,
    aws_s3_bucket_versioning.access_logs,
    aws_s3_bucket_versioning.replica,
  ]
}
