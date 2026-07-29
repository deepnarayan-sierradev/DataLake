# ---------------------------------------------------------------------------
# WAF on the control plane (DL-SEC-13, gap 7).
#
# §11 forbids throttling *included platform capabilities*; it says nothing about abuse
# protection. So the rate limits here are deliberately generous and the alarm fires well
# before the block does — a legitimate burst produces a page for an operator, not a 403 for
# a customer.
#
# Rule order matters and is explicit: managed rule sets run first (known-bad requests are
# cheapest to drop), then per-IP rate limiting, then per-tenant rate limiting.
# ---------------------------------------------------------------------------

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

  # AWS managed rule groups, in evaluation order. Each is count-only in `audit` mode so a
  # rollout can prove no legitimate request would be blocked before enforcement (the same
  # audit-then-enforce discipline DL-SEC-01 requires for IAM conditions).
  managed_rule_groups = [
    { name = "AWSManagedRulesCommonRuleSet", priority = 10 },
    { name = "AWSManagedRulesKnownBadInputsRuleSet", priority = 20 },
    { name = "AWSManagedRulesSQLiRuleSet", priority = 30 },
    { name = "AWSManagedRulesAmazonIpReputationList", priority = 40 },
  ]
}

resource "aws_wafv2_web_acl" "control_plane" {
  name        = "${var.environment}-edl-control-plane"
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

      # In audit mode every managed rule only counts, so CloudWatch shows exactly what would
      # have been blocked. Flip `enforcement_mode` to "enforce" once that is verified clean.
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

  # Per-IP rate limit. Generous by design: the point is stopping a scripted attack, not
  # shaping a customer's dashboard load.
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

  # Per-tenant rate limit, keyed on the tenant path segment rather than the IP, so one
  # tenant's runaway integration cannot consume another tenant's budget.
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

  # Oversized bodies are dropped outright in both modes: a 10 MB POST to a JSON control-plane
  # endpoint is never legitimate, so there is nothing to audit.
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
    metric_name                = "${var.environment}EdlControlPlane"
    sampled_requests_enabled   = true
  }

  tags = local.common_tags
}

resource "aws_wafv2_web_acl_association" "control_plane" {
  count = var.api_gateway_stage_arn == "" ? 0 : 1

  resource_arn = var.api_gateway_stage_arn
  web_acl_arn  = aws_wafv2_web_acl.control_plane.arn
}

# ---------------------------------------------------------------------------
# WAF logging. OWASP A09: the security log stream is separate from application logs with
# extended retention, so an incident investigation is not competing with pipeline noise.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "waf" {
  # AWS requires the `aws-waf-logs-` prefix on a WAF logging destination.
  name              = "aws-waf-logs-${var.environment}-edl-control-plane"
  retention_in_days = var.security_log_retention_days
  kms_key_id        = var.logs_kms_key_arn

  tags = merge(local.common_tags, {
    Purpose = "waf-security-log"
  })
}

resource "aws_wafv2_web_acl_logging_configuration" "control_plane" {
  resource_arn            = aws_wafv2_web_acl.control_plane.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]

  # Authorization headers carry bearer tokens; redacting them keeps a credential out of the
  # security log the way the structured logger already keeps them out of application logs.
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

# ---------------------------------------------------------------------------
# Alarms. `WafBlockedRequests` is emitted by AWS into the WAF namespace, so this alarm reads
# that namespace directly rather than the platform namespace.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "waf_blocked_requests" {
  count = var.alarm_sns_topic_arn == "" ? 0 : 1

  alarm_name          = "${var.environment}-edl-waf-blocked-requests"
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

  alarm_name          = "${var.environment}-edl-waf-counted-requests"
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
