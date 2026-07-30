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
  identifier = "${var.name_prefix}-serving-store-${var.engine}-${var.environment}"

  engine_defaults = {
    mysql = {
      port           = 3306
      license_model  = "general-public-license"
      engine_version = "8.0"
      log_exports    = ["error", "general", "slowquery"]
    }
    postgres = {
      port           = 5432
      license_model  = "postgresql-license"
      engine_version = "16.4"
      log_exports    = ["postgresql", "upgrade"]
    }
    "sqlserver-se" = {
      port           = 1433
      license_model  = "license-included" # the only RDS-supported model for SQL Server
      engine_version = "15.00.4322.2.v1"
      log_exports    = ["agent", "error"]
    }
  }
  selected = local.engine_defaults[var.engine]
}

data "aws_partition" "current" {}


resource "aws_db_subnet_group" "serving_store" {
  name       = "${local.identifier}-subnet-group"
  subnet_ids = var.subnet_ids
  tags       = merge(local.common_tags, { Name = "${local.identifier}-subnet-group" })
}


resource "aws_security_group" "serving_store_database" {
  name        = "${local.identifier}-sg"
  description = "Serving store RDS (${var.engine}). Ingress wired by the caller from the loader Lambda SG."
  vpc_id      = var.vpc_id

  tags = merge(local.common_tags, { Name = "${local.identifier}-sg" })

  lifecycle {
    create_before_destroy = true
  }
}


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
  username                      = "datalake_serving_admin"

  multi_az                  = var.multi_az
  backup_retention_period   = var.backup_retention_period_days
  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = var.environment != "prod"
  final_snapshot_identifier = var.environment == "prod" ? "${local.identifier}-final" : null

  auto_minor_version_upgrade = true

  copy_tags_to_snapshot = true

  enabled_cloudwatch_logs_exports = local.selected.log_exports

  monitoring_interval = 60
  monitoring_role_arn = aws_iam_role.enhanced_monitoring.arn

  performance_insights_enabled    = var.performance_insights_enabled
  performance_insights_kms_key_id = var.performance_insights_enabled ? var.storage_kms_key_arn : null

  apply_immediately = var.environment != "prod"

  tags = merge(local.common_tags, { Name = local.identifier })
}


data "aws_iam_policy_document" "enhanced_monitoring_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "enhanced_monitoring" {
  name               = "${local.identifier}-enhanced-monitoring"
  assume_role_policy = data.aws_iam_policy_document.enhanced_monitoring_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "enhanced_monitoring" {
  role       = aws_iam_role.enhanced_monitoring.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}
