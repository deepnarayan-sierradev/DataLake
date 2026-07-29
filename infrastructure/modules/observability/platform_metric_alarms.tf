# ---------------------------------------------------------------------------
# Alarms for every metric in the platform catalogue (contracts/platform_metrics.py).
#
# One alarm definition source, map-driven, so a new metric is one entry rather than a copied
# 20-line resource. `observability/tests/test_alarm_emitter_reconciliation.py` reconciles this
# map against the catalogue in both directions: a metric with no alarm and an alarm with no
# catalogue entry both fail CI.
#
# `paging = true` routes to the PagerDuty topic rather than email. The four metrics marked
# paging are the ones whose non-zero value means either an active attack or a defect that
# would already have leaked data or produced unattributable output:
#   CrossTenantAccessAttempts, CrossScopeAccessAttempts, ResolutionScopeViolations,
#   ConfigVersionMismatchWithinRun — plus ReconciliationVariancePct, because an undetected
#   revenue mismatch is the highest-consequence failure mode in this platform.
# ---------------------------------------------------------------------------

# Dedicated paging topic, created only when a PagerDuty integration URL is configured.
# Separate from the ops-email topic so a paging alarm is genuinely a page, not another email.
resource "aws_sns_topic" "platform_paging" {
  count = var.pagerduty_integration_url != "" ? 1 : 0

  name              = "${var.environment}-edl-platform-paging"
  kms_master_key_id = var.logs_kms_key_arn

  tags = merge(var.tags, {
    Name    = "${var.environment}-edl-platform-paging"
    Purpose = "paging-alarms"
  })
}

resource "aws_sns_topic_subscription" "platform_paging_pagerduty" {
  count = var.pagerduty_integration_url != "" ? 1 : 0

  topic_arn              = aws_sns_topic.platform_paging[0].arn
  protocol               = "https"
  endpoint               = var.pagerduty_integration_url
  endpoint_auto_confirms = true
}

locals {
  # ---------------------------------------------------------------------------
  # Pipeline freshness is the only alarm that measures the *commitment* rather than the failure
  # handling: the agreed expectation (2026-07-29) is end-to-end completion within 2-4 hours, same
  # business day. A run that *succeeds* in five hours breaches that and produces no DLQ message,
  # so no DLQ alarm can see it.
  #
  # It was 86400s (24h) and non-paging — 6-12x looser than the commitment, which made it decorative.
  # Set at the tight end of the range in prod so the loose end is margin, not the target.
  # See docs/SCALE_AND_DLQ_THRESHOLDS.md.
  # ---------------------------------------------------------------------------
  pipeline_freshness_thresholds = {
    dev     = { seconds = 86400, paging = false }
    staging = { seconds = 14400, paging = false }
    prod    = { seconds = 7200, paging = true }
  }
  pipeline_freshness = local.pipeline_freshness_thresholds[var.environment]

  # comparison: GreaterThanThreshold unless the metric is a "should be non-zero" signal.
  # missing_data: "notBreaching" for event counters (silence is normal); "breaching" only
  # where absence is itself the failure (an expected load producing nothing).
  platform_metric_alarms = {
    # ── Extraction / pipeline core ──────────────────────────────────────────
    ExtractionDurationMs = { threshold = 600000, paging = false, statistic = "Maximum" }
    RecordsSkipped       = { threshold = 100000, paging = false, statistic = "Sum" }
    RetryCount           = { threshold = 20, paging = false, statistic = "Sum" }
    StageDurationMs      = { threshold = 600000, paging = false, statistic = "Maximum" }
    GoldenRecordCount    = { threshold = 0, paging = false, statistic = "Sum", comparison = "LessThanOrEqualToThreshold", missing_data = "notBreaching" }
    ClusterCount         = { threshold = 0, paging = false, statistic = "Sum", comparison = "LessThanOrEqualToThreshold", missing_data = "notBreaching" }

    # ── DL-01 connectors ───────────────────────────────────────────────────
    RateLimitHits            = { threshold = 50, paging = false, statistic = "Sum" }
    RateLimitBackoffMs       = { threshold = 300000, paging = false, statistic = "Sum" }
    PagesFetched             = { threshold = 100000, paging = false, statistic = "Sum" }
    WebhookEventsReceived    = { threshold = 100000, paging = false, statistic = "Sum" }
    WebhookSignatureFailures = { threshold = 0, paging = false, statistic = "Sum" }
    WritebackRecords         = { threshold = 100000, paging = false, statistic = "Sum" }
    WritebackFailures        = { threshold = 0, paging = false, statistic = "Sum" }
    SourceApiErrors          = { threshold = 25, paging = false, statistic = "Sum" }
    CheckpointedRuns         = { threshold = 10, paging = false, statistic = "Sum" }

    # ── DL-02 quality and reconciliation ───────────────────────────────────
    QualityViolations         = { threshold = 100, paging = false, statistic = "Sum" }
    QualityGateBlocks         = { threshold = 0, paging = false, statistic = "Sum" }
    CompletenessRate          = { threshold = 95, paging = false, statistic = "Minimum", comparison = "LessThanThreshold" }
    DuplicateRate             = { threshold = 1, paging = false, statistic = "Maximum" }
    OrphanRate                = { threshold = 1, paging = false, statistic = "Maximum" }
    ReconciliationVariancePct = { threshold = 0, paging = true, statistic = "Maximum" }
    ReconciliationFailures    = { threshold = 0, paging = true, statistic = "Sum" }
    BackfillChunksCompleted   = { threshold = 10000, paging = false, statistic = "Sum" }
    BackfillChunksFailed      = { threshold = 0, paging = false, statistic = "Sum" }
    BackfillRowsPerSecond     = { threshold = 10, paging = false, statistic = "Average", comparison = "LessThanThreshold" }

    # ── DL-03 semantic ─────────────────────────────────────────────────────
    SemanticQueriesCompiled = { threshold = 100000, paging = false, statistic = "Sum" }
    SemanticQueryLatencyMs  = { threshold = 5000, paging = false, statistic = "Maximum" }
    SemanticAccessDenied    = { threshold = 25, paging = false, statistic = "Sum" }
    SemanticCacheHitRate    = { threshold = 20, paging = false, statistic = "Average", comparison = "LessThanThreshold" }
    ModelPublishes          = { threshold = 20, paging = false, statistic = "Sum" }
    ModelValidationFailures = { threshold = 0, paging = false, statistic = "Sum" }
    KpiValidationFailures   = { threshold = 0, paging = false, statistic = "Sum" }

    # ── DL-06 workflow ─────────────────────────────────────────────────────
    WorkflowExecutions           = { threshold = 10000, paging = false, statistic = "Sum" }
    WorkflowConditionEvaluations = { threshold = 50000, paging = false, statistic = "Sum" }
    WorkflowActionsExecuted      = { threshold = 50000, paging = false, statistic = "Sum" }
    WorkflowActionFailures       = { threshold = 5, paging = false, statistic = "Sum" }
    WorkflowTasksOpen            = { threshold = 100, paging = false, statistic = "Maximum" }
    WorkflowTaskAgeHours         = { threshold = 72, paging = false, statistic = "Maximum" }
    WorkflowEscalations          = { threshold = 10, paging = false, statistic = "Sum" }
    WorkflowCircuitBreakerOpen   = { threshold = 0, paging = false, statistic = "Sum" }
    WorkflowDlqDepth             = { threshold = 0, paging = false, statistic = "Maximum" }

    # ── DL-07 serving ──────────────────────────────────────────────────────
    ServingStoreLoadRows         = { threshold = 10000000, paging = false, statistic = "Sum" }
    ServingStoreLoadDurationMs   = { threshold = 900000, paging = false, statistic = "Maximum" }
    ServingStoreLoadFailures     = { threshold = 0, paging = false, statistic = "Sum" }
    ServingStoreSkippedNoConfig  = { threshold = 0, paging = false, statistic = "Sum" }
    ServingStoreConnectionErrors = { threshold = 0, paging = false, statistic = "Sum" }
    VpnClientConnections         = { threshold = 200, paging = false, statistic = "Maximum" }
    VpnCertificateDaysToExpiry   = { threshold = 30, paging = false, statistic = "Minimum", comparison = "LessThanThreshold" }
    ServingQueryLatencyMs        = { threshold = 5000, paging = false, statistic = "Maximum" }
    ServingConcurrentConnections = { threshold = 45, paging = false, statistic = "Maximum" }

    # ── DL-08 security ─────────────────────────────────────────────────────
    AuthenticationFailures    = { threshold = 25, paging = false, statistic = "Sum" }
    AuthorizationDenials      = { threshold = 50, paging = false, statistic = "Sum" }
    AdminActions              = { threshold = 25, paging = false, statistic = "Sum" }
    CrossTenantAccessAttempts = { threshold = 0, paging = true, statistic = "Sum" }
    WafBlockedRequests        = { threshold = 100, paging = false, statistic = "Sum" }
    CredentialRotationAge     = { threshold = 7776000, paging = false, statistic = "Maximum" }
    RowLevelPredicateApplied  = { threshold = 1000000, paging = false, statistic = "Sum" }
    SecretRetrievalFailures   = { threshold = 0, paging = false, statistic = "Sum" }

    # ── DL-09 operations ───────────────────────────────────────────────────
    PipelineFreshnessSeconds = { threshold = local.pipeline_freshness.seconds, paging = local.pipeline_freshness.paging, statistic = "Maximum" }
    StageRetries             = { threshold = 20, paging = false, statistic = "Sum" }
    DlqDepth                 = { threshold = 0, paging = false, statistic = "Maximum" }
    ReplaySuccessRate        = { threshold = 90, paging = false, statistic = "Average", comparison = "LessThanThreshold" }
    CostPerTenantUsd         = { threshold = 5000, paging = false, statistic = "Maximum" }
    LambdaMemoryUtilization  = { threshold = 90, paging = false, statistic = "Maximum" }
    DeploymentDurationMs     = { threshold = 1800000, paging = false, statistic = "Maximum" }
    PostDeploySmokeFailures  = { threshold = 0, paging = true, statistic = "Sum" }

    # ── DL-10 portability and compliance ───────────────────────────────────
    ExportJobsRequested     = { threshold = 1000, paging = false, statistic = "Sum" }
    ExportJobsCompleted     = { threshold = 1000, paging = false, statistic = "Sum" }
    ExportJobsFailed        = { threshold = 0, paging = false, statistic = "Sum" }
    ExportBytes             = { threshold = 107374182400, paging = false, statistic = "Sum" }
    ExportDurationMs        = { threshold = 900000, paging = false, statistic = "Maximum" }
    RetentionRecordsExpired = { threshold = 10000000, paging = false, statistic = "Sum" }
    LegalHoldsActive        = { threshold = 50, paging = false, statistic = "Maximum" }
    DeletionStepsCompleted  = { threshold = 1000, paging = false, statistic = "Sum" }
    PhiGateBlocks           = { threshold = 0, paging = false, statistic = "Sum" }

    # ── DL-11 configuration propagation ────────────────────────────────────
    ConfigPropagationLagSeconds           = { threshold = 3600, paging = false, statistic = "Maximum" }
    ConfigVersionPinFailures              = { threshold = 0, paging = false, statistic = "Sum" }
    ConfigVersionMismatchWithinRun        = { threshold = 0, paging = true, statistic = "Sum" }
    ConfigCacheStaleServed                = { threshold = 0, paging = false, statistic = "Sum" }
    EffectiveVersionTransitions           = { threshold = 500, paging = false, statistic = "Sum" }
    ConfigRollbacks                       = { threshold = 0, paging = false, statistic = "Sum" }
    ReprocessJobsStarted                  = { threshold = 50, paging = false, statistic = "Sum" }
    ReprocessJobsCompleted                = { threshold = 50, paging = false, statistic = "Sum" }
    ReprocessJobsFailed                   = { threshold = 0, paging = false, statistic = "Sum" }
    ReprocessRowsRecomputed               = { threshold = 100000000, paging = false, statistic = "Sum" }
    RestatementEventsEmitted              = { threshold = 0, paging = false, statistic = "Sum" }
    ConfigSchemaIncompatibilityRejections = { threshold = 0, paging = false, statistic = "Sum" }
    CredentialCachePropagationLagSeconds  = { threshold = 300, paging = false, statistic = "Maximum" }
    PublishesNotYetEffective              = { threshold = 25, paging = false, statistic = "Maximum" }

    # ── DL-12 connections and scope isolation ──────────────────────────────
    CrossScopeAccessAttempts     = { threshold = 0, paging = true, statistic = "Sum" }
    ScopePredicateApplied        = { threshold = 10000000, paging = false, statistic = "Sum" }
    UnattributedRowRate          = { threshold = 5, paging = false, statistic = "Maximum" }
    ScopeGrantExpansions         = { threshold = 100000, paging = false, statistic = "Sum" }
    EmptyScopeDenials            = { threshold = 10, paging = false, statistic = "Sum" }
    ConnectionHealth             = { threshold = 0, paging = false, statistic = "Minimum", comparison = "LessThanThreshold" }
    ConnectionCredentialFailures = { threshold = 0, paging = false, statistic = "Sum" }
    ConnectionsPerTenant         = { threshold = 100, paging = false, statistic = "Maximum" }
    ResolutionScopeViolations    = { threshold = 0, paging = true, statistic = "Sum" }
    AggregateSuppressions        = { threshold = 1000, paging = false, statistic = "Sum" }
    BenchmarkCohortSize          = { threshold = 5, paging = false, statistic = "Minimum", comparison = "LessThanThreshold" }
  }
}

resource "aws_cloudwatch_metric_alarm" "platform_metric" {
  for_each = local.platform_metric_alarms

  alarm_name          = "${var.environment}-edl-${lower(each.key)}"
  namespace           = "EnterpriseDatalake"
  metric_name         = each.key
  statistic           = each.value.statistic
  period              = 300
  evaluation_periods  = 1
  threshold           = each.value.threshold
  comparison_operator = try(each.value.comparison, "GreaterThanThreshold")
  treat_missing_data  = try(each.value.missing_data, "notBreaching")

  alarm_description = join(" ", [
    "Platform metric ${each.key} breached its threshold.",
    each.value.paging ? "PAGING: any breach is either an active attack or a defect that has already produced incorrect or exposed data." : "Informational: review on the platform-operations dashboard.",
  ])

  # Paging metrics route to the PagerDuty-subscribed topic when one is configured; otherwise
  # every alarm falls back to the ops-email topic rather than being silently unrouted.
  alarm_actions = compact([
    aws_sns_topic.platform_alerts.arn,
    each.value.paging ? try(aws_sns_topic.platform_paging[0].arn, "") : "",
  ])
  ok_actions = []

  tags = merge(var.tags, {
    Name    = "${var.environment}-edl-${lower(each.key)}"
    Paging  = tostring(each.value.paging)
    Purpose = "platform-metric-alarm"
  })
}

# ---------------------------------------------------------------------------
# Lambda Insights memory alarm (DL-OPS-05, closing the last FR-F0.6 item).
#
# Watches the Lambda Insights namespace rather than the platform namespace, because memory
# utilisation is measured by the extension, not emitted by application code.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "lambda_insights_memory" {
  for_each = toset([
    var.extraction_lambda_name,
    var.transformation_lambda_name,
    var.entity_resolution_lambda_name,
    var.analytics_publisher_lambda_name,
  ])

  alarm_name          = "${var.environment}-edl-${each.value}-memory-utilization"
  namespace           = "LambdaInsights"
  metric_name         = "memory_utilization"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 90
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    function_name = each.value
  }

  alarm_description = join(" ", [
    "Lambda ${each.value} exceeded 90% memory utilisation.",
    "A function running at the memory ceiling is one large batch away from an out-of-memory",
    "kill, which produces no failure record on the old path — raise the memory or reduce the",
    "batch before it happens in production.",
  ])

  alarm_actions = [aws_sns_topic.platform_alerts.arn]

  tags = merge(var.tags, {
    Name    = "${var.environment}-edl-${each.value}-memory-utilization"
    Purpose = "lambda-insights-memory"
  })
}

output "platform_metric_alarm_names" {
  description = "Names of every catalogued platform metric alarm."
  value       = [for name, alarm in aws_cloudwatch_metric_alarm.platform_metric : alarm.alarm_name]
}

output "paging_metric_names" {
  description = "Metrics whose breach pages rather than emails."
  value       = [for name, spec in local.platform_metric_alarms : name if spec.paging]
}

# ---------------------------------------------------------------------------
# G6 — alarm on ABSENCE for controls whose failure mode is silence.
#
# The 2026-07-28 audit found the scope predicate was never constructed at runtime: every query
# ran tenant-wide, and the only signal was a metric sitting at zero, which is indistinguishable
# from "no violations". A control that reports nothing when inert cannot be trusted to report
# anything when breached.
#
# These alarms invert the usual test: they fire when the metric is BELOW one over the window,
# with `treat_missing_data = "breaching"` so a metric that never arrives at all is a breach
# rather than an unknown. They are the reason an unwired control cannot look healthy.
# ---------------------------------------------------------------------------

locals {
  # metric -> why its absence is a defect. Only controls that MUST fire on every request or
  # every run belong here; a metric that is legitimately zero on a quiet day does not.
  absence_alarmed_metrics = {
    # A day, not an hour: a quiet hour with no requests is normal, a whole day with no
    # predicate in a live environment is a defect. Shorter windows would page on quiet traffic
    # and the alarm would be muted, which defeats the purpose.
    ScopePredicateApplied = {
      reason = "every data-returning request must build a scope predicate (DL-SCOPE-14); zero means the isolation control is inert, not that nothing was denied"
      period = 86400
    }
    EffectiveVersionTransitions = {
      reason = "a run consuming configuration must record the version it consumed (DL-CFG-08); zero means config pinning is not wired into any stage"
      period = 86400
    }
    # Counts batches stamped, not the unattributed *rate* — a rate legitimately reads 0 on a
    # healthy day, so absence-alarming on it would fire on success.
    # L17: usage metering feeds billing. A period with no computed usage is not "a quiet month",
    # it is a metering job that stopped running — and an invoice built on it would be wrong.
    TenantRecordsProcessed = {
      reason = "usage metering must publish every period (L17); zero means the meter is not running and any invoice derived from it understates"
      period = 86400
    }
    ScopeAttributionApplied = {
      reason = "curated writes must stamp scope_unit_id (DL-SCOPE-07); zero means rows are landing unattributed and every downstream row filter is filtering on an absent column"
      period = 86400
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "control_is_inert" {
  for_each = var.enable_absence_alarms ? local.absence_alarmed_metrics : {}

  alarm_name          = "${var.environment}-edl-${lower(each.key)}-inert"
  namespace           = "EnterpriseDatalake"
  metric_name         = each.key
  statistic           = "Sum"
  period              = each.value.period
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  # A metric that never publishes is the exact failure being watched for, so missing data is a
  # breach. Every other alarm in this file uses notBreaching for the opposite reason.
  treat_missing_data = "breaching"

  alarm_description = join(" ", [
    "CONTROL INERT: ${each.key} published no data points in the last ${each.value.period}s.",
    each.value.reason,
  ])

  alarm_actions = [aws_sns_topic.platform_alerts.arn]
  ok_actions    = [aws_sns_topic.platform_alerts.arn]

  tags = merge(var.tags, {
    Name    = "${var.environment}-edl-${lower(each.key)}-inert"
    Purpose = "control-liveness"
  })
}
