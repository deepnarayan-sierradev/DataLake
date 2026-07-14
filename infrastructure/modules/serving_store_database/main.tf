terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

locals {
  common_tags = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "serving_store_database"
  })
  # One identifier per (engine, environment) — deliberately not just environment,
  # so a second engine can be added to the same environment later without a
  # naming collision (Phase 2: generalized, but no second instance created yet).
  identifier = "edl-serving-store-${var.engine}-${var.environment}"

  # Per-engine RDS defaults. Only the identifiers/ports/license models that
  # genuinely differ per engine live here — everything else (storage, backup,
  # multi_az, deletion_protection) is caller-supplied and engine-agnostic.
  engine_defaults = {
    mysql = {
      port           = 3306
      license_model  = "general-public-license"
      engine_version = "8.0"
    }
    postgres = {
      port           = 5432
      license_model  = "postgresql-license"
      engine_version = "16.4"
    }
    "sqlserver-se" = {
      port           = 1433
      license_model  = "license-included" # the only RDS-supported model for SQL Server
      engine_version = "15.00.4322.2.v1"
    }
  }
  selected = local.engine_defaults[var.engine]
}

# ---------------------------------------------------------------------------
# DB Subnet Group — private subnets only, no public accessibility.
# ---------------------------------------------------------------------------

resource "aws_db_subnet_group" "serving_store" {
  name       = "${local.identifier}-subnet-group"
  subnet_ids = var.subnet_ids
  tags       = merge(local.common_tags, { Name = "${local.identifier}-subnet-group" })
}

# ---------------------------------------------------------------------------
# Security Group — no rules defined here. Ingress (3306 from the serving
# store loader Lambda's SG) is wired by the caller as a standalone
# aws_security_group_rule, once both this module's and the Lambda module's
# SGs exist — defining it on either side inside the module would create a
# circular module dependency (each needs the other's SG id to create its
# own rule). No egress needed; RDS does not initiate outbound connections.
# ---------------------------------------------------------------------------

resource "aws_security_group" "serving_store_database" {
  name        = "${local.identifier}-sg"
  description = "Serving store RDS (${var.engine}). Ingress wired by the caller from the loader Lambda's SG."
  vpc_id      = var.vpc_id

  tags = merge(local.common_tags, { Name = "${local.identifier}-sg" })

  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------------------
# RDS Instance — Serving Store
#
# Not publicly accessible (private subnets only). Master credential is
# AWS-managed (manage_master_user_password) — no password ever appears in
# Terraform state or configuration; it is created directly in Secrets
# Manager and rotated by AWS. The serving_store_loader Lambda reads it via
# the ARN exposed in this module's outputs (OWASP A02, A07).
#
# Tenant-level isolation (one database/schema per tenant, one read-only
# reader credential per tenant) is enforced by the matching engine adapter
# under serving_store/loaders/ at the application layer, not by this
# instance-level resource.
# ---------------------------------------------------------------------------

resource "aws_db_instance" "serving_store" {
  identifier = local.identifier

  engine         = var.engine
  engine_version = coalesce(var.engine_version, local.selected.engine_version)
  license_model  = local.selected.license_model
  port           = local.selected.port
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = var.storage_kms_key_arn

  db_subnet_group_name   = aws_db_subnet_group.serving_store.name
  vpc_security_group_ids = [aws_security_group.serving_store_database.id]
  publicly_accessible    = false

  manage_master_user_password   = true
  master_user_secret_kms_key_id = var.secrets_kms_key_arn
  username                      = "edl_serving_admin"

  multi_az                  = var.multi_az
  backup_retention_period   = var.backup_retention_period_days
  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = var.environment != "prod"
  final_snapshot_identifier = var.environment == "prod" ? "${local.identifier}-final" : null

  apply_immediately = var.environment != "prod"

  tags = merge(local.common_tags, { Name = local.identifier })
}
