
resource "aws_sns_topic" "platform_paging" {
  count = var.pagerduty_integration_url != "" ? 1 : 0

  name              = "${var.name_prefix}-platform-paging-${var.environment}"
  kms_master_key_id = var.logs_kms_key_arn

  tags = merge(var.tags, {
    Name    = "${var.name_prefix}-platform-paging-${var.environment}"
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
  pipeline_freshness_thresholds = {
    dev  = { seconds = 86400, paging = false }
    uat  = { seconds = 14400, paging = false }
    prod = { seconds = 7200, paging = true }
  }
  pipeline_freshness = local.pipeline_freshness_thresholds[var.environment]

  platform_metric_alarms = {
    ExtractionDurationMs = { threshold = 600000, paging = false, statistic = "Maximum" }
    RecordsSkipped       = { threshold = 100000, paging = false, statistic = "Sum" }
    RetryCount           = { threshold = 20, paging = false, statistic = "Sum" }
    StageDurationMs      = { threshold = 600000, paging = false, statistic = "Maximum" }
    GoldenRecordCount    = { threshold = 0, paging = false, statistic = "Sum", comparison = "LessThanOrEqualToThreshold", missing_data = "notBreaching" }
    ClusterCount         = { threshold = 0, paging = false, statistic = "Sum", comparison = "LessThanOrEqualToThreshold", missing_data = "notBreaching" }

    RateLimitHits            = { threshold = 50, paging = false, statistic = "Sum" }
    RateLimitBackoffMs       = { threshold = 300000, paging = false, statistic = "Sum" }
    PagesFetched             = { threshold = 100000, paging = false, statistic = "Sum" }
    WebhookEventsReceived    = { threshold = 100000, paging = false, statistic = "Sum" }
    WebhookSignatureFailures = { threshold = 0, paging = false, statistic = "Sum" }
    WritebackRecords         = { threshold = 100000, paging = false, statistic = "Sum" }
    WritebackFailures        = { threshold = 0, paging = false, statistic = "Sum" }
    SourceApiErrors          = { threshold = 25, paging = false, statistic = "Sum" }
    CheckpointedRuns         = { threshold = 10, paging = false, statistic = "Sum" }

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

    SemanticQueriesCompiled = { threshold = 100000, paging = false, statistic = "Sum" }
    SemanticQueryLatencyMs  = { threshold = 5000, paging = false, statistic = "Maximum" }
    SemanticAccessDenied    = { threshold = 25, paging = false, statistic = "Sum" }
    SemanticCacheHitRate    = { threshold = 20, paging = false, statistic = "Average", comparison = "LessThanThreshold" }
    ModelPublishes          = { threshold = 20, paging = false, statistic = "Sum" }
    ModelValidationFailures = { threshold = 0, paging = false, statistic = "Sum" }
    KpiValidationFailures   = { threshold = 0, paging = false, statistic = "Sum" }

    WorkflowExecutions           = { threshold = 10000, paging = false, statistic = "Sum" }
    WorkflowConditionEvaluations = { threshold = 50000, paging = false, statistic = "Sum" }
    WorkflowActionsExecuted      = { threshold = 50000, paging = false, statistic = "Sum" }
    WorkflowActionFailures       = { threshold = 5, paging = false, statistic = "Sum" }
    WorkflowTasksOpen            = { threshold = 100, paging = false, statistic = "Maximum" }
    WorkflowTaskAgeHours         = { threshold = 72, paging = false, statistic = "Maximum" }
    WorkflowEscalations          = { threshold = 10, paging = false, statistic = "Sum" }
    WorkflowCircuitBreakerOpen   = { threshold = 0, paging = false, statistic = "Sum" }
    WorkflowDlqDepth             = { threshold = 0, paging = false, statistic = "Maximum" }

    ServingStoreLoadRows         = { threshold = 10000000, paging = false, statistic = "Sum" }
    ServingStoreLoadDurationMs   = { threshold = 900000, paging = false, statistic = "Maximum" }
    ServingStoreLoadFailures     = { threshold = 0, paging = false, statistic = "Sum" }
    ServingStoreSkippedNoConfig  = { threshold = 0, paging = false, statistic = "Sum" }
    ServingStoreConnectionErrors = { threshold = 0, paging = false, statistic = "Sum" }
    VpnClientConnections         = { threshold = 200, paging = false, statistic = "Maximum" }
    VpnCertificateDaysToExpiry   = { threshold = 30, paging = false, statistic = "Minimum", comparison = "LessThanThreshold" }
    ServingQueryLatencyMs        = { threshold = 5000, paging = false, statistic = "Maximum" }
    ServingConcurrentConnections = { threshold = 45, paging = false, statistic = "Maximum" }

    AuthenticationFailures    = { threshold = 25, paging = false, statistic = "Sum" }
    AuthorizationDenials      = { threshold = 50, paging = false, statistic = "Sum" }
    AdminActions              = { threshold = 25, paging = false, statistic = "Sum" }
    CrossTenantAccessAttempts = { threshold = 0, paging = true, statistic = "Sum" }
    IamBoundaryAccessDenied   = { threshold = 0, paging = true, statistic = "Sum" }
    WafBlockedRequests        = { threshold = 100, paging = false, statistic = "Sum" }
    CredentialRotationAge     = { threshold = 7776000, paging = false, statistic = "Maximum" }
    RowLevelPredicateApplied  = { threshold = 1000000, paging = false, statistic = "Sum" }
    SecretRetrievalFailures   = { threshold = 0, paging = false, statistic = "Sum" }

    PipelineFreshnessSeconds = { threshold = local.pipeline_freshness.seconds, paging = local.pipeline_freshness.paging, statistic = "Maximum" }
    StageRetries             = { threshold = 20, paging = false, statistic = "Sum" }
    DlqDepth                 = { threshold = 0, paging = false, statistic = "Maximum" }
    DlqMessagesEnqueued      = { threshold = 200, paging = false, statistic = "Sum" }
    ReplaySuccessRate        = { threshold = 90, paging = false, statistic = "Average", comparison = "LessThanThreshold" }
    CostPerTenantUsd         = { threshold = 5000, paging = false, statistic = "Maximum" }
    LambdaMemoryUtilization  = { threshold = 90, paging = false, statistic = "Maximum" }
    DeploymentDurationMs     = { threshold = 1800000, paging = false, statistic = "Maximum" }
    PostDeploySmokeFailures  = { threshold = 0, paging = true, statistic = "Sum" }

    ExportJobsRequested     = { threshold = 1000, paging = false, statistic = "Sum" }
    ExportJobsCompleted     = { threshold = 1000, paging = false, statistic = "Sum" }
    ExportJobsFailed        = { threshold = 0, paging = false, statistic = "Sum" }
    ExportBytes             = { threshold = 107374182400, paging = false, statistic = "Sum" }
    ExportDurationMs        = { threshold = 900000, paging = false, statistic = "Maximum" }
    RetentionRecordsExpired = { threshold = 10000000, paging = false, statistic = "Sum" }
    LegalHoldsActive        = { threshold = 50, paging = false, statistic = "Maximum" }
    DeletionStepsCompleted  = { threshold = 1000, paging = false, statistic = "Sum" }
    PhiGateBlocks           = { threshold = 0, paging = false, statistic = "Sum" }

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

    CrossScopeAccessAttempts     = { threshold = 0, paging = true, statistic = "Sum" }
    ScopePredicateApplied        = { threshold = 10000000, paging = false, statistic = "Sum" }
    UnattributedRowRate          = { threshold = 5, paging = false, statistic = "Maximum" }
    ScopeGrantExpansions         = { threshold = 100000, paging = false, statistic = "Sum" }
    EmptyScopeDenials            = { threshold = 10, paging = false, statistic = "Sum" }
    UnrestrictedScopeReads       = { threshold = 500, paging = false, statistic = "Sum" }
    ScopeFilterDrift             = { threshold = 0, paging = true, statistic = "Maximum" }
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

  alarm_name          = "${var.name_prefix}-${lower(each.key)}-${var.environment}"
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

  alarm_actions = compact([
    aws_sns_topic.platform_alerts.arn,
    each.value.paging ? try(aws_sns_topic.platform_paging[0].arn, "") : "",
  ])
  ok_actions = []

  tags = merge(var.tags, {
    Name    = "${var.name_prefix}-${lower(each.key)}-${var.environment}"
    Paging  = tostring(each.value.paging)
    Purpose = "platform-metric-alarm"
  })
}


resource "aws_cloudwatch_metric_alarm" "lambda_insights_memory" {
  for_each = local.monitored_lambdas

  alarm_name          = "${var.name_prefix}-${each.key}-memory-utilization-${var.environment}"
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
    Name    = "${var.name_prefix}-${each.key}-memory-utilization-${var.environment}"
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


locals {
  absence_alarmed_metrics = {
    ScopePredicateApplied = {
      reason = "every data-returning request must build a scope predicate (DL-SCOPE-14); zero means the isolation control is inert, not that nothing was denied"
      period = 86400
    }
    EffectiveVersionTransitions = {
      reason = "a run consuming configuration must record the version it consumed (DL-CFG-08); zero means config pinning is not wired into any stage"
      period = 86400
    }
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

  alarm_name          = "${var.name_prefix}-${lower(each.key)}-inert-${var.environment}"
  namespace           = "EnterpriseDatalake"
  metric_name         = each.key
  statistic           = "Sum"
  period              = each.value.period
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  alarm_description = join(" ", [
    "CONTROL INERT: ${each.key} published no data points in the last ${each.value.period}s.",
    each.value.reason,
  ])

  alarm_actions = [aws_sns_topic.platform_alerts.arn]
  ok_actions    = [aws_sns_topic.platform_alerts.arn]

  tags = merge(var.tags, {
    Name    = "${var.name_prefix}-${lower(each.key)}-inert-${var.environment}"
    Purpose = "control-liveness"
  })
}
