
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  common_tags = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "platform_lambdas"
  })

  functions = {
    webhook_receiver = {
      name        = "${var.name_prefix}-webhook-receiver-${var.environment}"
      handler     = "connector_runtime.webhook_receiver_handler.lambda_handler"
      description = "Provider webhook receiver: verifies the signature, deduplicates by provider event id, and enqueues to the FIFO ingest queue. Never processes inline (DL-CONN-14)."
      role_arn    = var.webhook_receiver_role_arn
      timeout     = 30
      memory      = 512
      environment = {
        WEBHOOK_INGEST_QUEUE_URL = var.webhook_ingest_queue_url
        WEBHOOK_DEDUP_TABLE      = var.resource_names["WEBHOOK_DEDUP_TABLE"]
        RESOURCE_NAME_PREFIX     = var.resource_names["RESOURCE_NAME_PREFIX"]
        SECRET_PATH_PREFIX       = var.resource_names["SECRET_PATH_PREFIX"]
      }
    }
    writeback = {
      name        = "${var.name_prefix}-connector-writeback-${var.environment}"
      handler     = "connector_runtime.writeback_handler.lambda_handler"
      description = "Bi-directional write-back to a source system. Gated on the entity's own writeback_enabled flag and a separate write-back credential (DL-CONN-02)."
      role_arn    = var.writeback_role_arn
      timeout     = 300
      memory      = 512
      environment = {
        ENTITY_CONFIG_TABLE     = var.resource_names["ENTITY_CONFIG_TABLE"]
        AUDIT_LOG_TABLE         = var.resource_names["AUDIT_LOG_TABLE"]
        SOURCE_CONNECTION_TABLE = var.resource_names["SOURCE_CONNECTION_TABLE"]
        RESOURCE_NAME_PREFIX    = var.resource_names["RESOURCE_NAME_PREFIX"]
        SECRET_PATH_PREFIX      = var.resource_names["SECRET_PATH_PREFIX"]
      }
    }
    workflow_runner = {
      name        = "${var.name_prefix}-workflow-runner-${var.environment}"
      handler     = "workflow_automation.workflow_runner_handler.lambda_handler"
      description = "Scheduled evaluation of a tenant's published workflows. Idempotency is mandatory, so a retried schedule cannot send a duplicate notification (DL-WF-07)."
      role_arn    = var.workflow_runner_role_arn
      timeout     = 600
      memory      = 1024
      environment = {
        WORKFLOW_DEFINITION_TABLE  = var.resource_names["WORKFLOW_DEFINITION_TABLE"]
        WORKFLOW_EXECUTION_TABLE   = var.resource_names["WORKFLOW_EXECUTION_TABLE"]
        WORKFLOW_IDEMPOTENCY_TABLE = var.resource_names["WORKFLOW_IDEMPOTENCY_TABLE"]
        WORKFLOW_DESTINATION_TABLE = var.resource_names["WORKFLOW_DESTINATION_TABLE"]
        WORKFLOW_TASK_TABLE        = var.resource_names["WORKFLOW_TASK_TABLE"]
        WORKFLOW_BREAKER_TABLE     = var.resource_names["WORKFLOW_BREAKER_TABLE"]
        RESOURCE_NAME_PREFIX       = var.resource_names["RESOURCE_NAME_PREFIX"]
        SECRET_PATH_PREFIX         = var.resource_names["SECRET_PATH_PREFIX"]
      }
    }
    portability = {
      name        = "${var.name_prefix}-portability-${var.environment}"
      handler     = "portability.portability_handler.lambda_handler"
      description = "Tenant export and tenant deletion. Both are maker-checker privileged operations and both emit AdminActions (DL-PORT-01..10)."
      role_arn    = var.portability_role_arn
      timeout     = 900
      memory      = 1024
      environment = {
        EXPORT_ARTEFACT_BUCKET     = var.export_artefact_bucket_name
        EXPORT_JOB_TABLE           = var.resource_names["EXPORT_JOB_TABLE"]
        DELETION_CERTIFICATE_TABLE = var.resource_names["DELETION_CERTIFICATE_TABLE"]
        RAW_S3_BUCKET              = var.raw_s3_bucket_name
        CURATED_S3_BUCKET          = var.curated_s3_bucket_name
        ANALYTICS_S3_BUCKET        = var.analytics_s3_bucket_name
        SCOPE_UNIT_TABLE           = var.resource_names["SCOPE_UNIT_TABLE"]
        SERVING_STORE_CONFIG_TABLE = var.resource_names["SERVING_STORE_CONFIG_TABLE"]
        RESOURCE_NAME_PREFIX       = var.resource_names["RESOURCE_NAME_PREFIX"]
        SECRET_PATH_PREFIX         = var.resource_names["SECRET_PATH_PREFIX"]
        TENANT_KEYED_TABLES        = var.resource_names["TENANT_KEYED_TABLES"]
        TENANT_SCOPED_KEY_TABLES   = var.resource_names["TENANT_SCOPED_KEY_TABLES"]
        TENANT_ATTRIBUTED_TABLES   = var.resource_names["TENANT_ATTRIBUTED_TABLES"]
        DELETION_EVIDENCE_TABLES   = var.resource_names["DELETION_EVIDENCE_TABLES"]
        PREVENT_DESTROY_TABLES     = var.resource_names["PREVENT_DESTROY_TABLES"]
      }
    }
  }
}

resource "aws_cloudwatch_log_group" "lambda_execution" {
  for_each = local.functions

  name              = "/aws/lambda/${each.value.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn

  tags = merge(local.common_tags, { Name = "/aws/lambda/${each.value.name}" })
}

resource "aws_lambda_function" "platform" {
  for_each = local.functions

  function_name = each.value.name
  description   = each.value.description

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  runtime = "python3.13"

  code_signing_config_arn = var.code_signing_config_arn


  dead_letter_config {

    target_arn = aws_sqs_queue.async_dlq.arn

  }


  dynamic "vpc_config" {


    for_each = var.vpc_id == null ? [] : [1]


    content {


      subnet_ids = var.subnet_ids


      security_group_ids = concat(var.security_group_ids, aws_security_group.platform_lambdas_lambda[*].id)


    }


  }
  handler     = each.value.handler
  role        = each.value.role_arn
  memory_size = each.value.memory
  timeout     = each.value.timeout

  kms_key_arn = var.kms_key_arn

  reserved_concurrent_executions = var.reserved_concurrent_executions

  environment {
    variables = merge(
      { PLATFORM_ENVIRONMENT = var.environment },
      each.value.environment,
    )
  }

  tracing_config {
    mode = var.enable_xray_tracing ? "Active" : "PassThrough"
  }

  depends_on = [aws_cloudwatch_log_group.lambda_execution]

  tags = merge(local.common_tags, { Name = each.value.name })
}


resource "aws_lambda_permission" "webhook_api_invoke" {
  count = var.control_plane_api_execution_arn == "" ? 0 : 1

  statement_id  = "AllowControlPlaneApiInvokeWebhook"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.platform["webhook_receiver"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${var.control_plane_api_execution_arn}/*/*"
}

resource "aws_apigatewayv2_integration" "webhook" {
  count = var.control_plane_api_id == "" ? 0 : 1

  api_id                 = var.control_plane_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.platform["webhook_receiver"].invoke_arn
  payload_format_version = "1.0"
}

resource "aws_apigatewayv2_route" "webhook" {
  count = var.control_plane_api_id == "" ? 0 : 1

  api_id    = var.control_plane_api_id
  route_key = "POST /webhooks/{tenant_code}/{source_id}/{connection_id}"
  target    = "integrations/${aws_apigatewayv2_integration.webhook[0].id}"
  #checkov:skip=CKV_AWS_309:Authenticated by provider HMAC signature, verified fail-closed in the handler.
  authorization_type = "NONE"
}


resource "aws_scheduler_schedule" "workflow_runner" {
  for_each = var.workflow_schedule_enabled ? toset(var.tenant_codes) : toset([])

  name                         = "${var.name_prefix}-workflow-runner-${each.value}-${var.environment}"
  group_name                   = var.scheduler_group_name
  schedule_expression          = var.workflow_schedule_expression
  schedule_expression_timezone = "UTC"
  state                        = "ENABLED"
  kms_key_arn                  = var.kms_key_arn

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 5
  }

  target {
    arn      = aws_lambda_function.platform["workflow_runner"].arn
    role_arn = var.scheduler_invoke_role_arn

    input = jsonencode({
      tenant_code = each.value
      dry_run     = false
    })

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }

    dead_letter_config {
      arn = var.workflow_dlq_arn
    }
  }
}

resource "aws_lambda_permission" "workflow_scheduler_invoke" {
  count = var.workflow_schedule_enabled ? 1 : 0

  statement_id  = "AllowSchedulerInvokeWorkflowRunner"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.platform["workflow_runner"].function_name
  principal     = "scheduler.amazonaws.com"

  source_arn = "arn:${data.aws_partition.current.partition}:scheduler:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:schedule/${var.scheduler_group_name}/*"
}


resource "aws_sqs_queue" "async_dlq" {
  name                      = "${var.name_prefix}-platform-async-dlq-${var.environment}"
  message_retention_seconds = 1209600 # 14 days, the maximum — a DLQ that expires loses the evidence
  sqs_managed_sse_enabled   = true

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-platform-async-dlq-${var.environment}" })
}


resource "aws_security_group" "platform_lambdas_lambda" {
  #checkov:skip=CKV2_AWS_5:Attached via dynamic vpc_config in this module.
  count = var.vpc_id == null ? 0 : 1

  name        = "${var.name_prefix}-platform-lambdas-${var.environment}-sg"
  description = "HTTPS egress only for the PlatformLambdas Lambda function(s)."
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS egress to AWS service endpoints."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-platform-lambdas-${var.environment}-sg" })

  lifecycle {
    create_before_destroy = true
  }
}
