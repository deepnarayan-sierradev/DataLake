locals {
  environment  = "staging"
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

# Look up pre-existing operational DynamoDB tables.
# These tables are created outside Terraform with the correct key schema
# for the Python connector runtime (entity_id hash key only).
# They must exist before terraform apply is run in a new environment.
data "aws_dynamodb_table" "entity_config" {
  name = "${local.environment}-entity-extraction-config"
}

data "aws_dynamodb_table" "watermark" {
  name = "${local.environment}-watermark-repository"
}

data "aws_dynamodb_table" "audit_log" {
  name = "${local.environment}-run-audit-log"
}

module "kms_storage" {
  source                  = "../../modules/kms"
  environment             = local.environment
  aws_region              = local.aws_region
  capability              = "storage"
  description             = "KMS key for S3 data lake bucket encryption (staging)"
  deletion_window_in_days = 14
  tags                    = local.common_tags
}

module "kms_database" {
  source                  = "../../modules/kms"
  environment             = local.environment
  aws_region              = local.aws_region
  capability              = "database"
  description             = "KMS key for DynamoDB and SQS encryption (staging)"
  deletion_window_in_days = 14
  tags                    = local.common_tags
}

module "kms_secrets" {
  source                  = "../../modules/kms"
  environment             = local.environment
  aws_region              = local.aws_region
  capability              = "secrets"
  description             = "KMS key for Secrets Manager encryption (staging)"
  deletion_window_in_days = 14
  tags                    = local.common_tags
}

module "kms_logs" {
  source                  = "../../modules/kms"
  environment             = local.environment
  aws_region              = local.aws_region
  capability              = "logs"
  description             = "KMS key for CloudWatch Logs and SNS encryption (staging)"
  allow_cloudwatch_logs   = true
  allow_sns               = true
  deletion_window_in_days = 14
  tags                    = local.common_tags
}

module "networking" {
  source      = "../../modules/networking"
  environment = local.environment

  vpc_cidr             = "10.1.0.0/16"
  availability_zones   = ["${local.aws_region}a", "${local.aws_region}b", "${local.aws_region}c"]
  private_subnet_cidrs = ["10.1.0.0/20", "10.1.16.0/20", "10.1.32.0/20"]
  public_subnet_cidrs  = ["10.1.128.0/20", "10.1.144.0/20", "10.1.160.0/20"]

  single_nat_gateway                     = false # HA: one NAT per AZ
  flow_log_retention_days                = 90
  flow_logs_kms_key_arn                  = module.kms_logs.key_arn
  enable_secrets_manager_endpoint        = true
  enable_cloudwatch_logs_endpoint        = true
  enable_cloudwatch_monitoring_endpoint  = true
  enable_step_functions_endpoint         = true
  enable_glue_endpoint                   = true
  enable_kms_endpoint                    = true

  tags = local.common_tags
}

module "storage" {
  source                                = "../../modules/storage"
  environment                           = local.environment
  project_name                          = local.project_name
  storage_kms_key_arn                   = module.kms_storage.key_arn
  extraction_runtime_role_arns          = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.environment}-extraction-runtime-role"]
  raw_object_lock_retention_days        = 180
  raw_noncurrent_version_retention_days = 14
  access_logs_retention_days            = 90
  tags                                  = local.common_tags
}

module "metadata_persistence" {
  source                    = "../../modules/metadata_persistence"
  environment               = local.environment
  database_kms_key_arn      = module.kms_database.key_arn
  orchestration_role_arns   = [module.iam.orchestration_step_functions_role_arn]
  replay_operator_role_arns = var.replay_operator_role_arns
  tags                      = local.common_tags
}

module "secrets" {
  source                       = "../../modules/secrets"
  environment                  = local.environment
  secrets_kms_key_arn          = module.kms_secrets.key_arn
  extraction_runtime_role_arns = [module.iam.extraction_runtime_role_arn]
  secret_recovery_window_days  = 14
  tags                         = local.common_tags
}

module "iam" {
  source                          = "../../modules/iam"
  environment                     = local.environment
  raw_layer_bucket_arn            = module.storage.raw_layer_bucket_arn
  curated_layer_bucket_arn        = module.storage.curated_layer_bucket_arn
  analytics_layer_bucket_arn      = module.storage.analytics_layer_bucket_arn
  schema_snapshots_bucket_arn     = module.storage.schema_snapshots_bucket_arn
  watermark_table_arn             = data.aws_dynamodb_table.watermark.arn
  run_audit_log_table_arn         = data.aws_dynamodb_table.audit_log.arn
  entity_config_table_arn         = data.aws_dynamodb_table.entity_config.arn
  dlq_arn                         = module.metadata_persistence.extraction_failure_dlq_arn
  kms_key_arns_for_extraction     = [module.kms_storage.key_arn, module.kms_secrets.key_arn, module.kms_database.key_arn]
  kms_key_arns_for_transformation = [module.kms_storage.key_arn, module.kms_database.key_arn]
  github_org                      = var.github_org
  github_repo                     = var.github_repo
  cicd_deployment_policy_arns     = var.cicd_deployment_policy_arns
  tags                            = local.common_tags
}

module "observability" {
  source                    = "../../modules/observability"
  environment               = local.environment
  logs_kms_key_arn          = module.kms_logs.key_arn
  log_retention_days        = 90
  alert_email               = var.alert_email
  watermark_lag_slo_seconds = 86400 # 24h SLO for staging
  tags                      = local.common_tags
}

module "glue" {
  source      = "../../modules/glue"
  environment = local.environment

  curated_layer_bucket_id   = module.storage.curated_layer_bucket_id
  analytics_layer_bucket_id = module.storage.analytics_layer_bucket_id
  athena_results_bucket_id  = module.storage.analytics_layer_bucket_id
  kms_key_arn               = module.kms_storage.key_arn

  tags = local.common_tags

  depends_on = [module.storage]
}

# ---------------------------------------------------------------------------
# Lambda — Extraction Pipeline
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

  entity_config_table_name = data.aws_dynamodb_table.entity_config.name
  watermark_table_name     = data.aws_dynamodb_table.watermark.name
  audit_log_table_name     = data.aws_dynamodb_table.audit_log.name

  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = []

  cloudwatch_log_group_arn = module.observability.log_group_arns["connector-runtime"]
  log_retention_days       = 90
  memory_size_mb           = 1024
  timeout_seconds          = 900

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]
}

# ---------------------------------------------------------------------------
# Lambda — Transformation Pipeline
# ---------------------------------------------------------------------------

module "transformation_lambda" {
  source      = "../../modules/transformation_lambda"
  environment = local.environment

  kms_key_arn        = module.kms_logs.key_arn
  execution_role_arn = module.iam.transformation_runtime_role_arn

  lambda_package_s3_bucket   = var.lambda_package_s3_bucket
  lambda_package_s3_key      = var.lambda_package_s3_key
  lambda_package_source_hash = var.lambda_package_source_hash

  raw_s3_bucket_name           = module.storage.raw_layer_bucket_id
  curated_s3_bucket_name       = module.storage.curated_layer_bucket_id
  field_mapping_s3_bucket_name = module.storage.curated_layer_bucket_id

  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = []

  cloudwatch_log_group_arn = module.observability.log_group_arns["transformation"]
  log_retention_days       = 90
  memory_size_mb           = 1024
  timeout_seconds          = 900

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]
}

# ---------------------------------------------------------------------------
# Lambda — Entity Resolution Pipeline
# ---------------------------------------------------------------------------

module "entity_resolution_lambda" {
  source      = "../../modules/entity_resolution_lambda"
  environment = local.environment

  kms_key_arn        = module.kms_logs.key_arn
  execution_role_arn = module.iam.entity_resolution_runtime_role_arn

  lambda_package_s3_bucket   = var.lambda_package_s3_bucket
  lambda_package_s3_key      = var.lambda_package_s3_key
  lambda_package_source_hash = var.lambda_package_source_hash

  curated_s3_bucket_name   = module.storage.curated_layer_bucket_id
  analytics_s3_bucket_name = module.storage.analytics_layer_bucket_id

  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = []

  cloudwatch_log_group_arn = module.observability.log_group_arns["entity-resolution"]
  log_retention_days       = 90
  memory_size_mb           = 1024
  timeout_seconds          = 900

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]
}

# ---------------------------------------------------------------------------
# Lambda — Analytics Publisher
# ---------------------------------------------------------------------------

module "analytics_publisher_lambda" {
  source      = "../../modules/analytics_publisher_lambda"
  environment = local.environment

  kms_key_arn        = module.kms_logs.key_arn
  execution_role_arn = module.iam.analytics_publisher_runtime_role_arn

  lambda_package_s3_bucket   = var.lambda_package_s3_bucket
  lambda_package_s3_key      = var.lambda_package_s3_key
  lambda_package_source_hash = var.lambda_package_source_hash

  analytics_s3_bucket_name = module.storage.analytics_layer_bucket_id
  glue_catalog_database    = module.glue.analytics_database_name

  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = []

  cloudwatch_log_group_arn = module.observability.log_group_arns["analytics-publisher"]
  log_retention_days       = 90
  memory_size_mb           = 512
  timeout_seconds          = 300

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking, module.glue]
}

# ---------------------------------------------------------------------------
# Orchestration module — full chained pipeline state machine
#
# State machine type: STANDARD for staging (execution history preserved for
# 90 days, supports executions > 5 minutes, exactly-once semantics).
#
# Five Lambda stages chained in sequence with explicit branching on
# transformation_blocked and is_publication_blocked:
#   extraction → transformation → entity_resolution → analytics → serving_store
# ---------------------------------------------------------------------------
module "orchestration" {
  source = "../../modules/orchestration"

  environment             = local.environment
  kms_key_arn             = module.kms_logs.key_arn
  step_functions_role_arn = module.iam.orchestration_step_functions_role_arn
  state_machine_type      = "STANDARD"
  log_retention_days      = 90
  alert_topic_arn         = module.observability.platform_alerts_topic_arn
  enable_xray_tracing     = true

  extraction_pipeline_lambda_arn     = var.extraction_pipeline_lambda_arn
  transformation_pipeline_lambda_arn = module.transformation_lambda.lambda_function_arn
  entity_resolution_lambda_arn       = module.entity_resolution_lambda.lambda_function_arn
  analytics_publisher_lambda_arn     = module.analytics_publisher_lambda.lambda_function_arn
  serving_store_loader_lambda_arn    = var.serving_store_loader_lambda_arn

  tags = local.common_tags

  depends_on = [module.iam, module.observability]
}
