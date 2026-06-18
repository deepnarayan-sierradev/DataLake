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
  watermark_table_arn             = module.metadata_persistence.watermark_repository_table_arn
  run_audit_log_table_arn         = module.metadata_persistence.run_audit_log_table_arn
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
  transformation_pipeline_lambda_arn = var.transformation_pipeline_lambda_arn
  entity_resolution_lambda_arn       = var.entity_resolution_lambda_arn
  analytics_publisher_lambda_arn     = var.analytics_publisher_lambda_arn
  serving_store_loader_lambda_arn    = var.serving_store_loader_lambda_arn

  tags = local.common_tags

  depends_on = [module.iam, module.observability]
}
