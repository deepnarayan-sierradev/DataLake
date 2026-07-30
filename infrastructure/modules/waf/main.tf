
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

locals {
  common_tags = merge(var.tags, {
    Module    = "waf"
    Component = "control-plane-perimeter"
  })

  managed_rule_groups = [
    { name = "AWSManagedRulesCommonRuleSet", priority = 10 },
    { name = "AWSManagedRulesKnownBadInputsRuleSet", priority = 20 },
    { name = "AWSManagedRulesSQLiRuleSet", priority = 30 },
    { name = "AWSManagedRulesAmazonIpReputationList", priority = 40 },
  ]
}

resource "aws_wafv2_web_acl" "control_plane" {
  name        = "${var.name_prefix}-control-plane-waf-${var.environment}"
  description = "Control-plane WAF: managed rules plus per-IP and per-tenant rate limiting."
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  dynamic "rule" {
    for_each = local.managed_rule_groups
    content {
      name     = rule.value.name
      priority = rule.value.priority

      dynamic "override_action" {
        for_each = var.enforcement_mode == "audit" ? [1] : []
        content {
          count {}
        }
      }

      dynamic "override_action" {
        for_each = var.enforcement_mode == "enforce" ? [1] : []
        content {
          none {}
        }
      }

      statement {
        managed_rule_group_statement {
          vendor_name = "AWS"
          name        = rule.value.name
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = replace(rule.value.name, "AWSManagedRules", "")
        sampled_requests_enabled   = true
      }
    }
  }

  rule {
    name     = "per-ip-rate-limit"
    priority = 100

    action {
      dynamic "block" {
        for_each = var.enforcement_mode == "enforce" ? [1] : []
        content {}
      }
      dynamic "count" {
        for_each = var.enforcement_mode == "audit" ? [1] : []
        content {}
      }
    }

    statement {
      rate_based_statement {
        limit              = var.per_ip_rate_limit
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "PerIpRateLimit"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "per-tenant-rate-limit"
    priority = 110

    action {
      dynamic "block" {
        for_each = var.enforcement_mode == "enforce" ? [1] : []
        content {}
      }
      dynamic "count" {
        for_each = var.enforcement_mode == "audit" ? [1] : []
        content {}
      }
    }

    statement {
      rate_based_statement {
        limit              = var.per_tenant_rate_limit
        aggregate_key_type = "CUSTOM_KEYS"

        custom_key {
          uri_path {
            text_transformation {
              priority = 1
              type     = "LOWERCASE"
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "PerTenantRateLimit"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "oversized-body"
    priority = 120

    action {
      block {}
    }

    statement {
      size_constraint_statement {
        field_to_match {
          body {
            oversize_handling = "MATCH"
          }
        }
        comparison_operator = "GT"
        size                = var.max_request_body_bytes

        text_transformation {
          priority = 1
          type     = "NONE"
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "OversizedBody"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.environment}ControlPlane"
    sampled_requests_enabled   = true
  }

  tags = local.common_tags
}

resource "aws_wafv2_web_acl_association" "control_plane" {
  count = var.api_gateway_stage_arn == "" ? 0 : 1

  resource_arn = var.api_gateway_stage_arn
  web_acl_arn  = aws_wafv2_web_acl.control_plane.arn
}


resource "aws_cloudwatch_log_group" "waf" {
  name              = "aws-waf-logs-${var.name_prefix}-control-plane-${var.environment}"
  retention_in_days = var.security_log_retention_days
  kms_key_id        = var.logs_kms_key_arn

  tags = merge(local.common_tags, {
    Purpose = "waf-security-log"
  })
}

resource "aws_wafv2_web_acl_logging_configuration" "control_plane" {
  resource_arn            = aws_wafv2_web_acl.control_plane.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]

  redacted_fields {
    single_header {
      name = "authorization"
    }
  }

  redacted_fields {
    single_header {
      name = "cookie"
    }
  }
}


resource "aws_cloudwatch_metric_alarm" "waf_blocked_requests" {
  count = var.alarm_sns_topic_arn == "" ? 0 : 1

  alarm_name          = "${var.name_prefix}-waf-blocked-requests-${var.environment}"
  namespace           = "AWS/WAFV2"
  metric_name         = "BlockedRequests"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.blocked_request_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    WebACL = aws_wafv2_web_acl.control_plane.name
    Region = var.region
    Rule   = "ALL"
  }

  alarm_description = join(" ", [
    "The control-plane WAF blocked more than ${var.blocked_request_alarm_threshold} requests",
    "in five minutes. Investigate before raising the limit: a legitimate burst and an attack",
    "look the same from the count alone, and §11 forbids throttling included capabilities.",
  ])

  alarm_actions = [var.alarm_sns_topic_arn]

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "waf_counted_requests" {
  count = var.alarm_sns_topic_arn == "" && var.enforcement_mode == "audit" ? 0 : 1

  alarm_name          = "${var.name_prefix}-waf-counted-requests-${var.environment}"
  namespace           = "AWS/WAFV2"
  metric_name         = "CountedRequests"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.blocked_request_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    WebACL = aws_wafv2_web_acl.control_plane.name
    Region = var.region
    Rule   = "ALL"
  }

  alarm_description = join(" ", [
    "In audit mode this is what *would* have been blocked. Review it before switching",
    "enforcement_mode to \"enforce\" — a non-zero count with legitimate traffic in it means",
    "enforcement would break a customer.",
  ])

  alarm_actions = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]

  tags = local.common_tags
}
