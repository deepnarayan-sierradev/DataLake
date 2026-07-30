locals {
  environment = "prod"
  name_prefix = "datalake"
  aws_region  = var.aws_region

  region_short_by_region = {
    "us-east-1"      = "use1"
    "us-east-2"      = "use2"
    "us-west-1"      = "usw1"
    "us-west-2"      = "usw2"
    "eu-west-1"      = "euw1"
    "eu-west-2"      = "euw2"
    "eu-central-1"   = "euc1"
    "ap-south-1"     = "aps1"
    "ap-southeast-1" = "apse1"
    "ap-southeast-2" = "apse2"
    "ca-central-1"   = "cac1"
  }
  region_short = lookup(
    local.region_short_by_region,
    data.aws_region.current.name,
    replace(data.aws_region.current.name, "-", ""),
  )
  replica_region_short = lookup(
    local.region_short_by_region,
    var.replica_region,
    replace(var.replica_region, "-", ""),
  )

  common_tags = {
    Application = "datalake"
    Project     = "datalake"
    Environment = local.environment
    ManagedBy   = "terraform"
    CostCenter  = var.cost_center
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}


module "code_signing" {
  source      = "../../modules/code_signing"
  environment = local.environment
  name_prefix = local.name_prefix
  tags        = local.common_tags
}

module "kms_storage" {
  source                  = "../../modules/kms"
  environment             = local.environment
  name_prefix             = local.name_prefix
  aws_region              = local.aws_region
  capability              = "storage"
  description             = "KMS key for S3 data lake bucket encryption (prod)"
  deletion_window_in_days = 30 # Maximum window for prod
  tags                    = local.common_tags
}

module "kms_database" {
  source                  = "../../modules/kms"
  environment             = local.environment
  name_prefix             = local.name_prefix
  aws_region              = local.aws_region
  capability              = "database"
  description             = "KMS key for DynamoDB and SQS encryption (prod)"
  deletion_window_in_days = 30
  tags                    = local.common_tags
}

module "kms_secrets" {
  source                  = "../../modules/kms"
  environment             = local.environment
  name_prefix             = local.name_prefix
  aws_region              = local.aws_region
  capability              = "secrets"
  description             = "KMS key for Secrets Manager encryption (prod)"
  deletion_window_in_days = 30
  tags                    = local.common_tags
}

module "kms_logs" {
  source                  = "../../modules/kms"
  environment             = local.environment
  name_prefix             = local.name_prefix
  aws_region              = local.aws_region
  capability              = "logs"
  description             = "KMS key for CloudWatch Logs and SNS encryption (prod)"
  allow_cloudwatch_logs   = true
  allow_sns               = true
  deletion_window_in_days = 30
  tags                    = local.common_tags
}

module "networking" {
  source      = "../../modules/networking"
  environment = local.environment
  name_prefix = local.name_prefix

  vpc_cidr             = "10.2.0.0/16"
  availability_zones   = ["${local.aws_region}a", "${local.aws_region}b", "${local.aws_region}c"]
  private_subnet_cidrs = ["10.2.0.0/20", "10.2.16.0/20", "10.2.32.0/20"]
  public_subnet_cidrs  = ["10.2.128.0/20", "10.2.144.0/20", "10.2.160.0/20"]

  single_nat_gateway                    = false # HA: one NAT per AZ in prod
  flow_log_retention_days               = 365
  flow_logs_kms_key_arn                 = module.kms_logs.key_arn
  enable_secrets_manager_endpoint       = true
  enable_cloudwatch_logs_endpoint       = true
  enable_cloudwatch_monitoring_endpoint = true
  enable_step_functions_endpoint        = true
  enable_glue_endpoint                  = true
  enable_kms_endpoint                   = true

  tags = local.common_tags
}

module "storage" {
  source = "../../modules/storage"
  providers = {
    aws         = aws
    aws.replica = aws.replica
  }

  replica_region                        = var.replica_region
  environment                           = local.environment
  name_prefix                           = local.name_prefix
  region_short                          = local.region_short
  replica_region_short                  = local.replica_region_short
  storage_kms_key_arn                   = module.kms_storage.key_arn
  extraction_runtime_role_arns          = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-extraction-${local.environment}-exec"]
  raw_object_lock_retention_days        = 365 # 1 year immutable retention in prod
  raw_noncurrent_version_retention_days = 30
  access_logs_retention_days            = 365
  tags                                  = local.common_tags
}

module "metadata_persistence" {
  source                    = "../../modules/metadata_persistence"
  environment               = local.environment
  name_prefix               = local.name_prefix
  database_kms_key_arn      = module.kms_database.key_arn
  orchestration_role_arns   = [module.iam.orchestration_step_functions_role_arn]
  replay_operator_role_arns = var.replay_operator_role_arns
  tags                      = local.common_tags
}

locals {
  programme_tables = module.metadata_persistence.programme_table_names

  prevent_destroy_tables = [
    module.metadata_persistence.watermark_repository_table_name,
    module.metadata_persistence.run_audit_log_table_name,
    module.metadata_persistence.entity_extraction_config_table_name,
    module.metadata_persistence.entity_type_registry_table_name,
    module.metadata_persistence.serving_store_config_table_name,
    module.metadata_persistence.twin_index_table_name,
    module.metadata_persistence.semantic_model_table_name,
    module.metadata_persistence.saved_query_table_name,
  ]

  tenant_keyed_tables = concat(
    [
      module.metadata_persistence.serving_store_config_table_name,
      module.metadata_persistence.source_onboarding_registry_table_name,
      module.metadata_persistence.twin_index_table_name,
      module.metadata_persistence.semantic_model_table_name,
      module.metadata_persistence.saved_query_table_name,
    ],
    [for purpose, name in local.programme_tables : name if purpose != "deletion-certificates"],
  )

  resource_names = merge(
    {
      RESOURCE_NAME_PREFIX = local.name_prefix
      SECRET_PATH_PREFIX   = "${local.name_prefix}/${local.environment}"

      AUDIT_LOG_TABLE            = module.metadata_persistence.run_audit_log_table_name
      RUN_AUDIT_LOG_TABLE        = module.metadata_persistence.run_audit_log_table_name
      ENTITY_CONFIG_TABLE        = module.metadata_persistence.entity_extraction_config_table_name
      ENTITY_TYPE_REGISTRY_TABLE = module.metadata_persistence.entity_type_registry_table_name
      WATERMARK_TABLE            = module.metadata_persistence.watermark_repository_table_name
      SERVING_STORE_CONFIG_TABLE = module.metadata_persistence.serving_store_config_table_name
      SOURCE_ONBOARDING_TABLE    = module.metadata_persistence.source_onboarding_registry_table_name
      TWIN_INDEX_TABLE           = module.metadata_persistence.twin_index_table_name
      SEMANTIC_MODEL_TABLE       = module.metadata_persistence.semantic_model_table_name
      SAVED_QUERY_TABLE          = module.metadata_persistence.saved_query_table_name

      TENANT_KEYED_TABLES = join(",", local.tenant_keyed_tables)
      TENANT_SCOPED_KEY_TABLES = join(",", [
        module.metadata_persistence.entity_extraction_config_table_name,
        module.metadata_persistence.watermark_repository_table_name,
      ])
      TENANT_ATTRIBUTED_TABLES = module.metadata_persistence.run_audit_log_table_name
      DELETION_EVIDENCE_TABLES = local.programme_tables["deletion-certificates"]
      PREVENT_DESTROY_TABLES   = join(",", local.prevent_destroy_tables)
    },
    {
      BACKFILL_JOB_TABLE           = local.programme_tables["backfill-jobs"]
      BRAND_REGISTRY_TABLE         = local.programme_tables["brand-registry"]
      CONFIG_GOVERNANCE_TABLE      = local.programme_tables["config-governance"]
      CONFIG_RESTATEMENT_TABLE     = local.programme_tables["config-restatements"]
      DATA_QUALITY_EXCEPTION_TABLE = local.programme_tables["data-quality-exceptions"]
      DELETION_CERTIFICATE_TABLE   = local.programme_tables["deletion-certificates"]
      EFFECTIVE_CONFIG_TABLE       = local.programme_tables["effective-config"]
      EXPORT_JOB_TABLE             = local.programme_tables["export-jobs"]
      QUALITY_POLICY_TABLE         = local.programme_tables["quality-policy-attachments"]
      RECONCILIATION_REPORT_TABLE  = local.programme_tables["reconciliation-reports"]
      SCOPE_UNIT_TABLE             = local.programme_tables["scope-units"]
      SEMANTIC_APPROVAL_TABLE      = local.programme_tables["semantic-approvals"]
      SERVING_CLAIM_TABLE          = local.programme_tables["serving-credential-claims"]
      SOURCE_CONNECTION_TABLE      = local.programme_tables["source-connections"]
      SUBPROCESSOR_TABLE           = local.programme_tables["subprocessor-register"]
      TENANT_USAGE_TABLE           = local.programme_tables["tenant-usage-metering"]
      WEBHOOK_DEDUP_TABLE          = local.programme_tables["webhook-event-dedup"]
      WORKFLOW_BREAKER_TABLE       = local.programme_tables["workflow-circuit-breaker"]
      WORKFLOW_DEFINITION_TABLE    = local.programme_tables["workflow-definitions"]
      WORKFLOW_DESTINATION_TABLE   = local.programme_tables["workflow-destinations"]
      WORKFLOW_EXECUTION_TABLE     = local.programme_tables["workflow-executions"]
      WORKFLOW_IDEMPOTENCY_TABLE   = local.programme_tables["workflow-idempotency"]
      WORKFLOW_TASK_TABLE          = local.programme_tables["workflow-tasks"]
    },
  )
}

module "serving_store_database" {
  source      = "../../modules/serving_store_database"
  environment = local.environment
  name_prefix = local.name_prefix
  engine      = "mysql"

  vpc_id     = module.networking.vpc_id
  subnet_ids = module.networking.private_subnet_ids

  storage_kms_key_arn = module.kms_database.key_arn
  secrets_kms_key_arn = module.kms_secrets.key_arn

  instance_class               = "db.r6g.large"
  multi_az                     = true
  deletion_protection          = true
  backup_retention_period_days = 30

  tags = local.common_tags
}

module "secrets" {
  source                       = "../../modules/secrets"
  code_signing_config_arn      = module.code_signing.code_signing_config_arn
  vpc_id                       = module.networking.vpc_id
  subnet_ids                   = module.networking.private_subnet_ids
  environment                  = local.environment
  name_prefix                  = local.name_prefix
  secrets_kms_key_arn          = module.kms_secrets.key_arn
  logs_kms_key_arn             = module.kms_logs.key_arn
  extraction_runtime_role_arns = [module.iam.extraction_runtime_role_arn]
  secret_recovery_window_days  = 30 # Maximum recovery window in prod

  credential_expiry_notifier_role_arn  = module.iam.credential_expiry_notifier_role_arn
  credential_expiry_scheduler_role_arn = module.iam.credential_expiry_scheduler_role_arn
  alert_topic_arn                      = module.observability.platform_alerts_topic_arn
  lambda_package_s3_bucket             = var.lambda_package_s3_bucket
  lambda_package_s3_key                = var.lambda_package_s3_key
  lambda_package_source_hash           = var.lambda_package_source_hash

  tags = local.common_tags

  resource_names = local.resource_names
}

module "iam" {
  source                         = "../../modules/iam"
  environment                    = local.environment
  name_prefix                    = local.name_prefix
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
    "arn:aws:secretsmanager:${local.aws_region}:${data.aws_caller_identity.current.account_id}:secret:datalake/<env>/serving-store/*",
  ]
  kms_key_arns_for_extraction                 = [module.kms_storage.key_arn, module.kms_secrets.key_arn, module.kms_database.key_arn]
  kms_key_arns_for_transformation             = [module.kms_storage.key_arn, module.kms_database.key_arn]
  kms_key_arns_for_credential_expiry_notifier = [module.kms_logs.key_arn]
  kms_key_arns_for_serving_store              = [module.kms_secrets.key_arn]
  github_org                                  = var.github_org
  github_repo                                 = var.github_repo
  cicd_deployment_policy_arns                 = var.cicd_deployment_policy_arns

  data_bucket_arns = [
    module.storage.raw_layer_bucket_arn,
    module.storage.curated_layer_bucket_arn,
    module.storage.analytics_layer_bucket_arn,
  ]
  tenant_scoped_table_arns = [
    module.metadata_persistence.entity_extraction_config_table_arn,
    module.metadata_persistence.watermark_repository_table_arn,
    module.metadata_persistence.twin_index_table_arn,
    module.metadata_persistence.semantic_model_table_arn,
    module.metadata_persistence.saved_query_table_arn,
    module.metadata_persistence.serving_store_config_table_arn,
  ]
  cloudtrail_log_group_name = module.audit_trail.log_group_name

  tenant_session_tagging_adopted = false

  tags = local.common_tags
}


module "audit_trail" {
  source = "../../modules/audit_trail"

  providers = {
    aws         = aws
    aws.replica = aws.replica
  }
  environment  = local.environment
  name_prefix  = local.name_prefix
  region_short = local.region_short
  account_id   = data.aws_caller_identity.current.account_id
  region       = local.aws_region

  access_log_bucket_id = module.storage.access_logs_bucket_id

  kms_key_arn      = module.kms_logs.key_arn
  logs_kms_key_arn = module.kms_logs.key_arn

  data_bucket_arns = [
    module.storage.raw_layer_bucket_arn,
    module.storage.curated_layer_bucket_arn,
    module.storage.analytics_layer_bucket_arn,
  ]

  tags = local.common_tags
}

module "observability" {
  enable_absence_alarms = false

  source                    = "../../modules/observability"
  environment               = local.environment
  name_prefix               = local.name_prefix
  logs_kms_key_arn          = module.kms_logs.key_arn
  log_retention_days        = 365
  alert_email               = var.alert_email
  watermark_lag_slo_seconds = 43200 # 12h strict SLO for prod
  tags                      = local.common_tags
}

module "glue" {
  source      = "../../modules/glue"
  environment = local.environment
  name_prefix = local.name_prefix

  curated_layer_bucket_id   = module.storage.curated_layer_bucket_id
  analytics_layer_bucket_id = module.storage.analytics_layer_bucket_id
  athena_results_bucket_id  = module.storage.analytics_layer_bucket_id
  kms_key_arn               = module.kms_storage.key_arn

  analytics_reader_principals = var.analytics_reader_principals

  tags = local.common_tags

  depends_on = [module.storage]
}


module "lambda_pipeline" {
  source                  = "../../modules/lambda_pipeline"
  code_signing_config_arn = module.code_signing.code_signing_config_arn
  environment             = local.environment
  name_prefix             = local.name_prefix

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
  log_retention_days       = 365
  memory_size_mb           = 1024
  timeout_seconds          = 900

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]

  resource_names = local.resource_names
}


module "transformation_lambda" {
  source      = "../../modules/transformation_lambda"
  environment = local.environment
  name_prefix = local.name_prefix

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
  log_retention_days       = 365
  memory_size_mb           = 1024
  timeout_seconds          = 900

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]

  resource_names = local.resource_names
}


module "entity_resolution_lambda" {
  source      = "../../modules/entity_resolution_lambda"
  environment = local.environment
  name_prefix = local.name_prefix

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
  log_retention_days       = 365
  memory_size_mb           = 1024
  timeout_seconds          = 900

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]

  resource_names = local.resource_names
}


module "analytics_publisher_lambda" {
  source                  = "../../modules/analytics_publisher_lambda"
  code_signing_config_arn = module.code_signing.code_signing_config_arn
  environment             = local.environment
  name_prefix             = local.name_prefix

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
  log_retention_days       = 365
  memory_size_mb           = 512
  timeout_seconds          = 300

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking, module.glue]

  resource_names = local.resource_names
}


module "serving_store_lambda" {
  source                  = "../../modules/serving_store_lambda"
  code_signing_config_arn = module.code_signing.code_signing_config_arn
  environment             = local.environment
  name_prefix             = local.name_prefix

  kms_key_arn        = module.kms_logs.key_arn
  execution_role_arn = module.iam.serving_store_loader_runtime_role_arn

  lambda_package_s3_bucket   = var.lambda_package_s3_bucket
  lambda_package_s3_key      = var.lambda_package_s3_key
  lambda_package_source_hash = var.lambda_package_source_hash

  analytics_s3_bucket_name  = module.storage.analytics_layer_bucket_id
  governance_s3_bucket_name = ""

  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = []

  cloudwatch_log_group_arn = module.observability.log_group_arns["serving-store-loader"]
  log_retention_days       = 365
  memory_size_mb           = 512
  timeout_seconds          = 300

  tags = local.common_tags

  depends_on = [module.iam, module.storage, module.networking]

  resource_names = local.resource_names
}

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
  name_prefix = local.name_prefix

  code_signing_config_arn = module.code_signing.code_signing_config_arn
  vpc_id                  = module.networking.vpc_id
  subnet_ids              = module.networking.private_subnet_ids

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

  resource_names = local.resource_names
}

module "orchestration" {
  source                  = "../../modules/orchestration"
  code_signing_config_arn = module.code_signing.code_signing_config_arn
  vpc_id                  = module.networking.vpc_id
  subnet_ids              = module.networking.private_subnet_ids

  environment = local.environment

  name_prefix             = local.name_prefix
  kms_key_arn             = module.kms_logs.key_arn
  step_functions_role_arn = module.iam.orchestration_step_functions_role_arn
  state_machine_type      = "STANDARD"
  log_retention_days      = 365
  alert_topic_arn         = module.observability.platform_alerts_topic_arn
  enable_xray_tracing     = true

  extraction_pipeline_lambda_arn     = var.extraction_pipeline_lambda_arn
  transformation_pipeline_lambda_arn = module.transformation_lambda.lambda_function_arn
  entity_resolution_lambda_arn       = module.entity_resolution_lambda.lambda_function_arn
  analytics_publisher_lambda_arn     = module.analytics_publisher_lambda.lambda_function_arn
  serving_store_loader_lambda_arn    = module.serving_store_lambda.lambda_function_arn
  twin_build_lambda_arn              = module.twin_build_lambda.lambda_function_arn

  lambda_package_s3_bucket   = var.lambda_package_s3_bucket
  lambda_package_s3_key      = var.lambda_package_s3_key
  lambda_package_source_hash = var.lambda_package_source_hash

  pipeline_trigger_role_arn = module.iam.pipeline_trigger_role_arn

  dlq_processor_batch_size           = 10
  dlq_processor_reserved_concurrency = 20

  dlq_processor_role_arn     = module.iam.dlq_processor_role_arn
  extraction_failure_dlq_arn = module.metadata_persistence.extraction_failure_dlq_arn
  run_audit_log_table_name   = module.metadata_persistence.run_audit_log_table_name

  tags = local.common_tags

  depends_on = [module.iam, module.observability]

  resource_names = local.resource_names
}


module "control_plane" {
  source                  = "../../modules/control_plane"
  code_signing_config_arn = module.code_signing.code_signing_config_arn
  vpc_id                  = module.networking.vpc_id
  subnet_ids              = module.networking.private_subnet_ids
  environment             = local.environment
  name_prefix             = local.name_prefix

  kms_key_arn         = module.kms_logs.key_arn
  log_retention_days  = 365
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

  resource_names = local.resource_names
}


module "waf" {
  source      = "../../modules/waf"
  environment = local.environment
  name_prefix = local.name_prefix
  region      = local.aws_region

  api_gateway_stage_arn = module.control_plane.api_stage_arn
  enforcement_mode      = var.waf_enforcement_mode
  logs_kms_key_arn      = module.kms_logs.key_arn
  alarm_sns_topic_arn   = module.observability.platform_alerts_topic_arn

  tags = local.common_tags
}

module "client_vpn" {
  source      = "../../modules/client_vpn"
  environment = local.environment
  name_prefix = local.name_prefix

  enabled              = var.client_vpn_enabled
  vpc_id               = module.networking.vpc_id
  private_subnet_ids   = module.networking.private_subnet_ids
  private_subnet_cidrs = var.private_subnet_cidrs

  server_certificate_arn      = var.vpn_server_certificate_arn
  client_root_certificate_arn = var.vpn_client_root_certificate_arn
  tenant_access_groups        = var.vpn_tenant_access_groups
  serving_store_port          = 3306

  logs_kms_key_arn    = module.kms_logs.key_arn
  alarm_sns_topic_arn = module.observability.platform_alerts_topic_arn

  tags = local.common_tags
}

module "lake_formation" {
  source      = "../../modules/lake_formation"
  environment = local.environment
  name_prefix = local.name_prefix

  glue_database_name       = module.glue.analytics_database_name
  tenant_codes             = var.lake_formation_tenant_codes
  tenant_scoped_principals = var.lake_formation_tenant_scoped_principals
  data_lake_admin_arns     = var.lake_formation_admin_arns

  tags = local.common_tags

  depends_on = [module.glue]
}


module "platform_lambdas" {
  source                  = "../../modules/platform_lambdas"
  code_signing_config_arn = module.code_signing.code_signing_config_arn
  vpc_id                  = module.networking.vpc_id
  subnet_ids              = module.networking.private_subnet_ids
  environment             = local.environment
  name_prefix             = local.name_prefix

  lambda_package_s3_bucket   = var.lambda_package_s3_bucket
  lambda_package_s3_key      = var.lambda_package_s3_key
  lambda_package_source_hash = var.lambda_package_source_hash

  webhook_receiver_role_arn = module.iam.webhook_receiver_role_arn
  writeback_role_arn        = module.iam.writeback_role_arn
  workflow_runner_role_arn  = module.iam.workflow_runner_role_arn
  portability_role_arn      = module.iam.portability_role_arn

  kms_key_arn      = module.kms_logs.key_arn
  logs_kms_key_arn = module.kms_logs.key_arn

  raw_s3_bucket_name       = module.storage.raw_layer_bucket_id
  curated_s3_bucket_name   = module.storage.curated_layer_bucket_id
  analytics_s3_bucket_name = module.storage.analytics_layer_bucket_id

  entity_config_table_name = module.metadata_persistence.entity_extraction_config_table_name
  run_audit_log_table_name = module.metadata_persistence.run_audit_log_table_name

  webhook_dedup_table_name        = module.metadata_persistence.programme_table_names["webhook-event-dedup"]
  workflow_definition_table_name  = module.metadata_persistence.programme_table_names["workflow-definitions"]
  workflow_execution_table_name   = module.metadata_persistence.programme_table_names["workflow-executions"]
  workflow_idempotency_table_name = module.metadata_persistence.programme_table_names["workflow-idempotency"]
  workflow_destination_table_name = module.metadata_persistence.programme_table_names["workflow-destinations"]
  workflow_task_table_name        = module.metadata_persistence.programme_table_names["workflow-tasks"]
  export_job_table_name           = module.metadata_persistence.programme_table_names["export-jobs"]
  deletion_certificate_table_name = module.metadata_persistence.programme_table_names["deletion-certificates"]

  control_plane_api_id            = ""
  control_plane_api_execution_arn = ""
  workflow_schedule_enabled       = false
  tenant_codes                    = []

  tags = local.common_tags

  resource_names = local.resource_names
}
