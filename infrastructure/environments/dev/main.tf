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
  source                  = "../../modules/kms"
  environment             = local.environment
  aws_region              = local.aws_region
  capability              = "storage"
  description             = "KMS key for S3 data lake bucket encryption (dev)"
  key_user_role_arns      = [] # Populated after IAM module creates roles
  allow_cloudwatch_logs   = false
  deletion_window_in_days = 7 # Shorter window for dev
  tags                    = local.common_tags
}

module "kms_database" {
  source                  = "../../modules/kms"
  environment             = local.environment
  aws_region              = local.aws_region
  capability              = "database"
  description             = "KMS key for DynamoDB and SQS encryption (dev)"
  deletion_window_in_days = 7
  tags                    = local.common_tags
}

module "kms_secrets" {
  source                  = "../../modules/kms"
  environment             = local.environment
  aws_region              = local.aws_region
  capability              = "secrets"
  description             = "KMS key for Secrets Manager encryption (dev)"
  deletion_window_in_days = 7
  tags                    = local.common_tags
}

module "kms_logs" {
  source                  = "../../modules/kms"
  environment             = local.environment
  aws_region              = local.aws_region
  capability              = "logs"
  description             = "KMS key for CloudWatch Logs and SNS encryption (dev)"
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

  single_nat_gateway      = true # Cost-optimised for dev
  flow_log_retention_days = 30
  flow_logs_kms_key_arn   = module.kms_logs.key_arn

  # Interface endpoints — all enabled for parity with staging/prod
  enable_secrets_manager_endpoint       = true
  enable_cloudwatch_logs_endpoint       = true
  enable_cloudwatch_monitoring_endpoint = true
  enable_step_functions_endpoint        = true
  enable_glue_endpoint                  = false # Glue endpoint optional in dev
  enable_kms_endpoint                   = true

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

module "storage" {
  source       = "../../modules/storage"
  environment  = local.environment
  project_name = local.project_name

  storage_kms_key_arn = module.kms_storage.key_arn
  extraction_runtime_role_arns = [
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/EdlExtractionRuntimeRole",
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
# Serving Store — MySQL RDS
# Master credential is AWS-managed (no password in Terraform state).
# ---------------------------------------------------------------------------

module "serving_store_database" {
  source      = "../../modules/serving_store_database"
  environment = local.environment
  engine      = "mysql"

  vpc_id     = module.networking.vpc_id
  subnet_ids = module.networking.private_subnet_ids

  storage_kms_key_arn = module.kms_database.key_arn
  secrets_kms_key_arn = module.kms_secrets.key_arn

  instance_class               = "db.t3.micro" # dev-sized; revisit before staging/prod
  multi_az                     = false
  deletion_protection          = false # dev only — staging/prod should set true
  backup_retention_period_days = 1

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

module "secrets" {
  source      = "../../modules/secrets"
  environment = local.environment

  secrets_kms_key_arn          = module.kms_secrets.key_arn
  logs_kms_key_arn             = module.kms_logs.key_arn
  extraction_runtime_role_arns = [module.iam.extraction_runtime_role_arn]
  secret_recovery_window_days  = 7 # Shorter for dev

  # SEC-6: credential expiry notifier Lambda + daily EventBridge schedule.
  credential_expiry_notifier_role_arn  = module.iam.credential_expiry_notifier_role_arn
  credential_expiry_scheduler_role_arn = module.iam.credential_expiry_scheduler_role_arn
  alert_topic_arn                      = module.observability.platform_alerts_topic_arn
  lambda_package_s3_bucket             = var.lambda_package_s3_bucket
  lambda_package_s3_key                = var.lambda_package_s3_key
  lambda_package_source_hash           = var.lambda_package_source_hash

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

module "iam" {
  source      = "../../modules/iam"
  environment = local.environment

  raw_layer_bucket_arn           = module.storage.raw_layer_bucket_arn
  curated_layer_bucket_arn       = module.storage.curated_layer_bucket_arn
  analytics_layer_bucket_arn     = module.storage.analytics_layer_bucket_arn
  schema_snapshots_bucket_arn    = module.storage.schema_snapshots_bucket_arn
  watermark_table_arn            = module.metadata_persistence.watermark_repository_table_arn
  run_audit_log_table_arn        = module.metadata_persistence.run_audit_log_table_arn
  entity_config_table_arn        = module.metadata_persistence.entity_extraction_config_table_arn
  entity_type_registry_table_arn = module.metadata_persistence.entity_type_registry_table_arn
  dlq_arn                        = module.metadata_persistence.extraction_failure_dlq_arn

  serving_store_config_table_arn = module.metadata_persistence.serving_store_config_table_arn
  twin_index_table_arn           = module.metadata_persistence.twin_index_table_arn
  semantic_model_table_arn       = module.metadata_persistence.semantic_model_table_arn
  saved_query_table_arn          = module.metadata_persistence.saved_query_table_arn
  serving_store_secret_arns = [
    module.serving_store_database.master_user_secret_arn,
    "arn:aws:secretsmanager:${local.aws_region}:${data.aws_caller_identity.current.account_id}:secret:edl/serving-store/*",
  ]

  kms_key_arns_for_extraction = [
    module.kms_storage.key_arn,
    module.kms_secrets.key_arn,
    module.kms_database.key_arn,
  ]
  kms_key_arns_for_transformation = [
    module.kms_storage.key_arn,
    module.kms_database.key_arn,
  ]
  kms_key_arns_for_credential_expiry_notifier = [module.kms_logs.key_arn]
  kms_key_arns_for_serving_store              = [module.kms_secrets.key_arn]

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

  # DLQ depth alarm
  extraction_failure_dlq_name = module.metadata_persistence.extraction_failure_dlq_name

  # Lambda alarms — names resolved from Lambda module outputs
  extraction_lambda_name          = "EdlExtractionPipeline"
  transformation_lambda_name      = "EdlTransformationPipeline"
  entity_resolution_lambda_name   = "EdlEntityResolutionPipeline"
  analytics_publisher_lambda_name = "EdlAnalyticsLayerPublisher"

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

  entity_config_table_name = module.metadata_persistence.entity_extraction_config_table_name
  watermark_table_name     = module.metadata_persistence.watermark_repository_table_name
  audit_log_table_name     = module.metadata_persistence.run_audit_log_table_name

  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = []

  cloudwatch_log_group_arn = module.observability.log_group_arns["connector-runtime"]
  log_retention_days       = 30
  memory_size_mb           = 1024
  timeout_seconds          = 900 # Max Lambda timeout; most entities complete in < 120s
  # Reserved concurrency cap: prevents this function from consuming the full account pool.
  # Improvement plan §1.6 (burst-buffer reserved concurrency table) specifies 400, but the
  # dev account's Lambda concurrency quota is the AWS default (1000) and AWS requires at
  # least 100 unreserved — the full §1.6 table (400+300+200+100+50=1050) doesn't fit in dev.
  # Halved proportionally across all 5 reserved-concurrency Lambdas to fit comfortably;
  # request a quota increase (or apply the full §1.6 values) if dev throughput needs it.
  reserved_concurrent_executions = 200

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]
}

# ---------------------------------------------------------------------------
# Lambda — Transformation Pipeline
# Reuses the same Lambda zip as the extraction pipeline (both packaged into
# extraction-pipeline.zip). Different handler entry point.
# See: make lambda-package && make lambda-upload
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
  field_mapping_s3_bucket_name = module.storage.curated_layer_bucket_id # field mappings live under curated/field-mappings/

  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = []

  cloudwatch_log_group_arn = module.observability.log_group_arns["transformation"]
  log_retention_days       = 30
  memory_size_mb           = 2048 # Increased for DuckDB in-process merge (§3.1)
  timeout_seconds          = 900
  # Reserved concurrency: 300 per improvement plan §1.6, halved for dev's account quota
  # (see lambda_pipeline module block above for the full explanation).
  reserved_concurrent_executions = 150

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]
}

# ---------------------------------------------------------------------------
# Orchestration — Step Functions + EventBridge Scheduler
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Lambda — Entity Resolution Pipeline
# Reuses the same Lambda zip as the extraction and transformation pipelines.
# Different handler entry point: entity_resolution_pipeline_handler.
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
  log_retention_days       = 30
  memory_size_mb           = 1024
  timeout_seconds          = 900
  # Reserved concurrency: 200 per improvement plan §1.6, halved for dev's account quota
  # (see lambda_pipeline module block above for the full explanation).
  reserved_concurrent_executions = 100

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]
}

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
  log_retention_days       = 30
  memory_size_mb           = 512
  timeout_seconds          = 300
  # Reserved concurrency: 100 per improvement plan §1.6, halved for dev's account quota
  # (see lambda_pipeline module block above for the full explanation).
  reserved_concurrent_executions = 50

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking, module.glue]
}

module "serving_store_lambda" {
  source      = "../../modules/serving_store_lambda"
  environment = local.environment

  kms_key_arn        = module.kms_logs.key_arn
  execution_role_arn = module.iam.serving_store_loader_runtime_role_arn

  lambda_package_s3_bucket   = var.lambda_package_s3_bucket
  lambda_package_s3_key      = var.lambda_package_s3_key
  lambda_package_source_hash = var.lambda_package_source_hash

  analytics_s3_bucket_name  = module.storage.analytics_layer_bucket_id
  governance_s3_bucket_name = ""

  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = []

  cloudwatch_log_group_arn       = module.observability.log_group_arns["serving-store-loader"]
  log_retention_days             = 30
  memory_size_mb                 = 512
  timeout_seconds                = 300
  reserved_concurrent_executions = 10 # dev-sized; onboarded tenants are few at first

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]
}

# Cross-module SG wiring for the serving store — kept out of both modules to
# avoid a circular module dependency (each needs the other's SG id).
resource "aws_security_group_rule" "serving_store_lambda_to_database" {
  type                     = "egress"
  from_port                = 3306
  to_port                  = 3306
  protocol                 = "tcp"
  security_group_id        = module.serving_store_lambda.lambda_security_group_id
  source_security_group_id = module.serving_store_database.security_group_id
  description              = "MySQL egress from the serving store loader Lambda to its RDS instance."
}

resource "aws_security_group_rule" "serving_store_database_from_lambda" {
  type                     = "ingress"
  from_port                = 3306
  to_port                  = 3306
  protocol                 = "tcp"
  security_group_id        = module.serving_store_database.security_group_id
  source_security_group_id = module.serving_store_lambda.lambda_security_group_id
  description              = "MySQL ingress to the serving store RDS instance from the loader Lambda only."
}

module "twin_build_lambda" {
  source      = "../../modules/twin_build_lambda"
  environment = local.environment

  kms_key_arn        = module.kms_logs.key_arn
  execution_role_arn = module.iam.twin_build_runtime_role_arn

  lambda_package_s3_bucket   = var.lambda_package_s3_bucket
  lambda_package_s3_key      = var.lambda_package_s3_key
  lambda_package_source_hash = var.lambda_package_source_hash

  analytics_s3_bucket_name          = module.storage.analytics_layer_bucket_id
  relationship_rules_s3_bucket_name = module.storage.curated_layer_bucket_id

  reserved_concurrent_executions = 10

  tags = local.common_tags

  depends_on = [module.iam, module.storage]
}

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
  transformation_pipeline_lambda_arn = module.transformation_lambda.lambda_function_arn
  entity_resolution_lambda_arn       = module.entity_resolution_lambda.lambda_function_arn
  analytics_publisher_lambda_arn     = module.analytics_publisher_lambda.lambda_function_arn
  serving_store_loader_lambda_arn    = module.serving_store_lambda.lambda_function_arn
  twin_build_lambda_arn              = module.twin_build_lambda.lambda_function_arn

  # SQS burst buffer — pipeline trigger Lambda package (same zip as extraction pipeline)
  lambda_package_s3_bucket   = var.lambda_package_s3_bucket
  lambda_package_s3_key      = var.lambda_package_s3_key
  lambda_package_source_hash = var.lambda_package_source_hash

  pipeline_trigger_role_arn = module.iam.pipeline_trigger_role_arn
  # Halved for dev's account quota (see lambda_pipeline module block above).
  pipeline_trigger_reserved_concurrency = 25

  # DLQ processor Lambda
  dlq_processor_role_arn     = module.iam.dlq_processor_role_arn
  extraction_failure_dlq_arn = module.metadata_persistence.extraction_failure_dlq_arn
  run_audit_log_table_name   = module.metadata_persistence.run_audit_log_table_name

  tags = local.common_tags

  depends_on = [module.iam, module.observability]
}

# ---------------------------------------------------------------------------
# Control Plane — multi-tenant SaaS API (tenant provisioning, entity
# registration, pipeline triggering, run status). Reuses the same pipeline
# trigger SQS FIFO queue as orchestration's pipeline_trigger Lambda, and the
# same Lambda deployment package as the rest of connector_runtime.
# ---------------------------------------------------------------------------

module "control_plane" {
  source      = "../../modules/control_plane"
  environment = local.environment

  kms_key_arn         = module.kms_logs.key_arn
  log_retention_days  = 30
  enable_xray_tracing = true

  lambda_package_s3_bucket   = var.lambda_package_s3_bucket
  lambda_package_s3_key      = var.lambda_package_s3_key
  lambda_package_source_hash = var.lambda_package_source_hash

  control_plane_role_arn = module.iam.control_plane_role_arn

  pipeline_trigger_queue_url      = module.orchestration.pipeline_trigger_queue_url
  entity_config_table_name        = module.metadata_persistence.entity_extraction_config_table_name
  entity_type_registry_table_name = module.metadata_persistence.entity_type_registry_table_name
  run_audit_log_table_name        = module.metadata_persistence.run_audit_log_table_name
  analytics_s3_bucket_name        = module.storage.analytics_layer_bucket_id
  twin_index_table_name           = module.metadata_persistence.twin_index_table_name
  semantic_model_table_name       = module.metadata_persistence.semantic_model_table_name
  saved_query_table_name          = module.metadata_persistence.saved_query_table_name

  tags = local.common_tags

  depends_on = [module.iam, module.orchestration, module.metadata_persistence]
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

  analytics_reader_principals = var.analytics_reader_principals

  tags = local.common_tags

  depends_on = [module.storage]
}
