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

  log_groups = {
    "connector-runtime"    = var.log_retention_days
    "transformation"       = var.log_retention_days
    "entity-resolution"    = var.log_retention_days
    "analytics-publisher"  = var.log_retention_days
    "serving-store-loader" = var.log_retention_days
    "orchestration"        = var.log_retention_days
    "schema-drift"         = var.log_retention_days
  }
}


resource "aws_cloudwatch_log_group" "platform_services" {
  for_each = local.log_groups

  name              = "/${var.name_prefix}/${each.key}-${var.environment}"
  retention_in_days = each.value
  kms_key_id        = var.logs_kms_key_arn

  tags = merge(local.common_tags, {
    Name    = "/${var.name_prefix}/${each.key}-${var.environment}"
    Service = each.key
  })
}


resource "aws_sns_topic" "platform_alerts" {
  name              = "${var.name_prefix}-platform-alerts-${var.environment}"
  kms_master_key_id = var.logs_kms_key_arn # Reuse log KMS key (allows SNS encryption)

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-platform-alerts-${var.environment}"
  })
}

resource "aws_sns_topic_subscription" "ops_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.platform_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}


resource "aws_cloudwatch_metric_alarm" "extraction_failures" {
  alarm_name          = "${var.name_prefix}-extraction-failures-${var.environment}"
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

resource "aws_cloudwatch_metric_alarm" "schema_drift_breaking" {
  alarm_name          = "${var.name_prefix}-schema-drift-breaking-${var.environment}"
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

resource "aws_cloudwatch_metric_alarm" "watermark_lag_slo_breach" {
  alarm_name          = "${var.name_prefix}-watermark-lag-slo-breach-${var.environment}"
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

resource "aws_cloudwatch_metric_alarm" "extraction_activity_absent" {
  alarm_name          = "${var.name_prefix}-extraction-activity-absent-${var.environment}"
  alarm_description   = "No extraction records have been emitted in the monitoring window. Pipeline may have stopped running silently."
  comparison_operator = "LessThanOrEqualToThreshold"
  evaluation_periods  = var.extraction_absence_evaluation_periods
  metric_name         = "RecordsExtracted"
  namespace           = "EnterpriseDatalake"
  period              = var.extraction_absence_period_seconds
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.platform_alerts.arn]
  ok_actions    = [aws_sns_topic.platform_alerts.arn]

  tags = local.common_tags
}


resource "aws_xray_group" "platform" {
  group_name        = "${var.name_prefix}-platform-${var.environment}"
  filter_expression = "annotation.platform_env = \"${var.environment}\""

  insights_configuration {
    insights_enabled      = true
    notifications_enabled = true
  }

  tags = local.common_tags
}


resource "aws_cloudwatch_metric_alarm" "transformation_quality_blocked" {
  alarm_name          = "${var.name_prefix}-transformation-quality-blocked-${var.environment}"
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
  alarm_name          = "${var.name_prefix}-serving-store-load-failures-${var.environment}"
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


resource "aws_cloudwatch_dashboard" "extraction_slo" {
  dashboard_name = "${var.name_prefix}-extraction-slo-${var.environment}"
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
  dashboard_name = "${var.name_prefix}-transformation-slo-${var.environment}"
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
  dashboard_name = "${var.name_prefix}-serving-slo-${var.environment}"
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
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "Serving Store Records Skipped (unchanged since last run)"
          region  = data.aws_region.current.name
          period  = 300
          stat    = "Sum"
          metrics = [["EnterpriseDatalake", "RecordsSkipped", "Stage", "serving_store_load"]]
        }
      },
      {
        type   = "alarm"
        x      = 0
        y      = 12
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


resource "aws_cloudwatch_metric_alarm" "extraction_failure_dlq_depth" {
  alarm_name          = "${var.name_prefix}-dlq-messages-present-${var.environment}"
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


locals {
  monitored_lambdas = {
    for k, v in {
      "extraction"          = var.extraction_lambda_name
      "transformation"      = var.transformation_lambda_name
      "entity-resolution"   = var.entity_resolution_lambda_name
      "analytics-publisher" = var.analytics_publisher_lambda_name
    } : k => v if v != ""
  }

  lambda_timeout_alert_ms = 900 * 1000 * 0.85
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = local.monitored_lambdas

  alarm_name          = "${var.name_prefix}-${each.key}-errors-${var.environment}"
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

  alarm_name          = "${var.name_prefix}-${each.key}-duration-high-${var.environment}"
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

  alarm_name          = "${var.name_prefix}-${each.key}-throttles-${var.environment}"
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


resource "aws_cloudwatch_log_metric_filter" "cb_ddb_fallback" {
  name           = "${var.name_prefix}-cb-ddb-fallback-${var.environment}"
  pattern        = "{ $.event = \"circuit_breaker_ddb_init_failed\" }"
  log_group_name = "/${var.name_prefix}/connector-runtime-${var.environment}"

  metric_transformation {
    name      = "CircuitBreakerDDBFallback"
    namespace = "EnterpriseDatalake"
    value     = "1"
  }

  depends_on = [aws_cloudwatch_log_group.platform_services]
}

resource "aws_cloudwatch_metric_alarm" "cb_ddb_fallback" {
  alarm_name          = "${var.name_prefix}-circuit-breaker-ddb-fallback-${var.environment}"
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


resource "aws_cloudwatch_log_metric_filter" "input_validation_failures" {
  name           = "${var.name_prefix}-input-validation-failures-${var.environment}"
  pattern        = "{ $.level = \"error\" && $.event = \"input_validation_failed\" }"
  log_group_name = "/${var.name_prefix}/connector-runtime-${var.environment}"

  metric_transformation {
    name      = "InputValidationFailures"
    namespace = "EnterpriseDatalake"
    value     = "1"
  }

  depends_on = [aws_cloudwatch_log_group.platform_services]
}

resource "aws_cloudwatch_metric_alarm" "input_validation_failures" {
  alarm_name          = "${var.name_prefix}-input-validation-failures-${var.environment}"
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
  name           = "${var.name_prefix}-credential-retrieval-failures-${var.environment}"
  pattern        = "{ $.event = \"credential_retrieval_failed\" }"
  log_group_name = "/${var.name_prefix}/connector-runtime-${var.environment}"

  metric_transformation {
    name      = "CredentialRetrievalFailures"
    namespace = "EnterpriseDatalake"
    value     = "1"
  }

  depends_on = [aws_cloudwatch_log_group.platform_services]
}

resource "aws_cloudwatch_metric_alarm" "credential_retrieval_failures" {
  alarm_name          = "${var.name_prefix}-credential-retrieval-failures-${var.environment}"
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
  name           = "${var.name_prefix}-circuit-breaker-opened-${var.environment}"
  pattern        = "{ $.event = \"circuit_breaker_opened\" }"
  log_group_name = "/${var.name_prefix}/connector-runtime-${var.environment}"

  metric_transformation {
    name      = "CircuitBreakerOpened"
    namespace = "EnterpriseDatalake"
    value     = "1"
  }

  depends_on = [aws_cloudwatch_log_group.platform_services]
}

resource "aws_cloudwatch_metric_alarm" "circuit_breaker_opened" {
  alarm_name          = "${var.name_prefix}-circuit-breaker-opened-${var.environment}"
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


resource "aws_cloudwatch_query_definition" "failed_runs_last_24h" {
  name = "${var.name_prefix}/failed-runs-last-24h-${var.environment}"
  log_group_names = [
    "/${var.name_prefix}/connector-runtime-${var.environment}",
    "/${var.name_prefix}/transformation-${var.environment}",
    "/${var.name_prefix}/entity-resolution-${var.environment}",
    "/${var.name_prefix}/analytics-publisher-${var.environment}",
  ]
  query_string = <<-EOT
    fields run_id, source_id, entity_id, @timestamp
    | filter level = "error"
    | sort @timestamp desc
    | limit 50
  EOT
}

resource "aws_cloudwatch_query_definition" "mapping_failures_by_entity" {
  name            = "${var.name_prefix}/mapping-failures-by-entity-${var.environment}"
  log_group_names = ["/${var.name_prefix}/transformation-${var.environment}"]
  query_string    = <<-EOT
    fields entity_id, @timestamp
    | filter event = "mapping_failure"
    | stats count() as failure_count by entity_id
    | sort failure_count desc
    | limit 20
  EOT
}

resource "aws_cloudwatch_query_definition" "schema_drift_events" {
  name            = "${var.name_prefix}/schema-drift-events-${var.environment}"
  log_group_names = ["/${var.name_prefix}/connector-runtime-${var.environment}"]
  query_string    = <<-EOT
    fields source_id, entity_id, drift_classification, @timestamp
    | filter event = "schema_drift_detected"
    | sort @timestamp desc
    | limit 50
  EOT
}

resource "aws_cloudwatch_query_definition" "watermark_lag_by_source" {
  name            = "${var.name_prefix}/watermark-lag-by-source-${var.environment}"
  log_group_names = ["/${var.name_prefix}/connector-runtime-${var.environment}"]
  query_string    = <<-EOT
    fields source_id, entity_id, watermark_lag_seconds, @timestamp
    | filter event = "watermark_updated"
    | stats max(watermark_lag_seconds) as max_lag_seconds by source_id, entity_id
    | sort max_lag_seconds desc
  EOT
}

resource "aws_cloudwatch_query_definition" "circuit_breaker_history" {
  name            = "${var.name_prefix}/circuit-breaker-events-${var.environment}"
  log_group_names = ["/${var.name_prefix}/connector-runtime-${var.environment}"]
  query_string    = <<-EOT
    fields source_id, entity_id, event, @timestamp
    | filter event in ["circuit_breaker_opened", "circuit_breaker_reset", "circuit_breaker_ddb_init_failed"]
    | sort @timestamp desc
    | limit 50
  EOT
}

resource "aws_cloudwatch_query_definition" "dlq_enqueue_history" {
  name = "${var.name_prefix}/dlq-enqueue-history-${var.environment}"
  log_group_names = [
    "/${var.name_prefix}/connector-runtime-${var.environment}",
    "/${var.name_prefix}/orchestration-${var.environment}",
  ]
  query_string = <<-EOT
    fields run_id, source_id, entity_id, failure_reason, @timestamp
    | filter event = "dlq_message_enqueued"
    | sort @timestamp desc
    | limit 100
  EOT
}

resource "aws_cloudwatch_query_definition" "cold_start_duration" {
  name = "${var.name_prefix}/cold-start-duration-${var.environment}"
  log_group_names = [
    "/aws/lambda/${var.name_prefix}-extraction-${var.environment}",
    "/aws/lambda/${var.name_prefix}-transformation-${var.environment}",
    "/aws/lambda/${var.name_prefix}-entity-resolution-${var.environment}",
    "/aws/lambda/${var.name_prefix}-analytics-publisher-${var.environment}",
  ]
  query_string = <<-EOT
    filter @type = "REPORT"
    | fields @initDuration, @duration, @memorySize, @maxMemoryUsed, @requestId
    | sort @timestamp desc
    | limit 50
  EOT
}


resource "aws_sns_topic_subscription" "pagerduty" {
  count                  = var.pagerduty_integration_url != "" ? 1 : 0
  topic_arn              = aws_sns_topic.platform_alerts.arn
  protocol               = "https"
  endpoint               = var.pagerduty_integration_url
  endpoint_auto_confirms = true
}
