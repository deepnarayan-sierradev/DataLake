terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_region" "current" {}

locals {
  common_tags = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "observability"
  })

  # Service log group definitions: name → retention
  log_groups = {
    "connector-runtime"   = var.log_retention_days
    "transformation"      = var.log_retention_days
    "entity-resolution"   = var.log_retention_days
    "analytics-publisher" = var.log_retention_days
    "orchestration"       = var.log_retention_days
    "schema-drift"        = var.log_retention_days
  }
}

# ---------------------------------------------------------------------------
# CloudWatch Log Groups — one per platform service
# All encrypted with the platform KMS key.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "platform_services" {
  for_each = local.log_groups

  name              = "/edl/${var.environment}/${each.key}"
  retention_in_days = each.value
  kms_key_id        = var.logs_kms_key_arn

  tags = merge(local.common_tags, {
    Name    = "/edl/${var.environment}/${each.key}"
    Service = each.key
  })
}

# ---------------------------------------------------------------------------
# SNS Alert Topic — receives CloudWatch alarm notifications
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "platform_alerts" {
  name              = "${var.environment}-edl-platform-alerts"
  kms_master_key_id = var.logs_kms_key_arn # Reuse log KMS key (allows SNS encryption)

  tags = merge(local.common_tags, {
    Name = "${var.environment}-edl-platform-alerts"
  })
}

resource "aws_sns_topic_subscription" "ops_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.platform_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------------------
# CloudWatch Alarms — SLO-bound alerting
# ---------------------------------------------------------------------------

# Alarm: extraction failure rate > 0 (any failed run triggers alert)
resource "aws_cloudwatch_metric_alarm" "extraction_failures" {
  alarm_name          = "${var.environment}-edl-extraction-failures"
  alarm_description   = "One or more extraction runs have failed. Investigate run audit log and DLQ."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "RecordsFailed"
  namespace           = "EnterpriseDatalake"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.platform_alerts.arn]
  ok_actions    = [aws_sns_topic.platform_alerts.arn]

  tags = local.common_tags
}

# Alarm: schema drift breaking changes detected
resource "aws_cloudwatch_metric_alarm" "schema_drift_breaking" {
  alarm_name          = "${var.environment}-edl-schema-drift-breaking-detected"
  alarm_description   = "Breaking schema drift detected. Downstream transformation may need updating."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "SchemaDriftCount"
  namespace           = "EnterpriseDatalake"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.platform_alerts.arn]

  tags = local.common_tags
}

# Alarm: watermark lag exceeds SLO threshold (data freshness alert)
resource "aws_cloudwatch_metric_alarm" "watermark_lag_slo_breach" {
  alarm_name          = "${var.environment}-edl-watermark-lag-slo-breach"
  alarm_description   = "Watermark lag exceeds SLO threshold. Data freshness degraded."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "WatermarkLagSeconds"
  namespace           = "EnterpriseDatalake"
  period              = 300
  statistic           = "Maximum"
  threshold           = var.watermark_lag_slo_seconds
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.platform_alerts.arn]

  tags = local.common_tags
}

# Alarm: no extraction records emitted for a full monitoring window.
# This detects silent failures where the pipeline stops running without
# producing explicit errors — e.g. scheduler misconfiguration, Step Functions
# execution not triggered, or IAM permission silently blocking starts.
resource "aws_cloudwatch_metric_alarm" "extraction_activity_absent" {
  alarm_name          = "${var.environment}-edl-extraction-activity-absent"
  alarm_description   = "No extraction records have been emitted in the monitoring window. Pipeline may have stopped running silently."
  comparison_operator = "LessThanOrEqualToThreshold"
  evaluation_periods  = var.extraction_absence_evaluation_periods
  metric_name         = "RecordsExtracted"
  namespace           = "EnterpriseDatalake"
  period              = var.extraction_absence_period_seconds
  statistic           = "Sum"
  threshold           = 0
  # BREACHING on missing data: absence of metric is itself the alert condition.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.platform_alerts.arn]
  ok_actions    = [aws_sns_topic.platform_alerts.arn]

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# X-Ray Tracing Group — groups traces by platform service
# ---------------------------------------------------------------------------

resource "aws_xray_group" "platform" {
  group_name        = "${var.environment}-edl-platform"
  filter_expression = "annotation.platform_env = \"${var.environment}\""

  insights_configuration {
    insights_enabled      = true
    notifications_enabled = true
  }

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# CloudWatch Alarms — transformation, entity-resolution, and serving tiers
# (spec §10.3: dashboards per source covering all pipeline stages)
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "transformation_quality_blocked" {
  alarm_name          = "${var.environment}-edl-transformation-quality-blocked"
  alarm_description   = "Quality policy blocking violations detected. Curated publication halted pending review."
  namespace           = "EnterpriseDatalake"
  metric_name         = "RecordsFailed"
  dimensions          = { Stage = "transformation" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.platform_alerts.arn]
  ok_actions          = [aws_sns_topic.platform_alerts.arn]
  tags                = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "serving_store_load_failures" {
  alarm_name          = "${var.environment}-edl-serving-store-load-failures"
  alarm_description   = "Serving store load errors detected. Target database records may be stale."
  namespace           = "EnterpriseDatalake"
  metric_name         = "RecordsFailed"
  dimensions          = { Stage = "serving_store_load" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.platform_alerts.arn]
  ok_actions          = [aws_sns_topic.platform_alerts.arn]
  tags                = local.common_tags
}

# ---------------------------------------------------------------------------
# CloudWatch Dashboards — SLO dashboard per pipeline tier (spec §10.3 AC)
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_dashboard" "extraction_slo" {
  dashboard_name = "${var.environment}-edl-extraction-slo"
  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Records Extracted"
          region  = data.aws_region.current.name
          period  = 300
          stat    = "Sum"
          metrics = [["EnterpriseDatalake", "RecordsExtracted"]]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Records Failed"
          region  = data.aws_region.current.name
          period  = 300
          stat    = "Sum"
          metrics = [["EnterpriseDatalake", "RecordsFailed"]]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "Schema Drift Events"
          region  = data.aws_region.current.name
          period  = 300
          stat    = "Sum"
          metrics = [["EnterpriseDatalake", "SchemaDriftCount"]]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "Watermark Lag (seconds)"
          region  = data.aws_region.current.name
          period  = 300
          stat    = "Maximum"
          metrics = [["EnterpriseDatalake", "WatermarkLagSeconds"]]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title   = "Retry Count"
          region  = data.aws_region.current.name
          period  = 300
          stat    = "Sum"
          metrics = [["EnterpriseDatalake", "RetryCount"]]
        }
      },
      {
        type   = "alarm"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title = "Active SLO Alarms"
          alarms = [
            aws_cloudwatch_metric_alarm.extraction_failures.arn,
            aws_cloudwatch_metric_alarm.schema_drift_breaking.arn,
            aws_cloudwatch_metric_alarm.watermark_lag_slo_breach.arn,
            aws_cloudwatch_metric_alarm.extraction_activity_absent.arn,
          ]
        }
      }
    ]
  })
}

resource "aws_cloudwatch_dashboard" "transformation_slo" {
  dashboard_name = "${var.environment}-edl-transformation-slo"
  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Canonical Records Produced"
          region  = data.aws_region.current.name
          period  = 300
          stat    = "Sum"
          metrics = [["EnterpriseDatalake", "RecordsExtracted", "Stage", "transformation"]]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Mapping Failures + Quality Blocking"
          region  = data.aws_region.current.name
          period  = 300
          stat    = "Sum"
          metrics = [["EnterpriseDatalake", "RecordsFailed", "Stage", "transformation"]]
        }
      },
      {
        type   = "alarm"
        x      = 0
        y      = 6
        width  = 24
        height = 6
        properties = {
          title  = "Transformation Alarms"
          alarms = [aws_cloudwatch_metric_alarm.transformation_quality_blocked.arn]
        }
      }
    ]
  })
}

resource "aws_cloudwatch_dashboard" "serving_slo" {
  dashboard_name = "${var.environment}-edl-serving-slo"
  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Serving Store Records Loaded"
          region  = data.aws_region.current.name
          period  = 300
          stat    = "Sum"
          metrics = [["EnterpriseDatalake", "RecordsExtracted", "Stage", "serving_store_load"]]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Serving Store Load Failures"
          region  = data.aws_region.current.name
          period  = 300
          stat    = "Sum"
          metrics = [["EnterpriseDatalake", "RecordsFailed", "Stage", "serving_store_load"]]
        }
      },
      {
        type   = "alarm"
        x      = 0
        y      = 6
        width  = 24
        height = 6
        properties = {
          title  = "Serving Store Alarms"
          alarms = [aws_cloudwatch_metric_alarm.serving_store_load_failures.arn]
        }
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# DLQ Depth Alarm (§5.3)
# Fires as soon as any message lands in the extraction failure DLQ.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "extraction_failure_dlq_depth" {
  alarm_name          = "${var.environment}-edl-dlq-messages-present"
  alarm_description   = "Extraction failure DLQ contains unprocessed messages. Investigate failed runs immediately."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  statistic   = "Maximum"
  period      = 60

  dimensions = {
    QueueName = var.extraction_failure_dlq_name
  }

  alarm_actions = [aws_sns_topic.platform_alerts.arn]

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Lambda Error / Duration / Throttle Alarms (§5.4)
# Three alarms per Lambda function: errors, duration at 85% timeout, throttles.
# ---------------------------------------------------------------------------

locals {
  # Lambda functions to monitor. Key = display name, value = function name.
  monitored_lambdas = {
    for k, v in {
      extraction          = var.extraction_lambda_name
      transformation      = var.transformation_lambda_name
      entity_resolution   = var.entity_resolution_lambda_name
      analytics_publisher = var.analytics_publisher_lambda_name
    } : k => v if v != ""
  }

  # 85% of 900s Lambda max timeout in milliseconds
  lambda_timeout_alert_ms = 900 * 1000 * 0.85
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = local.monitored_lambdas

  alarm_name          = "${var.environment}-edl-${each.key}-errors"
  alarm_description   = "Lambda ${each.value} reported execution errors. Check CloudWatch Logs."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  statistic   = "Sum"
  period      = 300

  dimensions = {
    FunctionName = each.value
  }

  alarm_actions = [aws_sns_topic.platform_alerts.arn]
  tags          = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "lambda_duration" {
  for_each = local.monitored_lambdas

  alarm_name          = "${var.environment}-edl-${each.key}-duration-high"
  alarm_description   = "Lambda ${each.value} approaching timeout (>85% of 900s). Risk of incomplete pipeline run."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = local.lambda_timeout_alert_ms
  treat_missing_data  = "notBreaching"

  namespace   = "AWS/Lambda"
  metric_name = "Duration"
  statistic   = "Maximum"
  period      = 300

  dimensions = {
    FunctionName = each.value
  }

  alarm_actions = [aws_sns_topic.platform_alerts.arn]
  tags          = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  for_each = local.monitored_lambdas

  alarm_name          = "${var.environment}-edl-${each.key}-throttles"
  alarm_description   = "Lambda ${each.value} is being throttled. Increase reserved concurrency or check limits."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"

  namespace   = "AWS/Lambda"
  metric_name = "Throttles"
  statistic   = "Sum"
  period      = 300

  dimensions = {
    FunctionName = each.value
  }

  alarm_actions = [aws_sns_topic.platform_alerts.arn]
  tags          = local.common_tags
}

# ---------------------------------------------------------------------------
# CloudWatch Metric Filter — Circuit Breaker DDB Fallback (§4.2)
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "cb_ddb_fallback" {
  name           = "${var.environment}-cb-ddb-fallback"
  pattern        = "{ $.event = \"circuit_breaker_ddb_init_failed\" }"
  log_group_name = "/edl/${var.environment}/connector-runtime"

  metric_transformation {
    name      = "CircuitBreakerDDBFallback"
    namespace = "EnterpriseDatalake"
    value     = "1"
  }

  depends_on = [aws_cloudwatch_log_group.platform_services]
}

resource "aws_cloudwatch_metric_alarm" "cb_ddb_fallback" {
  alarm_name          = "${var.environment}-edl-circuit-breaker-ddb-fallback"
  alarm_description   = "Circuit breaker fell back to in-process state. Distributed protection disabled — check VPC routing."
  namespace           = "EnterpriseDatalake"
  metric_name         = "CircuitBreakerDDBFallback"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.platform_alerts.arn]
  tags                = local.common_tags
}

# ---------------------------------------------------------------------------
# Security Event Metric Filters + Alarms (§4.5)
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "input_validation_failures" {
  name           = "${var.environment}-input-validation-failures"
  pattern        = "{ $.level = \"error\" && $.event = \"input_validation_failed\" }"
  log_group_name = "/edl/${var.environment}/connector-runtime"

  metric_transformation {
    name      = "InputValidationFailures"
    namespace = "EnterpriseDatalake"
    value     = "1"
  }

  depends_on = [aws_cloudwatch_log_group.platform_services]
}

resource "aws_cloudwatch_metric_alarm" "input_validation_failures" {
  alarm_name          = "${var.environment}-edl-input-validation-failures"
  alarm_description   = "Repeated input validation failures. Possible injection probing or misconfigured client."
  namespace           = "EnterpriseDatalake"
  metric_name         = "InputValidationFailures"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.platform_alerts.arn]
  tags                = local.common_tags
}

resource "aws_cloudwatch_log_metric_filter" "credential_retrieval_failures" {
  name           = "${var.environment}-credential-retrieval-failures"
  pattern        = "{ $.event = \"credential_retrieval_failed\" }"
  log_group_name = "/edl/${var.environment}/connector-runtime"

  metric_transformation {
    name      = "CredentialRetrievalFailures"
    namespace = "EnterpriseDatalake"
    value     = "1"
  }

  depends_on = [aws_cloudwatch_log_group.platform_services]
}

resource "aws_cloudwatch_metric_alarm" "credential_retrieval_failures" {
  alarm_name          = "${var.environment}-edl-credential-retrieval-failures"
  alarm_description   = "Credential retrieval from Secrets Manager failed. Check rotation or access policies."
  namespace           = "EnterpriseDatalake"
  metric_name         = "CredentialRetrievalFailures"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.platform_alerts.arn]
  tags                = local.common_tags
}

resource "aws_cloudwatch_log_metric_filter" "circuit_breaker_opened" {
  name           = "${var.environment}-circuit-breaker-opened"
  pattern        = "{ $.event = \"circuit_breaker_opened\" }"
  log_group_name = "/edl/${var.environment}/connector-runtime"

  metric_transformation {
    name      = "CircuitBreakerOpened"
    namespace = "EnterpriseDatalake"
    value     = "1"
  }

  depends_on = [aws_cloudwatch_log_group.platform_services]
}

resource "aws_cloudwatch_metric_alarm" "circuit_breaker_opened" {
  alarm_name          = "${var.environment}-edl-circuit-breaker-opened"
  alarm_description   = "Extraction circuit breaker opened. Source is unavailable or returning persistent errors."
  namespace           = "EnterpriseDatalake"
  metric_name         = "CircuitBreakerOpened"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.platform_alerts.arn]
  tags                = local.common_tags
}

# ---------------------------------------------------------------------------
# CloudWatch Logs Insights Saved Queries (§5.6)
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_query_definition" "failed_runs_last_24h" {
  name = "${var.environment}/edl/failed-runs-last-24h"
  log_group_names = [
    "/edl/${var.environment}/connector-runtime",
    "/edl/${var.environment}/transformation",
    "/edl/${var.environment}/entity-resolution",
    "/edl/${var.environment}/analytics-publisher",
  ]
  query_string = <<-EOT
    fields run_id, source_id, entity_id, @timestamp
    | filter level = "error"
    | sort @timestamp desc
    | limit 50
  EOT
}

resource "aws_cloudwatch_query_definition" "mapping_failures_by_entity" {
  name            = "${var.environment}/edl/mapping-failures-by-entity"
  log_group_names = ["/edl/${var.environment}/transformation"]
  query_string    = <<-EOT
    fields entity_id, @timestamp
    | filter event = "mapping_failure"
    | stats count() as failure_count by entity_id
    | sort failure_count desc
    | limit 20
  EOT
}

resource "aws_cloudwatch_query_definition" "schema_drift_events" {
  name            = "${var.environment}/edl/schema-drift-events"
  log_group_names = ["/edl/${var.environment}/connector-runtime"]
  query_string    = <<-EOT
    fields source_id, entity_id, drift_classification, @timestamp
    | filter event = "schema_drift_detected"
    | sort @timestamp desc
    | limit 50
  EOT
}

resource "aws_cloudwatch_query_definition" "watermark_lag_by_source" {
  name            = "${var.environment}/edl/watermark-lag-by-source"
  log_group_names = ["/edl/${var.environment}/connector-runtime"]
  query_string    = <<-EOT
    fields source_id, entity_id, watermark_lag_seconds, @timestamp
    | filter event = "watermark_updated"
    | stats max(watermark_lag_seconds) as max_lag_seconds by source_id, entity_id
    | sort max_lag_seconds desc
  EOT
}

resource "aws_cloudwatch_query_definition" "circuit_breaker_history" {
  name            = "${var.environment}/edl/circuit-breaker-events"
  log_group_names = ["/edl/${var.environment}/connector-runtime"]
  query_string    = <<-EOT
    fields source_id, entity_id, event, @timestamp
    | filter event in ["circuit_breaker_opened", "circuit_breaker_reset", "circuit_breaker_ddb_init_failed"]
    | sort @timestamp desc
    | limit 50
  EOT
}

resource "aws_cloudwatch_query_definition" "dlq_enqueue_history" {
  name = "${var.environment}/edl/dlq-enqueue-history"
  log_group_names = [
    "/edl/${var.environment}/connector-runtime",
    "/edl/${var.environment}/orchestration",
  ]
  query_string = <<-EOT
    fields run_id, source_id, entity_id, failure_reason, @timestamp
    | filter event = "dlq_message_enqueued"
    | sort @timestamp desc
    | limit 100
  EOT
}

resource "aws_cloudwatch_query_definition" "cold_start_duration" {
  name = "${var.environment}/edl/cold-start-duration"
  log_group_names = [
    "/aws/lambda/${var.environment}-extraction-pipeline",
    "/aws/lambda/${var.environment}-transformation-pipeline",
    "/aws/lambda/${var.environment}-entity-resolution-pipeline",
    "/aws/lambda/${var.environment}-analytics-layer-publisher",
  ]
  query_string = <<-EOT
    filter @type = "REPORT"
    | fields @initDuration, @duration, @memorySize, @maxMemoryUsed, @requestId
    | sort @timestamp desc
    | limit 50
  EOT
}

# ---------------------------------------------------------------------------
# PagerDuty / OpsGenie Integration (§5.8) — conditional on URL being set
# ---------------------------------------------------------------------------

resource "aws_sns_topic_subscription" "pagerduty" {
  count                  = var.pagerduty_integration_url != "" ? 1 : 0
  topic_arn              = aws_sns_topic.platform_alerts.arn
  protocol               = "https"
  endpoint               = var.pagerduty_integration_url
  endpoint_auto_confirms = true
}
