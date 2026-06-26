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

  # Step Functions state machine name
  state_machine_name = "${var.environment}-extraction-pipeline"

  # EventBridge Scheduler schedule group name
  schedule_group_name = "${var.environment}-extraction-schedules"

  # CloudWatch log group for Step Functions execution history
  sfn_log_group_name = "/edl/${var.environment}/step-functions/extraction-pipeline"
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
  policy_name = "${local.state_machine_name}-log-delivery"
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
  name     = local.state_machine_name
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
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "TransientExtractionError"]
            IntervalSeconds = 10
            MaxAttempts     = 3
            BackoffRate     = 2.0
            JitterStrategy  = "FULL"
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "ExtractionFailed"
            ResultPath  = "$.error"
          }
        ]
        Next = "CheckTransformationBlocked"
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
        Type  = "Succeed"
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
          "source_id.$"      = "$.source_id"
          "entity_id.$"      = "$.entity_id"
          "environment.$"    = "$.environment"
          "run_id.$"         = "$.extraction.run_id"
          "raw_s3_prefix.$"  = "$.extraction.raw_s3_prefix"
          "mapping_version"  = "latest"
        }
        ResultPath = "$.transformation"
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "TransientTransformationError"]
            IntervalSeconds = 15
            MaxAttempts     = 2
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

      # Guard: blocking quality violation stops entity resolution and downstream.
      CheckPublicationBlocked = {
        Type = "Choice"
        Choices = [
          {
            Variable      = "$.transformation.is_publication_blocked"
            BooleanEquals = true
            Next          = "TransformationCompletePublicationBlocked"
          }
        ]
        Default = "RunEntityResolution"
      }

      # Terminal: transformation succeeded but quality gate blocked publication.
      TransformationCompletePublicationBlocked = {
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
        }
        ResultPath = "$.entity_resolution"
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "TransientResolutionError"]
            IntervalSeconds = 20
            MaxAttempts     = 2
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
          "source_id.$"              = "$.source_id"
          "entity_id.$"              = "$.entity_id"
          "environment.$"            = "$.environment"
          "run_id.$"                 = "$.extraction.run_id"
          "canonical_prefix.$"           = "$.entity_resolution.canonical_prefix"
          "curated_s3_prefix.$"      = "$.transformation.curated_s3_prefix"
        }
        ResultPath = "$.analytics"
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "TransientPublishError"]
            IntervalSeconds = 15
            MaxAttempts     = 2
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
        Next = "LoadServingStore"
      }

      # ── Stage E: Serving Store Load ─────────────────────────────────────────
      # Reads analytics Parquet, retrieves DB credentials from Secrets Manager,
      # performs idempotent REPLACE INTO upsert into MySQL RDS serving tables.
      LoadServingStore = {
        Type     = "Task"
        Resource = var.serving_store_loader_lambda_arn
        Parameters = {
          "source_id.$"              = "$.source_id"
          "entity_id.$"              = "$.entity_id"
          "environment.$"            = "$.environment"
          "run_id.$"                 = "$.extraction.run_id"
          "analytics_s3_prefix.$"   = "$.analytics.analytics_s3_prefix"
        }
        ResultPath = "$.serving"
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "TransientServingError"]
            IntervalSeconds = 20
            MaxAttempts     = 2
            BackoffRate     = 2.0
            JitterStrategy  = "FULL"
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "ServingStoreFailed"
            ResultPath  = "$.error"
          }
        ]
        End = true
      }

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

      ServingStoreFailed = {
        Type  = "Fail"
        Error = "ServingStoreFailed"
        Cause = "Serving store load failed after all retry attempts. See DLQ and CloudWatch Logs."
      }
    }
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn_execution.arn}:*"
    include_execution_data = false # Execution input/output excluded — may contain source metadata
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
  alarm_name          = "${var.environment}-edl-pipeline-execution-failures"
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
    Name = "${var.environment}-edl-pipeline-execution-failures"
  })
}

# ---------------------------------------------------------------------------
# CloudWatch Metric Alarm — throttled executions
#
# Express Workflows have concurrency limits; throttled executions indicate
# the platform needs provisioned concurrency or rate-limit adjustment.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "sfn_executions_throttled" {
  alarm_name          = "${var.environment}-edl-pipeline-executions-throttled"
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
    Name = "${var.environment}-edl-pipeline-executions-throttled"
  })
}
