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
data "aws_region" "current" {}

locals {
  common_tags = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "orchestration"
  })
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  # Whether the serving store stage is active (ARN provided) or skipped (Pass state).
  serving_store_enabled = var.serving_store_loader_lambda_arn != ""

  # Build each branch as a JSON string first (both are type `string`), then
  # jsondecode to `any`.  This makes the conditional type-consistent — Terraform
  # cannot infer structural types through a string → jsondecode boundary.
  _serving_store_task_json = jsonencode({
    Type     = "Task"
    Resource = var.serving_store_loader_lambda_arn
    Parameters = {
      "source_id.$"           = "$.source_id"
      "entity_id.$"           = "$.entity_id"             # tracing/log context only
      "entity_type.$"         = "$.analytics.entity_type" # actual config lookup key
      "environment.$"         = "$.environment"
      "run_id.$"              = "$.extraction.run_id"
      "analytics_s3_prefix.$" = "$.analytics.analytics_s3_prefix"
      "tenant_code.$"         = "$.tenant_code"
    }
    ResultPath = "$.serving"
    Retry = [
      {
        ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException", "TransientServingError"]
        IntervalSeconds = 30
        MaxAttempts     = 5
        BackoffRate     = 2.0
        JitterStrategy  = "FULL"
      }
    ]
    End = true
  })
  _serving_store_pass_json = jsonencode({
    Type    = "Pass"
    Comment = "Serving store loader not yet deployed. Pipeline ends successfully after analytics publication."
    End     = true
  })
  # Conditional between two strings is type-consistent; jsondecode returns `any`.
  load_serving_store_state = jsondecode(
    local.serving_store_enabled ?
    local._serving_store_task_json :
    local._serving_store_pass_json
  )

  # BuildTwin stage: active when a twin builder ARN is provided, otherwise a Pass.
  # Both branches continue to LoadServingStore; a twin-build failure is caught and
  # skipped so this additive stage can never fail the core pipeline.
  twin_build_enabled = var.twin_build_lambda_arn != ""
  _build_twin_task_json = jsonencode({
    Type     = "Task"
    Resource = var.twin_build_lambda_arn
    Parameters = {
      "source_id.$"   = "$.source_id"
      "entity_id.$"   = "$.entity_id"
      "environment.$" = "$.environment"
      "run_id.$"      = "$.extraction.run_id"
      "tenant_code.$" = "$.tenant_code"
    }
    ResultPath = "$.twin"
    Retry = [
      {
        ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"]
        IntervalSeconds = 20
        MaxAttempts     = 3
        BackoffRate     = 2.0
        JitterStrategy  = "FULL"
      }
    ]
    Catch = [
      {
        ErrorEquals = ["States.ALL"]
        Next        = "LoadServingStore"
        ResultPath  = "$.twin_error"
      }
    ]
    Next = "LoadServingStore"
  })
  _build_twin_pass_json = jsonencode({
    Type    = "Pass"
    Comment = "Twin builder not yet deployed. Pipeline continues to the serving store stage."
    Next    = "LoadServingStore"
  })
  build_twin_state = jsondecode(
    local.twin_build_enabled ?
    local._build_twin_task_json :
    local._build_twin_pass_json
  )

  # Step Functions state machine name
  state_machine_name = "EdlExtractionPipeline"

  # EventBridge Scheduler schedule group name
  schedule_group_name = "EdlExtractionSchedules"

  # CloudWatch log group for Step Functions execution history
  sfn_log_group_name = "/edl/step-functions/extraction-pipeline"

  # SQS FIFO pipeline trigger queue name
  pipeline_trigger_queue_name = "EdlPipelineTrigger.fifo"

  # Pipeline trigger Lambda name
  pipeline_trigger_lambda_name = "EdlPipelineTrigger"

  # DLQ processor Lambda name
  dlq_processor_lambda_name = "EdlDlqProcessor"
}

# ---------------------------------------------------------------------------
# CloudWatch Log Group — Step Functions execution history
# Encrypted with the platform KMS key; logs capture all execution events
# including task input/output (sensitive values must be scrubbed before SFN).
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "sfn_execution" {
  name              = local.sfn_log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = merge(local.common_tags, {
    Name    = local.sfn_log_group_name
    Service = "step-functions"
  })
}

# CloudWatch log resource-based policy — pre-authorises the Step Functions log
# delivery service to write execution history to the log group above.
# This avoids granting logs:PutResourcePolicy to the SFN role (OWASP A01).
resource "aws_cloudwatch_log_resource_policy" "sfn_log_delivery" {
  policy_name = "${local.state_machine_name}LogDelivery"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowStepFunctionsLogDelivery"
        Effect = "Allow"
        Principal = {
          Service = ["delivery.logs.amazonaws.com", "states.amazonaws.com"]
        }
        Action = [
          "logs:CreateLogDelivery",
          "logs:GetLogDelivery",
          "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery",
          "logs:ListLogDeliveries",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeResourcePolicies",
        ]
        Resource = "*"
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Step Functions State Machine — Extraction Pipeline
#
# Design: single-task Express Workflow that invokes the extraction pipeline
# Lambda.  The Lambda (implemented in connector_runtime) handles all pipeline
# stages internally via ExtractionWorkflow and routes failures to the DLQ.
# Express Workflows are chosen over Standard Workflows because:
#   - Extraction runs complete in < 5 minutes for most entities.
#   - Express Workflows support higher throughput (needed for concurrent entities).
#   - Execution history is forwarded to CloudWatch Logs for auditability.
#
# Retry configuration:
#   - TransientExtractionError (network/throttle/timeout): 3 attempts,
#     10-second initial interval, 2× backoff.
#   - All other errors: no retry (DLQ routing handled in the Lambda).
#
# Error handling:
#   - Terminal failures are caught and forwarded to the DLQ Lambda for
#     structured enqueue — this is defense-in-depth (the pipeline Lambda
#     also enqueues on failure, but this catch covers unexpected Lambda crashes).
# ---------------------------------------------------------------------------

resource "aws_sfn_state_machine" "extraction_pipeline" {
  name = local.state_machine_name
  # Standard Workflow: supports execution history > 5 min, human-approval waits,
  # and at-least-once execution guarantees needed for staging/prod reliability.
  # Dev may use EXPRESS for lower cost; controlled by var.state_machine_type.
  type     = var.state_machine_type
  role_arn = var.step_functions_role_arn

  definition = jsonencode({
    Comment = "Enterprise Data Lake — full end-to-end pipeline: extraction → transformation → entity resolution → analytics → serving store."
    StartAt = "ExecuteExtraction"
    States = {

      # ── Stage A: Extraction ─────────────────────────────────────────────────
      # Runs ExtractionWorkflow: config load, credential retrieval, metadata
      # discovery, query build, extraction, schema snapshot, drift evaluation,
      # watermark update.
      # Output key checked: transformation_blocked (bool)
      ExecuteExtraction = {
        Type     = "Task"
        Resource = var.extraction_pipeline_lambda_arn
        # Pass full input to Lambda as-is; Lambda validates required fields.
        # ResultPath merges Lambda output into execution state under $.extraction.
        ResultPath = "$.extraction"
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException", "TransientExtractionError"]
            IntervalSeconds = 30
            MaxAttempts     = 5
            BackoffRate     = 2.0
            JitterStrategy  = "FULL"
          }
        ]
        Catch = [
          {
            # PERF-5: LambdaTimeoutWarning (raised by ExtractionWorkflow when
            # a max_records_per_lambda_run checkpoint fires) is NON-FATAL —
            # the extraction Lambda already committed a partial watermark
            # advance and a distinct '{run_id}-partN' audit record before
            # raising. Matched BEFORE the States.ALL catch-all below (Step
            # Functions evaluates Catch entries in order; first match wins),
            # so it does NOT fall through to ExtractionFailed / the DLQ.
            #
            # L14: this now routes to a resume loop rather than a terminal Succeed. Option (a)
            # from the note that used to live here turned out to be expressible: the checkpoint's
            # resume payload is carried in the exception *message*, and
            # `States.StringToJson($.checkpoint.Cause)` parses it into state the next
            # ExecuteExtraction invocation can read from its own Parameters. That is what makes
            # auto-resume possible without redesigning the Lambda's input contract.
            #
            # The loop is bounded by $.resume_attempts (see EvaluateResume) so a provider that
            # never stops throttling ends at a visible terminal state rather than looping forever.
            ErrorEquals = ["LambdaTimeoutWarning"]
            Next        = "ParseCheckpoint"
            ResultPath  = "$.checkpoint"
          },
          {
            ErrorEquals = ["States.ALL"]
            Next        = "ExtractionFailed"
            ResultPath  = "$.error"
          }
        ]
        Next = "CheckTransformationBlocked"
      }

      # ── L14: checkpoint resume loop ─────────────────────────────────────────
      #
      # The extraction Lambda raises LambdaTimeoutWarning for three distinct, non-fatal reasons:
      # a record-count checkpoint, an approaching Lambda timeout, and a provider rate-limit wait
      # too long to absorb in-process. All three mean "progress was made and committed; resume".
      #
      # `Cause` is the exception message, which carries the resume payload as JSON. Parsing it here
      # is what lets the retried Task read the resume position from its own Parameters.
      ParseCheckpoint = {
        Type = "Pass"
        Parameters = {
          "resume.$"                 = "States.StringToJson($.checkpoint.Cause)"
          "resume_attempts.$"        = "States.MathAdd($.resume_attempts, 1)"
          "source_id.$"              = "$.source_id"
          "entity_id.$"              = "$.entity_id"
          "environment.$"            = "$.environment"
          "tenant_code.$"            = "$.tenant_code"
          "connector_params.$"       = "$.connector_params"
          "is_replay.$"              = "$.is_replay"
          "pinned_config_versions.$" = "$.pinned_config_versions"
        }
        Next = "EvaluateResume"
      }

      EvaluateResume = {
        Type = "Choice"
        Choices = [
          {
            # Bounded: a provider that throttles indefinitely must not loop forever, and an
            # unbounded loop would also make the execution history unreadable.
            Variable           = "$.resume_attempts"
            NumericGreaterThan = var.max_extraction_resume_attempts
            Next               = "ExtractionResumeExhausted"
          },
          {
            # A rate-limit checkpoint carries a wait; honour it before resuming.
            Variable           = "$.resume.retry_after_seconds"
            NumericGreaterThan = 0
            Next               = "WaitForRateLimit"
          }
        ]
        # A record-count or timeout checkpoint needs no wait — resume immediately.
        Default = "ExecuteExtraction"
      }

      # Free: a Wait state costs nothing while it waits, which is the whole point of moving the
      # provider's Retry-After out of billed Lambda time.
      WaitForRateLimit = {
        Type        = "Wait"
        SecondsPath = "$.resume.retry_after_seconds"
        Next        = "ExecuteExtraction"
      }

      # Terminal: the resume loop hit its bound. Data written so far is valid and the watermark was
      # partially advanced, so this is not a data-loss failure — but the remaining window is
      # unprocessed and needs an operator to look at why the provider kept refusing.
      ExtractionResumeExhausted = {
        Type  = "Fail"
        Error = "ExtractionResumeExhausted"
        Cause = "Extraction checkpointed more times than max_extraction_resume_attempts allows. Records written so far are durable and the watermark advanced partially; the remaining window is unprocessed."
      }

      # Terminal: retained for executions that predate the resume loop and for a checkpoint that
      # deliberately stops (reason=operator_requested).
      ExtractionCheckpointed = {
        Type = "Succeed"
      }

      # Guard: breaking schema drift blocks all downstream stages.
      CheckTransformationBlocked = {
        Type = "Choice"
        Choices = [
          {
            Variable      = "$.extraction.transformation_blocked"
            BooleanEquals = true
            Next          = "ExtractionCompleteTransformationBlocked"
          }
        ]
        Default = "RunTransformation"
      }

      # Terminal: extraction succeeded but downstream is intentionally blocked.
      # Raw data is preserved; operator must resolve schema drift before replaying.
      ExtractionCompleteTransformationBlocked = {
        Type = "Succeed"
        # Step Functions Succeed state has no Comment field in ASL;
        # the CloudWatch log and structured log from the Lambda carry the detail.
      }

      # ── Stage B: Transformation ─────────────────────────────────────────────
      # Reads raw Parquet, applies field mappings, runs quality policy,
      # writes canonical Parquet to curated layer, registers Glue Catalog.
      # Output key checked: is_publication_blocked (bool)
      RunTransformation = {
        Type     = "Task"
        Resource = var.transformation_pipeline_lambda_arn
        Parameters = {
          "source_id.$"     = "$.source_id"
          "entity_id.$"     = "$.entity_id"
          "environment.$"   = "$.environment"
          "run_id.$"        = "$.extraction.run_id"
          "raw_s3_prefix.$" = "$.extraction.raw_s3_prefix"
          "mapping_version" = "latest"
          "tenant_code.$"   = "$.tenant_code"
        }
        ResultPath = "$.transformation"
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException", "TransientTransformationError"]
            IntervalSeconds = 30
            MaxAttempts     = 5
            BackoffRate     = 2.0
            JitterStrategy  = "FULL"
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "TransformationFailed"
            ResultPath  = "$.error"
          }
        ]
        Next = "CheckPublicationBlocked"
      }

      # Guard: blocking quality violation OR zero records stops entity resolution.
      CheckPublicationBlocked = {
        Type = "Choice"
        Choices = [
          {
            Variable      = "$.transformation.is_publication_blocked"
            BooleanEquals = true
            Next          = "TransformationCompletePublicationBlocked"
          },
          {
            Variable = "$.transformation.curated_s3_prefix"
            IsNull   = true
            Next     = "TransformationCompleteNoRecords"
          }
        ]
        Default = "RunEntityResolution"
      }

      # Terminal: transformation succeeded but quality gate blocked publication.
      TransformationCompletePublicationBlocked = {
        Type = "Succeed"
      }

      # Terminal: extraction returned 0 records — nothing to transform or resolve.
      TransformationCompleteNoRecords = {
        Type = "Succeed"
      }

      # ── Stage C: Entity Resolution ──────────────────────────────────────────
      # Matches curated records across source systems, applies survivorship
      # policy, produces golden records with full lineage.
      RunEntityResolution = {
        Type     = "Task"
        Resource = var.entity_resolution_lambda_arn
        Parameters = {
          "source_id.$"         = "$.source_id"
          "entity_id.$"         = "$.entity_id"
          "environment.$"       = "$.environment"
          "run_id.$"            = "$.extraction.run_id"
          "curated_s3_prefix.$" = "$.transformation.curated_s3_prefix"
          "tenant_code.$"       = "$.tenant_code"
        }
        ResultPath = "$.entity_resolution"
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException", "TransientResolutionError"]
            IntervalSeconds = 30
            MaxAttempts     = 5
            BackoffRate     = 2.0
            JitterStrategy  = "FULL"
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "EntityResolutionFailed"
            ResultPath  = "$.error"
          }
        ]
        Next = "PublishAnalytics"
      }

      # ── Stage D: Analytics Layer Publish ────────────────────────────────────
      # Reads golden records and curated datasets, writes consumption-optimised
      # Parquet to the analytics layer, registers/updates Glue Catalog table.
      PublishAnalytics = {
        Type     = "Task"
        Resource = var.analytics_publisher_lambda_arn
        Parameters = {
          "source_id.$"         = "$.source_id"
          "entity_id.$"         = "$.entity_id"
          "environment.$"       = "$.environment"
          "run_id.$"            = "$.extraction.run_id"
          "canonical_prefix.$"  = "$.entity_resolution.canonical_prefix"
          "curated_s3_prefix.$" = "$.transformation.curated_s3_prefix"
          "tenant_code.$"       = "$.tenant_code"
          # §5.7 / OBS-4: end-to-end pipeline SLA metric — extraction's
          # started_at is still addressable here even though it is not part
          # of any intermediate stage's own output.
          "run_started_at.$" = "$.extraction.started_at"
        }
        ResultPath = "$.analytics"
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException", "TransientPublishError"]
            IntervalSeconds = 30
            MaxAttempts     = 5
            BackoffRate     = 2.0
            JitterStrategy  = "FULL"
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "AnalyticsPublishFailed"
            ResultPath  = "$.error"
          }
        ]
        Next = "BuildTwin"
      }

      # ── Stage E: Build Twin (conditional — Pass if no ARN; failure is caught) ─
      BuildTwin = local.build_twin_state

      # ── Stage F: Serving Store Load (conditional — Pass if no ARN) ────────────
      LoadServingStore = local.load_serving_store_state

      # ── Failure terminal states ─────────────────────────────────────────────
      # Each failure state records the terminal failure in Step Functions
      # execution history.  The pipeline Lambda for each stage already enqueued
      # a DLQ entry; these states are defense-in-depth for unexpected crashes.

      ExtractionFailed = {
        Type  = "Fail"
        Error = "ExtractionFailed"
        Cause = "Extraction pipeline failed after all retry attempts. See DLQ and CloudWatch Logs."
      }

      TransformationFailed = {
        Type  = "Fail"
        Error = "TransformationFailed"
        Cause = "Transformation pipeline failed after all retry attempts. See DLQ and CloudWatch Logs."
      }

      EntityResolutionFailed = {
        Type  = "Fail"
        Error = "EntityResolutionFailed"
        Cause = "Entity resolution failed after all retry attempts. See DLQ and CloudWatch Logs."
      }

      AnalyticsPublishFailed = {
        Type  = "Fail"
        Error = "AnalyticsPublishFailed"
        Cause = "Analytics layer publish failed after all retry attempts. See DLQ and CloudWatch Logs."
      }
    }
  })

  logging_configuration {
    # `include_execution_data` stays false: state input/output carries source metadata, and this
    # log group is not tenant-partitioned, so enabling it would put one tenant's payloads where
    # any reader of the group can see them. Logging itself is on at level ALL.
    #checkov:skip=CKV_AWS_285:Execution data excluded deliberately; see above. Logging is enabled.
    log_destination        = "${aws_cloudwatch_log_group.sfn_execution.arn}:*"
    include_execution_data = false
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = var.enable_xray_tracing
  }

  tags = merge(local.common_tags, {
    Name = local.state_machine_name
  })

  depends_on = [aws_cloudwatch_log_resource_policy.sfn_log_delivery]
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler Schedule Group
#
# Each entity extraction is scheduled as a separate schedule within this
# group.  Schedules are managed at runtime via ExtractionScheduleClient
# (not via Terraform, because schedules are data — entity configs drive them).
#
# The group is encrypted with the platform KMS key.
# ---------------------------------------------------------------------------

resource "aws_scheduler_schedule_group" "extraction_schedules" {
  name = local.schedule_group_name

  tags = merge(local.common_tags, {
    Name = local.schedule_group_name
  })
}

# ---------------------------------------------------------------------------
# CloudWatch Metric Alarm — pipeline failure rate
#
# Triggers when any extraction pipeline execution fails (terminal state).
# Step Functions Express Workflow execution metrics are reported to CloudWatch
# under the AWS/States namespace.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "sfn_execution_failures" {
  alarm_name          = "EdlPipelineExecutionFailures"
  alarm_description   = "One or more extraction pipeline Step Functions executions have failed. Check DLQ and CloudWatch Logs."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"

  metric_name = "ExecutionsFailed"
  namespace   = "AWS/States"
  statistic   = "Sum"
  period      = 300

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.extraction_pipeline.arn
  }

  alarm_actions = var.alert_topic_arn != "" ? [var.alert_topic_arn] : []
  ok_actions    = var.alert_topic_arn != "" ? [var.alert_topic_arn] : []

  tags = merge(local.common_tags, {
    Name = "EdlPipelineExecutionFailures"
  })
}

# ---------------------------------------------------------------------------
# CloudWatch Metric Alarm — throttled executions
#
# Express Workflows have concurrency limits; throttled executions indicate
# the platform needs provisioned concurrency or rate-limit adjustment.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "sfn_executions_throttled" {
  alarm_name          = "EdlPipelineExecutionsThrottled"
  alarm_description   = "Step Functions executions are being throttled. Increase concurrency limits."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = 0
  treat_missing_data  = "notBreaching"

  metric_name = "ExecutionsThrottled"
  namespace   = "AWS/States"
  statistic   = "Sum"
  period      = 300

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.extraction_pipeline.arn
  }

  alarm_actions = var.alert_topic_arn != "" ? [var.alert_topic_arn] : []

  tags = merge(local.common_tags, {
    Name = "EdlPipelineExecutionsThrottled"
  })
}

# ---------------------------------------------------------------------------
# SQS FIFO Queue — Pipeline Trigger Burst Buffer (§1.6)
#
# EventBridge Scheduler fires into this queue instead of directly into
# Step Functions.  A dedicated pipeline_trigger Lambda drains the queue at
# a controlled rate (reserved_concurrency=50), preventing concurrent Lambda
# spikes when many entity schedules fire simultaneously.
#
# FIFO with ContentBasedDeduplication: duplicate fires within 5 minutes for
# the same entity produce only one execution (idempotent schedule delivery).
# Message group ID is set to {source_id}--{entity_id} by EventBridge.
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "pipeline_trigger" {
  name                        = local.pipeline_trigger_queue_name
  fifo_queue                  = true
  content_based_deduplication = true

  # VisibilityTimeout matches Lambda max timeout so a crashed trigger Lambda
  # lets the message reappear for re-processing after 900 s.
  visibility_timeout_seconds = 900

  # 24 h retention — a missed schedule tick is retried within 24 h.
  message_retention_seconds = 86400

  # Encrypt with platform KMS key.
  kms_master_key_id = var.kms_key_arn

  # DLQ for trigger queue — messages that fail repeated trigger attempts land here.
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.pipeline_trigger_dlq.arn
    maxReceiveCount     = 5
  })

  tags = merge(local.common_tags, {
    Name = local.pipeline_trigger_queue_name
  })
}

resource "aws_sqs_queue" "pipeline_trigger_dlq" {
  name                      = "EdlPipelineTriggerDlq.fifo"
  fifo_queue                = true
  message_retention_seconds = 1209600 # 14 days
  kms_master_key_id         = var.kms_key_arn

  tags = merge(local.common_tags, {
    Name = "EdlPipelineTriggerDlq.fifo"
  })
}

# ---------------------------------------------------------------------------
# Pipeline Trigger Lambda — SQS consumer that starts Step Functions executions
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "pipeline_trigger" {
  function_name = local.pipeline_trigger_lambda_name
  description   = "Drains the pipeline trigger FIFO queue and starts Step Functions executions at a controlled rate."

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  handler     = "orchestration.pipeline_trigger.pipeline_trigger_handler.lambda_handler"
  runtime     = "python3.13"
  timeout     = 60 # Short timeout — each invocation processes one message
  memory_size = 256

  # Cap at 50 concurrent executions — prevents burst absorption from
  # translating into a Lambda concurrency spike downstream.
  reserved_concurrent_executions = var.pipeline_trigger_reserved_concurrency

  role = var.pipeline_trigger_role_arn

  environment {
    variables = {
      PLATFORM_ENVIRONMENT = var.environment
      STATE_MACHINE_ARN    = aws_sfn_state_machine.extraction_pipeline.arn
    }
  }

  kms_key_arn = var.kms_key_arn

  tracing_config {
    mode = var.enable_xray_tracing ? "Active" : "PassThrough"
  }

  tags = merge(local.common_tags, {
    Name = local.pipeline_trigger_lambda_name
  })
}

# CloudWatch Log Group for trigger Lambda (pre-created with correct retention)
resource "aws_cloudwatch_log_group" "pipeline_trigger" {
  name              = "/aws/lambda/${local.pipeline_trigger_lambda_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = merge(local.common_tags, {
    Name = "/aws/lambda/${local.pipeline_trigger_lambda_name}"
  })
}

# SQS Event Source Mapping — batch_size=1 ensures clear per-message audit trail
resource "aws_lambda_event_source_mapping" "pipeline_trigger_sqs" {
  event_source_arn = aws_sqs_queue.pipeline_trigger.arn
  function_name    = aws_lambda_function.pipeline_trigger.arn
  batch_size       = 1
  enabled          = true

  # Retry on transient failures; message reappears after VisibilityTimeout.
  function_response_types = ["ReportBatchItemFailures"]
}

# ---------------------------------------------------------------------------
# CloudWatch Alarms — Pipeline Trigger Queue
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "pipeline_trigger_dlq_depth" {
  alarm_name          = "EdlPipelineTriggerDlqMessages"
  alarm_description   = "Pipeline trigger DLQ contains messages. Trigger Lambda is failing to start Step Functions executions."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  statistic   = "Maximum"
  period      = 60

  dimensions = {
    QueueName = aws_sqs_queue.pipeline_trigger_dlq.name
  }

  alarm_actions = var.alert_topic_arn != "" ? [var.alert_topic_arn] : []

  tags = merge(local.common_tags, {
    Name = "EdlPipelineTriggerDlqMessages"
  })
}

# ---------------------------------------------------------------------------
# DLQ Processor Lambda + ESM (§4.4)
# Reads from the extraction failure DLQ, audits, notifies, optionally replays.
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "dlq_processor" {
  function_name = local.dlq_processor_lambda_name
  description   = "Processes extraction failure DLQ messages: writes audit record, sends SNS alert, optionally replays."

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  handler     = "orchestration.dlq_processor.dlq_processor_handler.lambda_handler"
  runtime     = "python3.13"
  timeout     = 60
  memory_size = 256

  # Reserved so a failure flood cannot consume account concurrency and starve the pipeline it is
  # trying to help. Unbounded before 2026-07-29, while the pipeline trigger reserved 50.
  reserved_concurrent_executions = var.dlq_processor_reserved_concurrency

  role = var.dlq_processor_role_arn

  environment {
    variables = {
      PLATFORM_ENVIRONMENT = var.environment
      RUN_AUDIT_LOG_TABLE  = var.run_audit_log_table_name
      ALERT_SNS_TOPIC_ARN  = var.alert_topic_arn
      STATE_MACHINE_ARN    = aws_sfn_state_machine.extraction_pipeline.arn
      AUTO_REPLAY          = "false"
    }
  }

  kms_key_arn = var.kms_key_arn

  tracing_config {
    mode = var.enable_xray_tracing ? "Active" : "PassThrough"
  }

  tags = merge(local.common_tags, {
    Name = local.dlq_processor_lambda_name
  })
}

resource "aws_cloudwatch_log_group" "dlq_processor" {
  name              = "/aws/lambda/${local.dlq_processor_lambda_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = merge(local.common_tags, {
    Name = "/aws/lambda/${local.dlq_processor_lambda_name}"
  })
}

# Per-stage mappings (gap item 21). The processor bound only to the single legacy queue, so the nine
# per-stage queues had no consumer as well as no producer — `maxReceiveCount = 3` never counted,
# because it only decrements on *receive*. One mapping per stage rather than one shared queue keeps
# each stage's visibility timeout matched to its own Lambda timeout, which is what
# CreateEventSourceMapping validates against (see infrastructure/CLAUDE.md).
resource "aws_lambda_event_source_mapping" "dlq_processor_stage_queues" {
  for_each = aws_sqs_queue.stage_dlq

  event_source_arn = each.value.arn
  function_name    = aws_lambda_function.dlq_processor.arn
  batch_size       = var.dlq_processor_batch_size
  enabled          = true

  maximum_batching_window_in_seconds = 20
  function_response_types            = ["ReportBatchItemFailures"]
}

resource "aws_lambda_event_source_mapping" "dlq_processor_sqs" {
  event_source_arn = var.extraction_failure_dlq_arn
  function_name    = aws_lambda_function.dlq_processor.arn
  batch_size       = var.dlq_processor_batch_size
  enabled          = true

  # A short window so a batch fills before invoking, without adding meaningful latency to a
  # message that is already a recorded failure.
  maximum_batching_window_in_seconds = 20

  # Partial-batch failure: only the failed messages are re-driven, which is what makes a batch
  # size above 1 safe here.
  function_response_types = ["ReportBatchItemFailures"]
}
