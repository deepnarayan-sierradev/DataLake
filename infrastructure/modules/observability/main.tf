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
  treat_missing_data  = "breaching"

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
          title  = "Records Extracted"
          region = data.aws_region.current.name
          period = 300
          stat   = "Sum"
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
          title  = "Records Failed"
          region = data.aws_region.current.name
          period = 300
          stat   = "Sum"
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
          title  = "Schema Drift Events"
          region = data.aws_region.current.name
          period = 300
          stat   = "Sum"
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
          title  = "Watermark Lag (seconds)"
          region = data.aws_region.current.name
          period = 300
          stat   = "Maximum"
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
          title  = "Retry Count"
          region = data.aws_region.current.name
          period = 300
          stat   = "Sum"
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
          title  = "Active SLO Alarms"
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

