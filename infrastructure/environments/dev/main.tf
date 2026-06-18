locals {
  environment  = "dev"
  project_name = "edl"
  aws_region   = var.aws_region

  common_tags = {
    Project     = "enterprise-data-lake"
    Environment = local.environment
    ManagedBy   = "terraform"
    CostCenter  = var.cost_center
  }
}

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# KMS Keys — one per capability area
# ---------------------------------------------------------------------------

module "kms_storage" {
  source      = "../../modules/kms"
  environment = local.environment
  aws_region  = local.aws_region
  capability  = "storage"
  description = "KMS key for S3 data lake bucket encryption (dev)"
  key_user_role_arns      = [] # Populated after IAM module creates roles
  allow_cloudwatch_logs   = false
  deletion_window_in_days = 7 # Shorter window for dev
  tags                    = local.common_tags
}

module "kms_database" {
  source      = "../../modules/kms"
  environment = local.environment
  aws_region  = local.aws_region
  capability  = "database"
  description = "KMS key for DynamoDB and SQS encryption (dev)"
  deletion_window_in_days = 7
  tags                    = local.common_tags
}

module "kms_secrets" {
  source      = "../../modules/kms"
  environment = local.environment
  aws_region  = local.aws_region
  capability  = "secrets"
  description = "KMS key for Secrets Manager encryption (dev)"
  deletion_window_in_days = 7
  tags                    = local.common_tags
}

module "kms_logs" {
  source      = "../../modules/kms"
  environment = local.environment
  aws_region  = local.aws_region
  capability  = "logs"
  description = "KMS key for CloudWatch Logs and SNS encryption (dev)"
  allow_cloudwatch_logs   = true
  allow_sns               = true
  deletion_window_in_days = 7
  tags                    = local.common_tags
}

# ---------------------------------------------------------------------------
# Networking
# dev: single NAT gateway (cost-optimised), fewer VPC endpoints
# ---------------------------------------------------------------------------

module "networking" {
  source      = "../../modules/networking"
  environment = local.environment

  vpc_cidr             = "10.0.0.0/16"
  availability_zones   = ["${local.aws_region}a", "${local.aws_region}b"]
  private_subnet_cidrs = ["10.0.0.0/20", "10.0.16.0/20"]
  public_subnet_cidrs  = ["10.0.128.0/20", "10.0.144.0/20"]

  single_nat_gateway       = true # Cost-optimised for dev
  flow_log_retention_days  = 30
  flow_logs_kms_key_arn    = module.kms_logs.key_arn

  # Interface endpoints — all enabled for parity with staging/prod
  enable_secrets_manager_endpoint        = true
  enable_cloudwatch_logs_endpoint        = true
  enable_cloudwatch_monitoring_endpoint  = true
  enable_step_functions_endpoint         = true
  enable_glue_endpoint                   = false # Glue endpoint optional in dev
  enable_kms_endpoint                    = true

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

module "storage" {
  source      = "../../modules/storage"
  environment = local.environment
  project_name = local.project_name

  storage_kms_key_arn = module.kms_storage.key_arn
  extraction_runtime_role_arns = [
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.environment}-extraction-runtime-role",
  ]

  raw_object_lock_retention_days        = 30 # Shorter for dev
  raw_noncurrent_version_retention_days = 7
  access_logs_retention_days            = 30

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Metadata Persistence (DynamoDB watermarks, run audit log, DLQ)
# ---------------------------------------------------------------------------

module "metadata_persistence" {
  source      = "../../modules/metadata_persistence"
  environment = local.environment

  database_kms_key_arn      = module.kms_database.key_arn
  orchestration_role_arns   = [module.iam.orchestration_step_functions_role_arn]
  replay_operator_role_arns = var.replay_operator_role_arns

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

module "secrets" {
  source      = "../../modules/secrets"
  environment = local.environment

  secrets_kms_key_arn          = module.kms_secrets.key_arn
  extraction_runtime_role_arns = [module.iam.extraction_runtime_role_arn]
  secret_recovery_window_days  = 7 # Shorter for dev

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

module "iam" {
  source      = "../../modules/iam"
  environment = local.environment

  raw_layer_bucket_arn        = module.storage.raw_layer_bucket_arn
  curated_layer_bucket_arn    = module.storage.curated_layer_bucket_arn
  analytics_layer_bucket_arn  = module.storage.analytics_layer_bucket_arn
  schema_snapshots_bucket_arn = module.storage.schema_snapshots_bucket_arn
  watermark_table_arn         = module.metadata_persistence.watermark_repository_table_arn
  run_audit_log_table_arn     = module.metadata_persistence.run_audit_log_table_arn
  dlq_arn                     = module.metadata_persistence.extraction_failure_dlq_arn

  kms_key_arns_for_extraction = [
    module.kms_storage.key_arn,
    module.kms_secrets.key_arn,
    module.kms_database.key_arn,
  ]
  kms_key_arns_for_transformation = [
    module.kms_storage.key_arn,
    module.kms_database.key_arn,
  ]

  github_org                  = var.github_org
  github_repo                 = var.github_repo
  cicd_deployment_policy_arns = var.cicd_deployment_policy_arns

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

module "observability" {
  source      = "../../modules/observability"
  environment = local.environment

  logs_kms_key_arn          = module.kms_logs.key_arn
  log_retention_days        = 30
  alert_email               = var.alert_email
  watermark_lag_slo_seconds = 172800 # 48h SLO in dev (more relaxed)

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Lambda — Extraction Pipeline
# Packages the connector_runtime Python code as a Lambda function.
# The zip must be uploaded to S3 before the first terraform apply.
# See: make lambda-package && make lambda-upload
# ---------------------------------------------------------------------------

module "lambda_pipeline" {
  source      = "../../modules/lambda_pipeline"
  environment = local.environment

  kms_key_arn        = module.kms_logs.key_arn
  execution_role_arn = module.iam.extraction_runtime_role_arn

  lambda_package_s3_bucket   = var.lambda_package_s3_bucket
  lambda_package_s3_key      = var.lambda_package_s3_key
  lambda_package_source_hash = var.lambda_package_source_hash

  raw_s3_bucket_name             = module.storage.raw_layer_bucket_id
  schema_snapshot_s3_bucket_name = module.storage.schema_snapshots_bucket_id

  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = []

  cloudwatch_log_group_arn = module.observability.log_group_arns["connector-runtime"]
  log_retention_days       = 30
  memory_size_mb           = 1024
  timeout_seconds          = 900  # Max Lambda timeout; most entities complete in < 120s

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]
}

# ---------------------------------------------------------------------------
# Orchestration — Step Functions + EventBridge Scheduler
# ---------------------------------------------------------------------------

module "orchestration" {
  source      = "../../modules/orchestration"
  environment = local.environment

  kms_key_arn             = module.kms_logs.key_arn
  step_functions_role_arn = module.iam.orchestration_step_functions_role_arn
  state_machine_type      = "STANDARD"
  log_retention_days      = 30
  alert_topic_arn         = module.observability.platform_alerts_topic_arn
  enable_xray_tracing     = true

  extraction_pipeline_lambda_arn     = var.extraction_pipeline_lambda_arn
  transformation_pipeline_lambda_arn = var.transformation_pipeline_lambda_arn
  entity_resolution_lambda_arn       = var.entity_resolution_lambda_arn
  analytics_publisher_lambda_arn     = var.analytics_publisher_lambda_arn
  serving_store_loader_lambda_arn    = var.serving_store_loader_lambda_arn

  tags = local.common_tags

  depends_on = [module.iam, module.observability]
}

# ---------------------------------------------------------------------------
# Glue Data Catalog + Athena Workgroup
# ---------------------------------------------------------------------------

module "glue" {
  source      = "../../modules/glue"
  environment = local.environment

  curated_layer_bucket_id   = module.storage.curated_layer_bucket_id
  analytics_layer_bucket_id = module.storage.analytics_layer_bucket_id
  athena_results_bucket_id  = module.storage.analytics_layer_bucket_id # reuse analytics bucket for query results
  kms_key_arn               = module.kms_storage.key_arn

  tags = local.common_tags

  depends_on = [module.storage]
}
