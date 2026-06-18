terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
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

# ---------------------------------------------------------------------------
# Access logs bucket — receives access logs from all other buckets.
# This bucket does NOT log to itself (would cause recursion).
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "access_logs" {
  bucket = "${var.environment}-${var.project_name}-s3-access-logs"

  tags = merge(local.common_tags, {
    Name       = "${var.environment}-${var.project_name}-s3-access-logs"
    DataLayer  = "access-logs"
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
  }
}

resource "aws_s3_bucket_policy" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  policy = data.aws_iam_policy_document.enforce_tls.json
}

# ---------------------------------------------------------------------------
# Raw layer bucket — immutable source-aligned records
# Object Lock in GOVERNANCE mode: prevents overwrite/delete by default.
# Object Lock must be enabled at bucket creation time.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "raw_layer" {
  bucket              = "${var.environment}-${var.project_name}-raw-layer"
  object_lock_enabled = true # Must be set at creation time — cannot be added later

  tags = merge(local.common_tags, {
    Name      = "${var.environment}-${var.project_name}-raw-layer"
    DataLayer = "raw"
  })
}

resource "aws_s3_bucket_object_lock_configuration" "raw_layer" {
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
  }
}

resource "aws_s3_bucket_policy" "raw_layer" {
  bucket = aws_s3_bucket.raw_layer.id
  policy = data.aws_iam_policy_document.raw_layer_policy.json
}

# ---------------------------------------------------------------------------
# Curated layer bucket
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "curated_layer" {
  bucket = "${var.environment}-${var.project_name}-curated-layer"
  tags = merge(local.common_tags, {
    Name      = "${var.environment}-${var.project_name}-curated-layer"
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
  }
}

resource "aws_s3_bucket_policy" "curated_layer" {
  bucket = aws_s3_bucket.curated_layer.id
  policy = data.aws_iam_policy_document.enforce_tls.json
}

# ---------------------------------------------------------------------------
# Analytics layer bucket
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "analytics_layer" {
  bucket = "${var.environment}-${var.project_name}-analytics-layer"
  tags = merge(local.common_tags, {
    Name      = "${var.environment}-${var.project_name}-analytics-layer"
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
  policy = data.aws_iam_policy_document.enforce_tls.json
}

# ---------------------------------------------------------------------------
# Schema snapshots bucket
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "schema_snapshots" {
  bucket = "${var.environment}-${var.project_name}-schema-snapshots"
  tags = merge(local.common_tags, {
    Name      = "${var.environment}-${var.project_name}-schema-snapshots"
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
  policy = data.aws_iam_policy_document.enforce_tls.json
}

# ---------------------------------------------------------------------------
# Shared IAM policy documents
# ---------------------------------------------------------------------------

# Enforce TLS: deny any request that does not use HTTPS
data "aws_iam_policy_document" "enforce_tls" {
  statement {
    sid     = "DenyNonTLSRequests"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = ["*"] # Applied per-bucket via individual bucket policies
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
    sid     = "DenyOutdatedTLS"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = ["*"]
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

# Raw layer policy: enforce TLS + restrict PutObject to extraction runtime role only
data "aws_iam_policy_document" "raw_layer_policy" {
  source_policy_documents = [data.aws_iam_policy_document.enforce_tls.json]

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
      values   = concat(
        var.extraction_runtime_role_arns,
        ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"],
      )
    }
  }
}
