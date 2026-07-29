# ---------------------------------------------------------------------------
# Four Lambda functions whose handlers existed in code with no deployed function (S8, S9).
#
# The 2026-07-28 audit found `webhook_receiver_handler`, `writeback_handler`,
# `workflow_runner_handler` and `portability_handler` complete and untestable in a real
# environment because no `aws_lambda_function` referenced them. They are grouped in one module
# because they share a shape — same package, same log-group convention, same tracing — and
# splitting them into four modules would quadruple the boilerplate for no isolation benefit.
# They do NOT share an IAM role: each is passed its own, so the write-back function cannot read
# the webhook secret and the portability function's delete permissions are not granted to
# anything on the ingestion path (OWASP A01, least privilege).
#
# All four are deliberately absent from the extraction state machine: webhooks arrive from
# outside, write-back is triggered by a workflow action, the runner is scheduled, and portability
# is invoked by an operator or the control plane.
# ---------------------------------------------------------------------------

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
      name        = "EdlWebhookReceiver"
      handler     = "connector_runtime.webhook_receiver_handler.lambda_handler"
      description = "Provider webhook receiver: verifies the signature, deduplicates by provider event id, and enqueues to the FIFO ingest queue. Never processes inline (DL-CONN-14)."
      role_arn    = var.webhook_receiver_role_arn
      timeout     = 30
      memory      = 512
      environment = {
        WEBHOOK_INGEST_QUEUE_URL = var.webhook_ingest_queue_url
        WEBHOOK_DEDUP_TABLE      = var.webhook_dedup_table_name
      }
    }
    writeback = {
      name        = "EdlConnectorWriteback"
      handler     = "connector_runtime.writeback_handler.lambda_handler"
      description = "Bi-directional write-back to a source system. Gated on the entity's own writeback_enabled flag and a separate write-back credential (DL-CONN-02)."
      role_arn    = var.writeback_role_arn
      timeout     = 300
      memory      = 512
      environment = {
        ENTITY_CONFIG_TABLE = var.entity_config_table_name
        AUDIT_LOG_TABLE     = var.run_audit_log_table_name
      }
    }
    workflow_runner = {
      name        = "EdlWorkflowRunner"
      handler     = "workflow_automation.workflow_runner_handler.lambda_handler"
      description = "Scheduled evaluation of a tenant's published workflows. Idempotency is mandatory, so a retried schedule cannot send a duplicate notification (DL-WF-07)."
      role_arn    = var.workflow_runner_role_arn
      timeout     = 600
      memory      = 1024
      environment = {
        WORKFLOW_DEFINITION_TABLE  = var.workflow_definition_table_name
        WORKFLOW_EXECUTION_TABLE   = var.workflow_execution_table_name
        WORKFLOW_IDEMPOTENCY_TABLE = var.workflow_idempotency_table_name
        WORKFLOW_DESTINATION_TABLE = var.workflow_destination_table_name
        WORKFLOW_TASK_TABLE        = var.workflow_task_table_name
      }
    }
    portability = {
      name        = "EdlPortability"
      handler     = "portability.portability_handler.lambda_handler"
      description = "Tenant export and tenant deletion. Both are maker-checker privileged operations and both emit AdminActions (DL-PORT-01..10)."
      role_arn    = var.portability_role_arn
      timeout     = 900
      memory      = 1024
      environment = {
        EXPORT_ARTEFACT_BUCKET     = var.export_artefact_bucket_name
        EXPORT_JOB_TABLE           = var.export_job_table_name
        DELETION_CERTIFICATE_TABLE = var.deletion_certificate_table_name
        RAW_S3_BUCKET              = var.raw_s3_bucket_name
        CURATED_S3_BUCKET          = var.curated_s3_bucket_name
        ANALYTICS_S3_BUCKET        = var.analytics_s3_bucket_name
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

  runtime     = "python3.13"
  handler     = each.value.handler
  role        = each.value.role_arn
  memory_size = each.value.memory
  timeout     = each.value.timeout

  kms_key_arn = var.kms_key_arn

  # Bounds one function's share of the account concurrency pool (CKV_AWS_115).
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

# ---------------------------------------------------------------------------
# Webhook receiver: reachable from the control-plane HTTP API.
#
# The route is unauthenticated by design — a provider cannot present a Cognito token — so the
# **signature** is the authentication, verified in the handler and failing closed. That is why the
# handler must never be given a route that skips verification, and why the WAF (audit mode today)
# matters more on this route than on the authenticated ones.
# ---------------------------------------------------------------------------

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
  # No authorizer: the provider signature is the credential (see the comment above). A third-party
  # webhook cannot present a Cognito JWT, so an authorizer here would reject every real delivery.
  #checkov:skip=CKV_AWS_309:Authenticated by provider HMAC signature, verified fail-closed in the handler.
  authorization_type = "NONE"
}

# ---------------------------------------------------------------------------
# Workflow runner: one schedule per tenant, so one tenant's workflow volume cannot starve
# another's and a single tenant's failure is contained to its own invocation.
# ---------------------------------------------------------------------------

resource "aws_scheduler_schedule" "workflow_runner" {
  for_each = var.workflow_schedule_enabled ? toset(var.tenant_codes) : toset([])

  name                         = "edl-workflow-runner-${each.value}"
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

  # Without this, *any* schedule in the account could invoke this function, not only ours
  # (CKV_AWS_364). Scoped to this environment's scheduler group.
  source_arn = "arn:${data.aws_partition.current.partition}:scheduler:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:schedule/${var.scheduler_group_name}/*"
}
